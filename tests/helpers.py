from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from bot.config import parse_business

TZ = ZoneInfo('Europe/Moscow')
NOW = datetime(2030, 1, 7, 5, 0, tzinfo=UTC)  # Понедельник, 08:00 в Москве.


def business(**overrides):
    raw = {'name': 'Тест', 'texts': {'welcome': 'Привет', 'thanks': 'Спасибо', 'cancel_rules': 'Правила'},
           'services': [{'id': 'cut', 'name': 'Стрижка', 'duration_minutes': 60, 'price': 1000}],
           'masters': [{'id': 'one', 'name': 'Иван', 'services': ['cut'],
                        'working_hours': {day: ['10:00', '18:00'] for day in ('mon', 'tue', 'wed', 'thu', 'fri')},
                        'breaks': {'mon': [['13:00', '14:00']]}}]}
    raw.update(overrides)
    return parse_business(raw)
