import logging

from bot.db import moment

logger = logging.getLogger(__name__)


def details(booking, business, timezone, *, private=False):
    service = business.services.get(booking['service_id'])
    master = business.masters.get(booking['master_id'])
    when = moment(booking['start_utc']).astimezone(timezone)
    result = (f"Запись №{booking['id']}\n"
              f"{service.name if service else booking['service_id']}\n"
              f"Мастер: {master.name if master else booking['master_id']}\n"
              f"{when:%d.%m.%Y в %H:%M} ({timezone.key})")
    if service:
        result += f'\nСтоимость: {service.price:g} ₽'
    if private:
        result += f"\nКлиент: {booking['name']}\nТелефон: {booking['phone']}"
        if booking.get('username'):
            result += f"\n@{booking['username']}"
    return result


async def safe_send(bot, chat_id, text, reply_markup=None):
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return True
    except Exception as exc:
        # Не выводим текст Telegram-исключения: в нём может оказаться телефон клиента.
        logger.error('Не удалось отправить сообщение: chat_id=%s, ошибка=%s', chat_id, type(exc).__name__)
        return False
