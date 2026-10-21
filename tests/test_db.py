import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from bot.db import Database, SlotTaken
from tests.helpers import NOW, TZ, business


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'test.db')
        await self.db.init()
        self.business = business()
        self.args = dict(business=self.business, timezone=TZ, client_id=1, name='Гоша', phone='+79991234567',
                         username=None, service_id='cut', master_id='one', start=NOW+timedelta(hours=3), now=NOW)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def book(self, token='request', **overrides):
        return await self.db.book(**(self.args | overrides), request_token=token)

    async def test_automatic_schema_and_repeat_init(self):
        await self.db.init()
        row, created = await self.book()
        self.assertTrue(created)
        self.assertEqual((await self.db.get(row['id']))['name'], 'Гоша')

    async def test_concurrent_clients_cannot_double_book(self):
        results = await asyncio.gather(*(self.book(str(i), client_id=i+1) for i in range(10)), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, tuple) for r in results), 1)
        self.assertEqual(sum(isinstance(r, SlotTaken) for r in results), 9)
        self.assertEqual(len(await self.db.bookings()), 1)
        self.assertIsNone(await self.db.client(999))

    async def test_repeated_confirm_is_idempotent_under_concurrency(self):
        results = await asyncio.gather(*(self.book() for _ in range(6)))
        self.assertEqual(len({r[0]['id'] for r in results}), 1)
        self.assertEqual(sum(r[1] for r in results), 1)
        self.assertEqual(len(await self.db.bookings()), 1)

    async def test_partial_overlap_rejected_and_touching_allowed(self):
        await self.book()
        with self.assertRaises(SlotTaken):
            await self.book('overlap', start=NOW+timedelta(hours=3, minutes=30))
        await self.book('adjacent', start=NOW+timedelta(hours=4))
        self.assertEqual(len(await self.db.bookings()), 2)

    async def test_invalid_slot_does_not_save_client(self):
        with self.assertRaises(SlotTaken):
            await self.book(start=NOW+timedelta(minutes=30))
        self.assertIsNone(await self.db.client(1))

    async def test_cancel_owner_deadline_and_repetition(self):
        row, _ = await self.book()
        self.assertEqual(await self.db.cancel(row['id'], 2, 180, now=NOW), 'missing')
        self.assertEqual(await self.db.cancel(row['id'], 1, 180, now=NOW), 'late')
        self.assertEqual(await self.db.cancel(row['id'], 1, 180, now=NOW-timedelta(seconds=1)), 'cancelled')
        self.assertEqual(await self.db.cancel(row['id'], 1, 180, now=NOW), 'already')
        self.assertEqual(await self.db.bookings(), [])
        self.assertEqual((await self.db.get(row['id']))['status'], 'cancelled_by_client')

    async def test_admin_cancellation_releases_slot(self):
        row, _ = await self.book()
        self.assertEqual(await self.db.cancel(row['id'], 100, 180, admin=True, now=NOW), 'cancelled')
        await self.book('new-client', client_id=2)
        self.assertEqual((await self.db.get(row['id']))['status'], 'cancelled_by_admin')

    async def test_first_booking_date_preserved_and_profile_updated(self):
        await self.book()
        first = await self.db.client(1)
        await self.book('next', start=NOW+timedelta(days=1, hours=3), now=NOW+timedelta(minutes=1), name='Иван')
        profile = await self.db.client(1)
        self.assertEqual(profile['first_booking_at'], first['first_booking_at'])
        self.assertEqual(profile['name'], 'Иван')

    async def test_request_token_cannot_be_reused_by_another_client(self):
        await self.book()
        with self.assertRaises(ValueError):
            await self.book(client_id=2)
