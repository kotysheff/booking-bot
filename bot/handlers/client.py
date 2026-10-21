from datetime import date, timedelta
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from bot.db import SlotTaken, moment, utcnow
from bot.keyboards import cancel_button, choices, contact, inline, menu
from bot.services.notify import details, safe_send
from bot.slots import available_slots, can_cancel
from bot.states import Booking
from bot.validation import normalize_name, normalize_phone

router = Router()
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, business):
    await state.clear()
    await message.answer(business.texts['welcome'], reply_markup=ReplyKeyboardRemove())
    await message.answer('Что хочешь сделать?', reply_markup=menu())


@router.message(Command('cancel'))
async def reset(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Текущий шаг сброшен.', reply_markup=ReplyKeyboardRemove())
    await message.answer('Главное меню', reply_markup=menu())


async def begin(message, state, business):
    await state.clear()
    session = uuid4().hex[:8]
    await state.set_state(Booking.service)
    await state.update_data(session=session)
    await message.answer('Выбери услугу:', reply_markup=choices([
        (f'{s.name} · {s.duration_minutes} мин · {s.price:g} ₽', s.id)
        for s in business.services.values()], 'service', session))


@router.callback_query(F.data == 'book')
async def book_menu(query: CallbackQuery, state: FSMContext, business):
    await query.answer()
    await query.message.answer('Начинаем запись.', reply_markup=ReplyKeyboardRemove())
    await begin(query.message, state, business)


async def show_days(message, state, business, settings, db):
    await state.update_data(session=uuid4().hex[:8])
    data = await state.get_data()
    now = utcnow()
    busy = await db.busy()
    today = now.astimezone(settings.timezone).date()
    days = []
    for i in range(business.booking_horizon_days):
        day = today + timedelta(days=i)
        if available_slots(business, data['service_id'], day, settings.timezone, now, busy, data['master_id']):
            days.append((f'{day:%d.%m}', day.isoformat()))
    await state.set_state(Booking.day)
    if days:
        await message.answer('Выбери дату:', reply_markup=choices(days, 'day', data['session'], 3))
    else:
        await message.answer('В ближайшие дни свободного времени нет. Попробуй другую услугу или мастера.',
                             reply_markup=menu())


async def show_times(message, state, business, settings, db):
    await state.update_data(session=uuid4().hex[:8])
    data = await state.get_data()
    slots = available_slots(business, data['service_id'], date.fromisoformat(data['day']), settings.timezone,
                            utcnow(), await db.busy(), data['master_id'])
    # При «Любой свободный» одинаковое время показываем один раз, мастера выберем при подтверждении.
    unique = sorted({s.start for s in slots})
    await state.set_state(Booking.time)
    await state.update_data(times=[s.isoformat() for s in unique])
    if unique:
        await message.answer('Выбери время:', reply_markup=choices(
            [(s.astimezone(settings.timezone).strftime('%H:%M'), str(i)) for i, s in enumerate(unique)],
            'time', data['session'], 3))
    else:
        await message.answer('На эту дату свободного времени уже нет. Выбери другую дату.')
        await show_days(message, state, business, settings, db)


async def ask_phone(message, state):
    await state.set_state(Booking.phone)
    await message.answer('Отправь свой контакт кнопкой или введи телефон, например +79991234567.', reply_markup=contact())


async def summary(message, state, business, settings):
    data = await state.get_data()
    service = business.services[data['service_id']]
    master = business.masters[data['master_id']].name if data['master_id'] else 'Любой свободный'
    when = moment(data['start']).astimezone(settings.timezone)
    await state.set_state(Booking.confirm)
    await message.answer('Проверь данные:', reply_markup=ReplyKeyboardRemove())
    await message.answer(
        f'{service.name} · {service.duration_minutes} мин · {service.price:g} ₽\n'
        f'Мастер: {master}\n{when:%d.%m.%Y в %H:%M} ({settings.timezone.key})\n'
        f"Имя: {data['name']}\nТелефон: {data['phone']}\n\n{business.texts['cancel_rules']}",
        reply_markup=inline([[('Подтвердить', f"confirm:{data['session']}:yes")], [('Начать заново', 'book')]]))


EXPECTED = {'service': Booking.service, 'master': Booking.master, 'day': Booking.day,
            'time': Booking.time, 'old_name': Booking.name, 'confirm': Booking.confirm}


@router.callback_query(F.data.regexp(r'^(service|master|day|time|old_name|confirm):'))
async def wizard(query: CallbackQuery, state: FSMContext, business, settings, db, reminders):
    parts = query.data.split(':', 2)
    data = await state.get_data()
    if (len(parts) != 3 or parts[1] != data.get('session')
            or await state.get_state() != EXPECTED[parts[0]].state):
        await query.answer('Эта кнопка устарела. Используй последнее сообщение или /start.', show_alert=True)
        return
    action, _, value = parts
    await query.answer()
    message = query.message
    if action == 'service':
        if value not in business.services:
            return
        await state.update_data(service_id=value)
        masters = [m for m in business.masters.values() if value in m.services]
        if len(masters) == 1:
            await state.update_data(master_id=masters[0].id)
            await show_days(message, state, business, settings, db)
        else:
            await state.set_state(Booking.master)
            await message.answer('Выбери мастера:', reply_markup=choices(
                [(m.name, m.id) for m in masters] + [('Любой свободный', 'any')], 'master', data['session']))
    elif action == 'master':
        if value != 'any' and (value not in business.masters or data['service_id'] not in business.masters[value].services):
            return
        await state.update_data(master_id=None if value == 'any' else value)
        await show_days(message, state, business, settings, db)
    elif action == 'day':
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return
        await state.update_data(day=day.isoformat())
        await show_times(message, state, business, settings, db)
    elif action == 'time':
        if not value.isdigit() or int(value) >= len(data.get('times', [])):
            return
        await state.update_data(start=data['times'][int(value)], request_token=uuid4().hex)
        await state.set_state(Booking.name)
        previous = await db.client(query.from_user.id)
        if previous:
            await state.update_data(previous_name=previous['name'])
        await message.answer('Как тебя зовут? Введи имя.' + (' Можно использовать прошлое:' if previous else ''),
                             reply_markup=choices([(previous['name'], 'yes')], 'old_name', data['session']) if previous else None)
    elif action == 'old_name':
        if data.get('previous_name'):
            await state.update_data(name=data['previous_name'])
            await ask_phone(message, state)
    elif action == 'confirm':
        # Для любого мастера повторно смотрим всех свободных на это время.
        slots = available_slots(business, data['service_id'], date.fromisoformat(data['day']), settings.timezone,
                                utcnow(), await db.busy(), data['master_id'])
        candidates = [s for s in slots if s.start == moment(data['start'])]
        booking = None
        for slot in candidates:
            try:
                booking, created = await db.book(
                    business=business, timezone=settings.timezone, client_id=query.from_user.id,
                    name=data['name'], phone=data['phone'], username=query.from_user.username,
                    service_id=data['service_id'], master_id=slot.master_id,
                    start=slot.start, request_token=data['request_token'])
                break
            except SlotTaken:
                continue
        if booking is None:
            await message.answer('Извини, это время уже заняли или оно стало недоступно. Выбери другое:')
            await show_times(message, state, business, settings, db)
            return
        await state.clear()
        # Сначала фиксируем задачи и уведомляем владельца; ошибки отправки не откатывают запись.
        reminders.schedule(booking)
        if created:
            await safe_send(query.bot, settings.admin_chat_id,
                            'Новая запись!\n' + details(booking, business, settings.timezone, private=True))
        await message.answer(business.texts['thanks'] + '\n\n' + details(booking, business, settings.timezone),
                             reply_markup=cancel_button(booking['id']))


@router.message(Booking.name)
async def receive_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer('Пришли имя текстом (от 2 до 80 символов).')
        return
    try:
        name = normalize_name(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(name=name)
    await ask_phone(message, state)


@router.message(Booking.phone)
async def receive_phone(message: Message, state: FSMContext, business, settings):
    if message.contact:
        if message.contact.user_id != message.from_user.id:
            await message.answer('Нужен твой контакт. Используй кнопку или введи свой номер вручную.')
            return
        raw = message.contact.phone_number
    elif message.text:
        raw = message.text
    else:
        await message.answer('Отправь контакт кнопкой или телефон текстом.')
        return
    try:
        phone = normalize_phone(raw)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(phone=phone)
    await summary(message, state, business, settings)


@router.callback_query(F.data == 'mine')
async def mine(query: CallbackQuery, state: FSMContext, db, business, settings):
    await query.answer()
    await state.clear()
    rows = await db.bookings(client_id=query.from_user.id, future=True)
    await query.message.answer('Твои будущие записи:' if rows else 'Будущих записей нет.', reply_markup=ReplyKeyboardRemove())
    for row in rows:
        allowed = can_cancel(moment(row['start_utc']), utcnow(), business.cancel_deadline_minutes)
        await query.message.answer(details(row, business, settings.timezone) + ('' if allowed else '\nОтмена только по телефону'),
                                   reply_markup=cancel_button(row['id']) if allowed else None)
    await query.message.answer('Главное меню', reply_markup=menu())


@router.callback_query(F.data.startswith('cancel_booking:'))
async def cancel_booking(query: CallbackQuery, db, business, settings, reminders):
    value = query.data.split(':')[-1]
    if not value.isdigit():
        await query.answer('Неверная кнопка')
        return
    booking_id = int(value)
    result = await reminders.cancel(booking_id, query.from_user.id)
    messages = {'missing': 'Запись не найдена.', 'already': 'Запись уже отменена.',
                'past': 'Визит уже начался.', 'late': 'Отмена только по телефону.', 'cancelled': 'Запись отменена.'}
    await query.answer(messages[result], show_alert=True)
    if result == 'cancelled':
        row = await db.get(booking_id)
        await safe_send(query.bot, settings.admin_chat_id,
                        'Клиент отменил запись:\n' + details(row, business, settings.timezone, private=True))
        await query.message.answer('Запись отменена.', reply_markup=menu())


@router.message(StateFilter('*'))
async def unexpected(message: Message, state: FSMContext):
    if await state.get_state():
        await message.answer('Используй кнопки последнего шага. /cancel — начать сначала.')
    else:
        await message.answer('Выбери действие:', reply_markup=menu())


@router.callback_query()
async def stale(query: CallbackQuery):
    await query.answer('Эта кнопка устарела. Открой /start.', show_alert=True)
