import logging
import traceback

from aiogram import Router
from aiogram.types import ErrorEvent

from bot.services.notify import safe_send

router = Router()
logger = logging.getLogger(__name__)


@router.error()
async def error(event: ErrorEvent, bot):
    # Тип и стек без значений сообщений, токенов и контактов.
    logger.error('Ошибка %s\n%s', type(event.exception).__name__, ''.join(traceback.format_tb(event.exception.__traceback__)))
    query = event.update.callback_query
    message = event.update.message or (query.message if query else None)
    if query:
        try:
            await query.answer('Что-то пошло не так', show_alert=True)
        except Exception:
            pass
    if message:
        await safe_send(bot, message.chat.id, 'Что-то пошло не так. Попробуй /start.')
    return True
