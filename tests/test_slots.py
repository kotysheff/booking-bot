import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from bot.slots import Busy, available_slots, can_cancel, reminder_times
from tests.helpers import NOW, TZ, business


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.business = business()
        self.day = date(2030, 1, 7)

    def slots(self, **kw):
        return available_slots(kw.pop('config', self.business), 'cut', kw.pop('day', self.day),
                               kw.pop('tz', TZ), kw.pop('now', NOW), **kw)

    def test_working_hours_and_grid(self):
        times = [s.start.astimezone(TZ).strftime('%H:%M') for s in self.slots()]
        self.assertEqual(times, ['10:00', '10:30', '11:00', '11:30', '12:00',
                                 '14:00', '14:30', '15:00', '15:30', '16:00', '16:30', '17:00'])
        self.assertTrue(all(s.start.tzinfo == UTC for s in self.slots()))

    def test_service_cannot_cross_break_or_close(self):
        config = replace(self.business, services={'cut': replace(self.business.services['cut'], duration_minutes=90)})
        times = [s.start.astimezone(TZ).strftime('%H:%M') for s in self.slots(config=config)]
        self.assertNotIn('12:00', times)
        self.assertNotIn('17:00', times)
        self.assertIn('16:30', times)

    def test_weekend(self):
        self.assertEqual(self.slots(day=date(2030, 1, 12)), [])

    def test_days_off(self):
        self.assertEqual(self.slots(config=replace(self.business, days_off=frozenset([self.day]))), [])

    def test_lead_time_boundary(self):
        slots = self.slots(now=NOW + timedelta(minutes=1))
        self.assertEqual(slots[0].start.astimezone(TZ).hour, 10)
        self.assertEqual(slots[0].start.astimezone(TZ).minute, 30)
        self.assertEqual(self.slots()[0].start, NOW + timedelta(hours=2))

    def test_horizon_includes_today_and_excludes_fourteenth_day(self):
        self.assertTrue(self.slots(day=self.day + timedelta(days=11)))
        self.assertEqual(self.slots(day=self.day + timedelta(days=14)), [])
        self.assertEqual(self.slots(day=self.day - timedelta(days=1)), [])

    def test_intersections_and_adjacent_bookings(self):
        start = NOW + timedelta(hours=3)
        result = self.slots(busy=[Busy('one', start, start + timedelta(hours=1))])
        times = [s.start for s in result]
        self.assertNotIn(start, times)
        self.assertNotIn(start - timedelta(minutes=30), times)
        self.assertIn(start - timedelta(hours=1), times)
        self.assertIn(start + timedelta(hours=1), times)

    def test_other_master_does_not_block(self):
        self.assertEqual(self.slots(), self.slots(busy=[Busy('other', NOW, NOW + timedelta(days=1))]))
        self.assertEqual(self.slots(master_id='missing'), [])

    def test_timezone_midnight(self):
        now = datetime(2030, 1, 6, 22, 0, tzinfo=UTC)
        self.assertTrue(self.slots(now=now))

    def test_nonexistent_dst_time_is_not_offered(self):
        config = business(masters=[{'id': 'one', 'name': 'Иван', 'services': ['cut'],
                                   'working_hours': {'sun': ['01:00', '05:00']}}], min_lead_minutes=0)
        tz = ZoneInfo('Europe/Berlin')
        rows = available_slots(config, 'cut', date(2030, 3, 31), tz, datetime(2030, 3, 30, tzinfo=UTC))
        self.assertTrue(rows)
        self.assertFalse(any(s.start.astimezone(tz).hour == 2 for s in rows))

    def test_cancel_strict_boundary(self):
        self.assertTrue(can_cancel(NOW + timedelta(minutes=181), NOW, 180))
        self.assertFalse(can_cancel(NOW + timedelta(minutes=180), NOW, 180))
        self.assertFalse(can_cancel(NOW - timedelta(minutes=1), NOW, 180))

    def test_reminders(self):
        start = NOW + timedelta(days=3)
        self.assertEqual(reminder_times(start, NOW), {24: start-timedelta(hours=24), 2: start-timedelta(hours=2)})

    def test_less_than_24_hours(self):
        start = NOW + timedelta(hours=23)
        self.assertEqual(reminder_times(start, NOW), {2: start-timedelta(hours=2)})
        self.assertEqual(reminder_times(NOW + timedelta(minutes=119), NOW), {})
        self.assertIn(24, reminder_times(NOW + timedelta(hours=24), NOW))
