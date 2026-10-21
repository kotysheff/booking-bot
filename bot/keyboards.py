from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows])


def menu():
    return inline([[('Записаться', 'book'), ('Мои записи', 'mine')]])


def cancel_button(booking_id, admin=False):
    return inline([[('Отменить запись', f'{"admin_cancel" if admin else "cancel_booking"}:{booking_id}')]])


def contact():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='Отправить контакт', request_contact=True)]],
                               resize_keyboard=True, one_time_keyboard=True)


def choices(items, prefix, session, columns=1):
    buttons = [(label, f'{prefix}:{session}:{value}') for label, value in items]
    return inline([buttons[i:i+columns] for i in range(0, len(buttons), columns)])
