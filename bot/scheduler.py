import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.db import moment, utcnow
from bot.keyboards import cancel_button
from bot.services.notify import details, safe_send
from bot.slots import reminder_times

logger = logging.getLogger(__name__)


class Reminders:
    def __init__(self, bot, db, settings, business, exporter):
        self.bot, self.db, self.settings, self.business = bot, db, settings, business
        self.exporter = exporter
        self.scheduler = AsyncIOScheduler(timezone='UTC')
        self.lock = asyncio.Lock()

    def schedule(self, row):
        now = utcnow()
        if row['status'] != 'active':
            return
        for hours, when in reminder_times(moment(row['start_utc']), moment(row['created_at'])).items():
            if not row[f'reminded_{hours}'] and when > now:
                self.scheduler.add_job(self.send, 'date', run_date=when, args=[row['id'], hours],
                                       id=f"reminder:{row['id']}:{hours}", replace_existing=True,
                                       misfire_grace_time=300)

    async def send(self, booking_id, hours):
        async with self.lock:
            row = await self.db.get(booking_id)
            if not row or row['status'] != 'active' or row[f'reminded_{hours}'] or moment(row['start_utc']) <= utcnow():
                return
            text = f'Напоминание о визите (за {hours} ч):\n' + details(row, self.business, self.settings.timezone)
            if await safe_send(self.bot, row['client_id'], text, cancel_button(booking_id)):
                await self.db.mark_reminded(booking_id, hours)

    async def cancel(self, booking_id, actor_id, *, admin=False):
        # Отмена и отправка напоминания не выполняются одновременно.
        async with self.lock:
            result = await self.db.cancel(booking_id, actor_id, self.business.cancel_deadline_minutes, admin=admin)
            if result == 'cancelled':
                for hours in (24, 2):
                    job = self.scheduler.get_job(f'reminder:{booking_id}:{hours}')
                    if job:
                        job.remove()
            return result

    async def reconcile(self):
        for row in await self.db.bookings(future=True):
            self.schedule(row)
            # Пропущенное во время остановки напоминание отправляем до начала визита.
            for hours, when in reminder_times(moment(row['start_utc']), moment(row['created_at'])).items():
                if when <= utcnow() and not row[f'reminded_{hours}']:
                    await self.send(row['id'], hours)
        if self.settings.sheet_id:
            # Ошибки экспорта повторяем и после начала визита или отмены записи.
            for row in await self.db.pending_exports():
                await self.exporter.export(row)

    async def start(self):
        self.scheduler.start(paused=True)
        await self.reconcile()
        self.scheduler.add_job(self.reconcile, 'interval', minutes=1, id='reconcile', max_instances=1, coalesce=True)
        self.scheduler.resume()

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
