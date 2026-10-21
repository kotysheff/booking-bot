from datetime import datetime, time, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.db import moment, utcnow
from bot.keyboards import cancel_button
from bot.services.notify import details, safe_send

router = Router()


@router.message(Command('today', 'tomorrow', 'week'))
async def schedule(message: Message, settings, business, db):
    if message.chat.id != settings.admin_chat_id:
        return
    command = message.text.split()[0].split('@')[0][1:]
    today = utcnow().astimezone(settings.timezone).date()
    day = today + timedelta(days=1 if command == 'tomorrow' else 0)
    begin = datetime.combine(day, time.min, settings.timezone)
    end = datetime.combine(day + timedelta(days=7 if command == 'week' else 1), time.min, settings.timezone)
    rows = await db.bookings(start=begin, end=end)
    if command == 'week':
        counts = {day + timedelta(days=i): 0 for i in range(7)}
        for row in rows:
            counts[moment(row['start_utc']).astimezone(settings.timezone).date()] += 1
        await message.answer('Записи на 7 дней:\n' + '\n'.join(f'{d:%d.%m}: {n}' for d, n in counts.items()))
        return
    await message.answer(f'Расписание на {day:%d.%m.%Y} ({settings.timezone.key})')
    masters = dict.fromkeys(list(business.masters) + [r['master_id'] for r in rows])
    for master_id in masters:
        master = business.masters.get(master_id)
        selected = [row for row in rows if row['master_id'] == master_id]
        await message.answer(f'{master.name if master else master_id}: {len(selected)} записей')
        for row in selected:
            await message.answer(details(row, business, settings.timezone, private=True), reply_markup=cancel_button(row['id'], admin=True))


@router.callback_query(F.data.startswith('admin_cancel:'))
async def cancel(query: CallbackQuery, settings, business, db, reminders):
    if not query.message or query.message.chat.id != settings.admin_chat_id:
        await query.answer('Недоступно')
        return
    value = query.data.split(':')[-1]
    if not value.isdigit():
        await query.answer('Неверная кнопка')
        return
    booking_id = int(value)
    result = await reminders.cancel(booking_id, settings.admin_chat_id, admin=True)
    if result != 'cancelled':
        await query.answer('Запись уже отменена, началась или не найдена.', show_alert=True)
        return
    await query.answer('Запись отменена')
    row = await db.get(booking_id)
    await safe_send(query.bot, row['client_id'], 'Владелец отменил запись.\n' + details(row, business, settings.timezone))
    await query.message.answer(f'Запись №{booking_id} отменена.')
