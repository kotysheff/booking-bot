import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from bot.config import ConfigError, ROOT, load_business, load_settings
from bot.db import Database
from bot.handlers import admin, client, errors
from bot.scheduler import Reminders
from bot.services.sheets import SheetsExporter


def setup_logging():
    directory = ROOT / 'logs'
    directory.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        handlers=[logging.StreamHandler(), RotatingFileHandler(
                            directory / 'bot.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')])


async def run():
    setup_logging()
    try:
        business = load_business()
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f'Ошибка настроек: {exc}') from None
    db = Database(settings.db_path)
    await db.init()
    bot = Bot(settings.token)
    exporter = SheetsExporter(settings, business, db)
    reminders = Reminders(bot, db, settings, business, exporter)
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.include_routers(errors.router, admin.router, client.router)
    try:
        base = [BotCommand(command='start', description='Главное меню'), BotCommand(command='cancel', description='Сбросить текущий шаг')]
        await bot.set_my_commands(base, scope=BotCommandScopeDefault())
        await bot.set_my_commands(base + [BotCommand(command='today', description='Расписание на сегодня'),
                                          BotCommand(command='tomorrow', description='Расписание на завтра'),
                                          BotCommand(command='week', description='Записи на неделю')],
                                  scope=BotCommandScopeChat(chat_id=settings.admin_chat_id))
        await reminders.start()
        await dispatcher.start_polling(bot, business=business, settings=settings, db=db, reminders=reminders, exporter=exporter)
    finally:
        reminders.stop()
        await dispatcher.storage.close()
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(run())
