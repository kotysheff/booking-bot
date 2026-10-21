import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import ConfigError, ROOT, load_business, load_settings, parse_business
from bot.validation import normalize_name, normalize_phone


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((ROOT / 'config/business.json').read_text())

    def test_example(self):
        config = load_business()
        self.assertEqual(config.name, 'Бритва')
        self.assertEqual(len(config.masters), 2)

    def test_defaults(self):
        for key in ('slot_step_minutes', 'booking_horizon_days', 'min_lead_minutes', 'cancel_deadline_minutes'):
            self.raw.pop(key)
        config = parse_business(self.raw)
        self.assertEqual((config.slot_step_minutes, config.booking_horizon_days, config.min_lead_minutes, config.cancel_deadline_minutes),
                         (30, 14, 120, 180))

    def test_invalid_configs(self):
        cases = [lambda r: r.update(slot_step_minutes=0), lambda r: r.update(booking_horizon_days=True),
                 lambda r: r.update(days_off=['bad']), lambda r: r.update(services=[]),
                 lambda r: r['masters'][0].update(services=['unknown']),
                 lambda r: r['masters'][0].update(working_hours={'bad': ['10:00', '20:00']}),
                 lambda r: r['masters'][0].update(working_hours={'mon': ['20:00', '10:00']}),
                 lambda r: r['masters'][0].update(breaks={'sun': [['10:00', '11:00']]}),
                 lambda r: r['masters'][0].update(breaks={'mon': [['09:00', '11:00']]}),
                 lambda r: r['masters'][0].update(breaks={'mon': [['11:00', '13:00'], ['12:00', '14:00']]}),
                 lambda r: r['services'].append(r['services'][0]),
                 lambda r: r['services'][0].update(price=float('nan')),
                 lambda r: r['services'][0].update(duration_minutes=-10)]
        for modify in cases:
            with self.subTest(modify=modify):
                raw = json.loads(json.dumps(self.raw))
                modify(raw)
                with self.assertRaises(ConfigError):
                    parse_business(raw)

    @patch('bot.config.load_dotenv')
    def test_env(self, _):
        with patch.dict(os.environ, {'BOT_TOKEN': '123456:'+'a'*30, 'ADMIN_CHAT_ID': '123'}, clear=True):
            config = load_settings()
            self.assertEqual(config.timezone.key, 'Europe/Moscow')
            self.assertEqual(config.db_path, ROOT / 'data/bot.db')
            self.assertIsNone(config.credentials_path)
            for changes in [{'BOT_TOKEN': ''}, {'ADMIN_CHAT_ID': 'no'}, {'TIMEZONE': 'bad'},
                            {'GOOGLE_SHEET_ID': 'sheet'}, {'GOOGLE_CREDENTIALS_PATH': 'key.json'},
                            {'GOOGLE_SHEET_ID': 'sheet', 'GOOGLE_CREDENTIALS_PATH': '/missing/key.json'}]:
                with self.subTest(changes=changes), patch.dict(os.environ, changes), self.assertRaises(ConfigError):
                    load_settings()


class ValidationTests(unittest.TestCase):
    def test_phone(self):
        for raw in ['89991234567', '79991234567', '8 (999) 123-45-67', '+7 (999) 123-45-67']:
            self.assertEqual(normalize_phone(raw), '+79991234567')
        self.assertEqual(normalize_phone('+1 202 555-01-23'), '+12025550123')

    def test_invalid_phones(self):
        for value in ['', '123', 'abc', '9991234567', '+00000000', '7'*41, '+7abc9991234567', '++79991234567']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_phone(value)

    def test_name(self):
        self.assertEqual(normalize_name('  Гоша   Иванов '), 'Гоша Иванов')
        for value in ['', 'A', '1'*81, '12345']:
            with self.assertRaises(ValueError):
                normalize_name(value)
