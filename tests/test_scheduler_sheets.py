import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gspread.exceptions import SpreadsheetNotFound
from requests.exceptions import ConnectionError as RequestsConnectionError

from bot.db import Database
from bot.scheduler import Reminders
from bot.services.sheets import SheetsExporter
from tests.helpers import NOW, TZ, business


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name)/'bot.db')
        await self.db.init()
        self.business = business()
        self.settings = SimpleNamespace(timezone=TZ, sheet_id='', credentials_path=None)
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.exporter = SimpleNamespace(export=AsyncMock())
        self.service = Reminders(self.bot, self.db, self.settings, self.business, self.exporter)
        self.row, _ = await self.db.book(business=self.business, timezone=TZ, client_id=1, name='Гоша',
                                        phone='+79991234567', username=None, service_id='cut', master_id='one',
                                        start=NOW+timedelta(days=2, hours=3), request_token='request', now=NOW)

    async def asyncTearDown(self):
        self.service.stop()
        self.tmp.cleanup()

    async def test_restart_recovers_jobs(self):
        with patch('bot.scheduler.utcnow', return_value=NOW), patch('bot.db.utcnow', return_value=NOW):
            await self.service.start()
            self.assertIsNotNone(self.service.scheduler.get_job('reminder:1:24'))
            self.assertIsNotNone(self.service.scheduler.get_job('reminder:1:2'))
            self.bot.send_message.assert_not_awaited()

    async def test_send_once_even_when_called_concurrently(self):
        import asyncio
        with patch('bot.scheduler.utcnow', return_value=NOW):
            await asyncio.gather(self.service.send(1, 24), self.service.send(1, 24))
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.db.get(1))['reminded_24'], 1)

    async def test_cancelled_booking_is_not_reminded(self):
        with patch('bot.db.utcnow', return_value=NOW):
            self.assertEqual(await self.service.cancel(1, 1), 'cancelled')
        await self.service.send(1, 24)
        self.bot.send_message.assert_not_awaited()

    async def test_cancel_removes_pending_jobs(self):
        with patch('bot.scheduler.utcnow', return_value=NOW), patch('bot.db.utcnow', return_value=NOW):
            self.service.schedule(self.row)
            await self.service.cancel(1, 1)
            self.assertIsNone(self.service.scheduler.get_job('reminder:1:24'))
            self.assertIsNone(self.service.scheduler.get_job('reminder:1:2'))

    async def test_send_failure_can_retry(self):
        self.bot.send_message.side_effect = RuntimeError('offline')
        with patch('bot.scheduler.utcnow', return_value=NOW):
            await self.service.send(1, 2)
        self.assertEqual((await self.db.get(1))['reminded_2'], 0)
        self.bot.send_message.side_effect = None
        with patch('bot.scheduler.utcnow', return_value=NOW):
            await self.service.send(1, 2)
        self.assertEqual((await self.db.get(1))['reminded_2'], 1)

    async def test_restart_sends_overdue_reminders_before_visit(self):
        later = NOW + timedelta(days=2, hours=2)
        with patch('bot.scheduler.utcnow', return_value=later), patch('bot.db.utcnow', return_value=later):
            await self.service.reconcile()
            self.assertEqual(self.bot.send_message.await_count, 2)
            await self.service.reconcile()
            self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_export_disabled(self):
        exporter = SheetsExporter(self.settings, self.business, self.db)
        with patch('gspread.service_account') as auth:
            await exporter.export(self.row)
            auth.assert_not_called()

    async def test_sheet_raw_values_and_dedup_after_restart(self):
        settings = SimpleNamespace(timezone=TZ, sheet_id='test-sheet', credentials_path=Path('/not-read'))
        exporter = SheetsExporter(settings, self.business, self.db)
        worksheet = Mock()
        worksheet.col_values.return_value = []
        exporter.worksheet = worksheet
        await exporter.export(self.row)
        self.assertEqual(worksheet.append_row.call_count, 2)
        self.assertEqual(worksheet.append_row.call_args.kwargs['value_input_option'], 'RAW')
        self.assertEqual((await self.db.get(1))['sheet_exported'], 1)
        # Имитация успешного append и падения до отметки в локальной БД.
        worksheet.col_values.return_value = ['ID записи', '1']
        worksheet.append_row.reset_mock()
        await exporter.export(self.row)
        worksheet.append_row.assert_not_called()

    async def test_export_failure_does_not_mark_success(self):
        settings = SimpleNamespace(timezone=TZ, sheet_id='test-sheet', credentials_path=Path('/not-read'))
        exporter = SheetsExporter(settings, self.business, self.db)
        exporter.worksheet = Mock()
        exporter.worksheet.col_values.side_effect = RuntimeError('offline')
        await exporter.export(self.row)
        self.assertEqual((await self.db.get(1))['sheet_exported'], 0)

    async def test_reconcile_retries_failed_exports_and_logs_success(self):
        settings = SimpleNamespace(timezone=TZ, sheet_id='test-sheet', credentials_path=Path('/not-read'))
        for error in (SpreadsheetNotFound('missing'), RequestsConnectionError('offline'), ConnectionError('offline')):
            with self.subTest(error=type(error).__name__):
                # Каждая ошибка должна оставить запись в очереди, а успех — убрать её.
                async with self.db.connect() as conn:
                    await conn.execute('UPDATE bookings SET sheet_exported=0 WHERE id=1')
                    await conn.commit()
                exporter = SheetsExporter(settings, self.business, self.db)
                exporter.worksheet = Mock()
                exporter.worksheet.col_values.side_effect = [error, ['ID записи'], ['ID записи']]
                service = Reminders(self.bot, self.db, settings, self.business, exporter)
                with patch('bot.scheduler.utcnow', return_value=NOW), patch('bot.db.utcnow', return_value=NOW):
                    with self.assertLogs('bot.services.sheets', level='INFO') as failed:
                        await service.reconcile()
                    self.assertIn(type(error).__name__, failed.output[-1])
                    self.assertEqual((await self.db.get(1))['sheet_exported'], 0)
                    with self.assertLogs('bot.services.sheets', level='INFO') as retried:
                        await service.reconcile()
                    self.assertEqual(retried.output, [
                        'INFO:bot.services.sheets:Повторная попытка экспорта записи 1 в Google Sheets',
                        'INFO:bot.services.sheets:Запись 1 успешно экспортирована в Google Sheets',
                    ])
                    self.assertEqual((await self.db.get(1))['sheet_exported'], 1)
                    await service.reconcile()
                exporter.worksheet.append_row.assert_called_once()
                self.assertEqual(exporter.worksheet.col_values.call_count, 2)

    async def test_reconcile_retries_cancelled_and_past_records_after_restart(self):
        settings = SimpleNamespace(timezone=TZ, sheet_id='test-sheet', credentials_path=Path('/not-read'))
        for status in ('active', 'cancelled_by_client', 'cancelled_by_admin'):
            with self.subTest(status=status):
                async with self.db.connect() as conn:
                    await conn.execute('UPDATE bookings SET sheet_exported=0,status=? WHERE id=1', (status,))
                    await conn.commit()
                exporter = SheetsExporter(settings, self.business, self.db)
                exporter.worksheet = Mock()
                exporter.worksheet.col_values.side_effect = RequestsConnectionError('offline')
                service = Reminders(self.bot, self.db, settings, self.business, exporter)
                with patch('bot.scheduler.utcnow', return_value=NOW), patch('bot.db.utcnow', return_value=NOW):
                    with self.assertLogs('bot.services.sheets', level='ERROR'):
                        await service.reconcile()
                self.assertEqual((await self.db.get(1))['sheet_exported'], 0)
                # Новый экземпляр имитирует перезапуск после начала визита.
                restarted = SheetsExporter(settings, self.business, self.db)
                restarted.worksheet = Mock()
                restarted.worksheet.col_values.return_value = ['ID записи']
                service = Reminders(self.bot, self.db, settings, self.business, restarted)
                later = NOW + timedelta(days=3)
                with patch('bot.scheduler.utcnow', return_value=later), patch('bot.db.utcnow', return_value=later):
                    with self.assertLogs('bot.services.sheets', level='INFO') as success:
                        await service.reconcile()
                self.assertIn('Запись 1 успешно экспортирована', success.output[-1])
                self.assertEqual((await self.db.get(1))['sheet_exported'], 1)
                restarted.worksheet.append_row.assert_called_once()

    async def test_failed_export_does_not_block_other_records(self):
        row, _ = await self.db.book(business=self.business, timezone=TZ, client_id=2, name='Иван',
                                  phone='+79991234567', username=None, service_id='cut', master_id='one',
                                  start=NOW+timedelta(days=2, hours=4), request_token='second', now=NOW)
        settings = SimpleNamespace(timezone=TZ, sheet_id='test-sheet', credentials_path=Path('/not-read'))
        exporter = SheetsExporter(settings, self.business, self.db)
        exporter.worksheet = Mock()
        exporter.worksheet.col_values.side_effect = [RequestsConnectionError('offline'), ['ID записи']]
        service = Reminders(self.bot, self.db, settings, self.business, exporter)
        with patch('bot.scheduler.utcnow', return_value=NOW), patch('bot.db.utcnow', return_value=NOW):
            with self.assertLogs('bot.services.sheets', level='INFO') as logs:
                await service.reconcile()
        self.assertEqual((await self.db.get(1))['sheet_exported'], 0)
        self.assertEqual((await self.db.get(row['id']))['sheet_exported'], 1)
        self.assertIn(f'Запись {row["id"]} успешно экспортирована', logs.output[-1])
