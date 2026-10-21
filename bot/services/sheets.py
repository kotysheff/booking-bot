"""Экспорт выполняется в потоке, чтобы не задерживать Telegram-бота."""
import asyncio
import logging

from bot.db import moment

logger = logging.getLogger(__name__)
HEADERS = ['ID записи', 'Создана (UTC)', 'Начало', 'Конец', 'Часовой пояс', 'Услуга', 'Мастер', 'Имя', 'Телефон', 'Telegram ID', 'Username']


class SheetsExporter:
    def __init__(self, settings, business, db):
        self.settings, self.business, self.db = settings, business, db
        self.lock = asyncio.Lock()
        self.worksheet = None
        self.failed_booking_ids = set()

    def _append(self, row):
        import gspread
        if self.worksheet is None:
            client = gspread.service_account(filename=str(self.settings.credentials_path))
            client.set_timeout(15)
            self.worksheet = client.open_by_key(self.settings.sheet_id).sheet1
        sheet = self.worksheet
        ids = sheet.col_values(1)
        if not ids:
            sheet.append_row(HEADERS, value_input_option='RAW')
        # Проверка ID спасает от дубля при перезапуске после успешного append.
        if str(row['id']) in ids:
            return
        service = self.business.services.get(row['service_id'])
        master = self.business.masters.get(row['master_id'])
        tz = self.settings.timezone
        sheet.append_row([str(row['id']), row['created_at'],
                          moment(row['start_utc']).astimezone(tz).isoformat(),
                          moment(row['end_utc']).astimezone(tz).isoformat(), tz.key,
                          service.name if service else row['service_id'],
                          master.name if master else row['master_id'], row['name'], row['phone'],
                          str(row['client_id']), row.get('username') or ''], value_input_option='RAW')

    async def export(self, row):
        if not self.settings.sheet_id or row['sheet_exported']:
            return
        async with self.lock:
            if row['id'] in self.failed_booking_ids:
                logger.info('Повторная попытка экспорта записи %s в Google Sheets', row['id'])
            try:
                await asyncio.to_thread(self._append, row)
                await self.db.mark_exported(row['id'])
                self.failed_booking_ids.discard(row['id'])
                logger.info('Запись %s успешно экспортирована в Google Sheets', row['id'])
            except Exception as exc:
                self.failed_booking_ids.add(row['id'])
                logger.error('Экспорт записи %s не удался: %s', row['id'], type(exc).__name__)
