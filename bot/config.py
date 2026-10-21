"""Загрузка настроек; ошибки конфига обнаруживаются до подключения к Telegram."""
import json
import os
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
WEEKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    duration_minutes: int
    price: float


@dataclass(frozen=True)
class Master:
    id: str
    name: str
    services: tuple[str, ...]
    working_hours: dict[str, tuple[time, time]]
    breaks: dict[str, tuple[tuple[time, time], ...]]


@dataclass(frozen=True)
class Business:
    name: str
    texts: dict[str, str]
    services: dict[str, Service]
    masters: dict[str, Master]
    days_off: frozenset[date]
    slot_step_minutes: int = 30
    booking_horizon_days: int = 14
    min_lead_minutes: int = 120
    cancel_deadline_minutes: int = 180


@dataclass(frozen=True)
class Settings:
    token: str
    admin_chat_id: int
    timezone: ZoneInfo
    db_path: Path
    sheet_id: str = ''
    credentials_path: Path | None = None


def text(value, label, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ConfigError(f'{label}: нужна непустая строка до {limit} символов')
    return value.strip()


def integer(value, label, minimum=1, maximum=100000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f'{label}: нужно целое число от {minimum} до {maximum}')
    return value


def identifier(value):
    value = text(value, 'id', 24)
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', value) or value == 'any':
        raise ConfigError('id: только латинские буквы, цифры, _ и -; any зарезервирован')
    return value


def interval(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ConfigError('Интервал задаётся парой ["10:00", "20:00"]')
    try:
        if any(not isinstance(v, str) or not re.fullmatch(r'\d{2}:\d{2}', v) for v in value):
            raise ValueError
        start, end = map(time.fromisoformat, value)
    except ValueError:
        raise ConfigError('Время должно быть в формате ЧЧ:ММ') from None
    if start >= end:
        raise ConfigError('Конец интервала должен быть позже начала; ночные смены не поддерживаются')
    return start, end


def parse_business(raw):
    try:
        if not isinstance(raw, dict):
            raise ConfigError('Корень business.json должен быть объектом')
        name = text(raw['name'], 'Название бизнеса')
        texts = {key: text(raw['texts'][key], f'texts.{key}', 1000)
                 for key in ('welcome', 'thanks', 'cancel_rules')}
        services = {}
        for row in raw['services']:
            sid = identifier(row['id'])
            if sid in services:
                raise ConfigError(f'Повтор id услуги: {sid}')
            price = row['price']
            if type(price) not in (int, float) or not 0 <= price <= 10000000:
                raise ConfigError(f'{sid}: некорректная цена')
            services[sid] = Service(sid, text(row['name'], 'Название услуги', 60),
                                    integer(row['duration_minutes'], 'Длительность', 1, 1440), price)
        masters = {}
        for row in raw['masters']:
            mid = identifier(row['id'])
            if mid in masters:
                raise ConfigError(f'Повтор id мастера: {mid}')
            ids = row['services']
            if not isinstance(ids, list) or not ids or any(s not in services for s in ids):
                raise ConfigError(f'{mid}: список услуг пуст или содержит неизвестные id')
            hours = {key: interval(v) for key, v in row['working_hours'].items()}
            if not hours or any(key not in WEEKDAYS for key in hours):
                raise ConfigError(f'{mid}: дни недели задаются mon, tue, wed, thu, fri, sat, sun')
            breaks = {}
            for day, values in row.get('breaks', {}).items():
                if day not in hours:
                    raise ConfigError(f'{mid}: перерыв в нерабочий день {day}')
                spans = sorted(interval(v) for v in values)
                if any(a < hours[day][0] or b > hours[day][1] for a, b in spans):
                    raise ConfigError(f'{mid}: перерыв за пределами рабочего дня')
                if any(spans[i][0] < spans[i-1][1] for i in range(1, len(spans))):
                    raise ConfigError(f'{mid}: перерывы пересекаются')
                breaks[day] = tuple(spans)
            masters[mid] = Master(mid, text(row['name'], 'Имя мастера', 60), tuple(ids), hours, breaks)
        if not services or not masters:
            raise ConfigError('Нужны хотя бы одна услуга и один мастер')
        if any(not any(sid in m.services for m in masters.values()) for sid in services):
            raise ConfigError('У каждой услуги должен быть хотя бы один мастер')
        days_off = frozenset(date.fromisoformat(v) for v in raw.get('days_off', []))
        return Business(name, texts, services, masters, days_off,
                        integer(raw.get('slot_step_minutes', 30), 'Шаг слотов', 1, 1440),
                        integer(raw.get('booking_horizon_days', 14), 'Горизонт', 1, 366),
                        integer(raw.get('min_lead_minutes', 120), 'Минимальное время до записи', 0),
                        integer(raw.get('cancel_deadline_minutes', 180), 'Срок отмены', 0))
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ConfigError(f'Неверная структура business.json: {exc}') from None


def load_business(path=ROOT / 'config/business.json'):
    try:
        return parse_business(json.loads(Path(path).read_text(encoding='utf-8')))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f'Не удалось прочитать business.json: {exc}') from None


def load_settings():
    load_dotenv(ROOT / '.env', override=False)
    token = os.getenv('BOT_TOKEN', '').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
        raise ConfigError('Заполни BOT_TOKEN в .env: токен от @BotFather')
    try:
        admin = int(os.getenv('ADMIN_CHAT_ID', ''))
        if admin == 0:
            raise ValueError
    except ValueError:
        raise ConfigError('ADMIN_CHAT_ID в .env должен быть ненулевым целым числом') from None
    try:
        timezone = ZoneInfo(os.getenv('TIMEZONE', '').strip() or 'Europe/Moscow')
    except ZoneInfoNotFoundError:
        raise ConfigError('TIMEZONE: неизвестный часовой пояс, например Europe/Moscow') from None
    sheet = os.getenv('GOOGLE_SHEET_ID', '').strip()
    credentials = os.getenv('GOOGLE_CREDENTIALS_PATH', '').strip()
    if bool(sheet) != bool(credentials):
        raise ConfigError('Заполни оба GOOGLE_SHEET_ID и GOOGLE_CREDENTIALS_PATH либо оставь оба пустыми')
    path = ROOT / credentials if credentials else None
    if path and not path.is_file():
        raise ConfigError('GOOGLE_CREDENTIALS_PATH: файл ключа не найден')
    return Settings(token, admin, timezone, ROOT / (os.getenv('DB_PATH', '').strip() or 'data/bot.db'), sheet, path)
