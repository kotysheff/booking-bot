"""Проверка реальных роутеров aiogram без сети и Telegram-токена."""
import importlib
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from bot.db import Database
from bot.handlers import admin, client, errors
from bot.scheduler import Reminders
from tests.helpers import NOW, TZ, business


class OfflineSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.answers = []
        self.fail_admin = False

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            if self.fail_admin and method.chat_id == 900:
                raise RuntimeError('offline delivery failed')
            self.sent.append(method)
            return Message(message_id=len(self.sent), date=NOW,
                           chat=Chat(id=int(method.chat_id), type='private'), text=method.text)
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method)
        return True

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name)/'bot.db')
        await self.db.init()
        self.business = business()
        self.settings = SimpleNamespace(admin_chat_id=900, timezone=TZ, sheet_id='')
        self.session = OfflineSession()
        self.bot = Bot('123456:' + 'x'*30, session=self.session)
        self.exporter = SimpleNamespace(export=AsyncMock())
        self.reminders = Reminders(self.bot, self.db, self.settings, self.business, self.exporter)
        for module in (client, admin, errors):
            importlib.reload(module)
        self.dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        self.dp.include_routers(errors.router, admin.router, client.router)
        self.data = dict(settings=self.settings, business=self.business, db=self.db,
                         reminders=self.reminders, exporter=self.exporter)
        self.sequence = 0
        self.patches = [patch('bot.handlers.client.utcnow', return_value=NOW),
                        patch('bot.db.utcnow', return_value=NOW), patch('bot.scheduler.utcnow', return_value=NOW),
                        patch('bot.handlers.admin.utcnow', return_value=NOW)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        self.reminders.stop()
        await self.dp.storage.close()
        await self.bot.session.close()
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    async def message(self, text=None, actor=1, **extra):
        self.sequence += 1
        entities = [MessageEntity(type='bot_command', offset=0, length=len(text.split()[0]))] if text and text.startswith('/') else None
        message = Message(message_id=self.sequence, date=NOW, chat=Chat(id=actor, type='private'),
                          from_user=User(id=actor, is_bot=False, first_name='Гоша'), text=text, entities=entities, **extra)
        await self.dp.feed_update(self.bot, Update(update_id=self.sequence, message=message), **self.data)

    async def click(self, value, actor=1):
        self.sequence += 1
        query = CallbackQuery(id=str(self.sequence), from_user=User(id=actor, is_bot=False, first_name='Гоша'),
                              chat_instance='test', data=value,
                              message=Message(message_id=self.sequence, date=NOW, chat=Chat(id=actor, type='private')))
        await self.dp.feed_update(self.bot, Update(update_id=self.sequence, callback_query=query), **self.data)

    def button(self, prefix):
        for method in reversed(self.session.sent):
            markup = method.reply_markup
            if hasattr(markup, 'inline_keyboard'):
                for row in markup.inline_keyboard:
                    for btn in row:
                        if btn.callback_data.startswith(prefix):
                            return btn.callback_data
        self.fail(f'Кнопка {prefix} не найдена')

    async def ready_to_confirm(self, actor=1):
        await self.click('book', actor)
        await self.click(self.button('service:'), actor)
        await self.click(self.button('day:'), actor)
        await self.click(self.button('time:'), actor)
        await self.message('Гоша', actor)
        await self.message('89991234567', actor)
        return self.button('confirm:')

    async def test_full_booking_repeated_confirm_and_mine(self):
        await self.message('/start')
        self.assertIn('Привет', self.session.sent[0].text)
        confirm = await self.ready_to_confirm()
        await self.click(confirm)
        await self.click(confirm)
        self.assertEqual(len(await self.db.bookings()), 1)
        self.assertEqual(sum('Новая запись!' in m.text for m in self.session.sent), 1)
        self.assertIn('устарела', self.session.answers[-1].text)
        await self.click('mine')
        self.assertTrue(any('Запись №1' in m.text and m.chat_id == 1 for m in self.session.sent))
        await self.click('cancel_booking:1', actor=2)
        self.assertEqual((await self.db.get(1))['status'], 'active')

    async def test_owner_notification_failure_keeps_booking(self):
        self.session.fail_admin = True
        await self.click(await self.ready_to_confirm())
        self.assertEqual(len(await self.db.bookings()), 1)
        self.assertTrue(any('Спасибо' in m.text for m in self.session.sent))

    async def test_competing_booking_returns_to_same_day(self):
        confirm = await self.ready_to_confirm()
        await self.db.book(business=self.business, timezone=TZ, client_id=2, name='Иван', phone='+79991234567',
                           username=None, service_id='cut', master_id='one', start=NOW+timedelta(hours=2),
                           request_token='competitor', now=NOW)
        await self.click(confirm)
        self.assertEqual(len(await self.db.bookings()), 1)
        self.assertTrue(any('уже заняли' in m.text for m in self.session.sent))
        await self.click(self.button('time:'))
        await self.message('Гоша')
        await self.message('79991234567')
        await self.click(self.button('confirm:'))
        self.assertEqual(len(await self.db.bookings()), 2)

    async def test_reset_and_stale_buttons(self):
        await self.click('book')
        old = self.button('service:')
        await self.message('/cancel')
        await self.click(old)
        self.assertIn('устарела', self.session.answers[-1].text)
        await self.click('book')
        await self.click(old)
        self.assertIn('устарела', self.session.answers[-1].text)

    async def test_admin_access_and_cancellation(self):
        await self.click(await self.ready_to_confirm())
        count = len(self.session.sent)
        await self.message('/today', actor=1)
        self.assertEqual(len(self.session.sent), count)
        await self.message('/today', actor=900)
        self.assertTrue(any('Телефон:' in m.text and m.chat_id == 900 for m in self.session.sent))
        await self.click('admin_cancel:1', actor=1)
        self.assertEqual((await self.db.get(1))['status'], 'active')
        await self.click('admin_cancel:1', actor=900)
        self.assertEqual((await self.db.get(1))['status'], 'cancelled_by_admin')
        self.assertTrue(any('Владелец отменил' in m.text and m.chat_id == 1 for m in self.session.sent))
        await self.message('/week', actor=900)
        self.assertIn('Записи на 7 дней', self.session.sent[-1].text)

    async def test_input_guidance(self):
        await self.click('book')
        await self.click(self.button('service:'))
        await self.click(self.button('day:'))
        await self.click(self.button('time:'))
        await self.message()
        self.assertIn('имя текстом', self.session.sent[-1].text)
        await self.message('Иван')
        await self.message('bad')
        self.assertIn('телефон', self.session.sent[-1].text)
        await self.message('79991234567')
        self.assertTrue(self.button('confirm:'))

    async def test_any_master_and_service_filter(self):
        from dataclasses import replace
        first = self.business.masters['one']
        self.business = replace(self.business, masters={'one': first, 'two': replace(first, id='two', name='Пётр')})
        self.data['business'] = self.business
        await self.click('book')
        await self.click(self.button('service:'))
        self.assertTrue(self.button('master:').endswith(':one'))
        session = self.button('master:').split(':')[1]
        await self.click(f'master:{session}:any')
        await self.click(self.button('day:'))
        await self.click(self.button('time:'))
        await self.message('Гоша')
        await self.message('79991234567')
        await self.click(self.button('confirm:'))
        self.assertEqual(len(await self.db.bookings()), 1)

    async def test_global_error_handler_recovers(self):
        await self.click('book')
        with patch.object(self.db, 'busy', side_effect=RuntimeError('test failure')):
            with self.assertLogs('bot.handlers.errors', level='ERROR'):
                await self.click(self.button('service:'))
        self.assertIn('Что-то пошло не так', self.session.sent[-1].text)
        await self.message('/start')
        self.assertEqual(self.session.sent[-1].text, 'Что хочешь сделать?')

    async def test_old_time_button_rejected_after_slot_conflict(self):
        await self.click('book')
        await self.click(self.button('service:'))
        await self.click(self.button('day:'))
        old_time = self.button('time:')
        await self.click(old_time)
        await self.message('Гоша')
        await self.message('79991234567')
        confirm = self.button('confirm:')
        await self.db.book(business=self.business, timezone=TZ, client_id=2, name='Иван', phone='+79991234567',
                           username=None, service_id='cut', master_id='one', start=NOW+timedelta(hours=2),
                           request_token='competitor', now=NOW)
        await self.click(confirm)
        await self.click(old_time)
        self.assertIn('устарела', self.session.answers[-1].text)
