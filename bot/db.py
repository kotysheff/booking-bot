"""SQLite: отдельное соединение на операцию и атомарное бронирование."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from bot.slots import Busy, available_slots, can_cancel


def utcnow():
    return datetime.now(UTC)


def stamp(value):
    if value.tzinfo is None:
        raise ValueError('Дата должна содержать часовой пояс')
    return value.astimezone(UTC).isoformat(timespec='microseconds')


def moment(value):
    return datetime.fromisoformat(value)


class SlotTaken(Exception):
    pass


class Database:
    def __init__(self, path):
        self.path = Path(path)

    @asynccontextmanager
    async def connect(self):
        async with aiosqlite.connect(self.path, timeout=30) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute('PRAGMA foreign_keys=ON')
            yield connection

    async def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as conn:
            await conn.execute('PRAGMA journal_mode=WAL')
            await conn.executescript('''
                CREATE TABLE IF NOT EXISTS clients (
                    telegram_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL, phone TEXT NOT NULL, username TEXT,
                    first_booking_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER NOT NULL REFERENCES clients(telegram_id),
                    service_id TEXT NOT NULL, master_id TEXT NOT NULL,
                    start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','cancelled_by_client','cancelled_by_admin')),
                    created_at TEXT NOT NULL, request_token TEXT NOT NULL UNIQUE,
                    reminded_24 INTEGER NOT NULL DEFAULT 0,
                    reminded_2 INTEGER NOT NULL DEFAULT 0,
                    sheet_exported INTEGER NOT NULL DEFAULT 0,
                    CHECK(start_utc < end_utc)
                );
                CREATE INDEX IF NOT EXISTS bookings_slots ON bookings(master_id,status,start_utc,end_utc);
                CREATE INDEX IF NOT EXISTS bookings_client ON bookings(client_id,status,start_utc);
                PRAGMA user_version=1;
            ''')
            await conn.commit()

    async def client(self, telegram_id):
        async with self.connect() as conn:
            row = await (await conn.execute('SELECT * FROM clients WHERE telegram_id=?', (telegram_id,))).fetchone()
            return dict(row) if row else None

    async def bookings(self, *, client_id=None, start=None, end=None, future=False):
        sql = "SELECT b.*,c.name,c.phone,c.username FROM bookings b JOIN clients c ON c.telegram_id=b.client_id WHERE b.status='active'"
        args = []
        for condition, value in [('b.client_id=?', client_id), ('b.start_utc>=?', stamp(start) if start else None),
                                 ('b.start_utc<?', stamp(end) if end else None)]:
            if value is not None:
                sql += ' AND ' + condition
                args.append(value)
        if future:
            sql += ' AND b.start_utc>?'
            args.append(stamp(utcnow()))
        async with self.connect() as conn:
            rows = await (await conn.execute(sql + ' ORDER BY b.start_utc,b.master_id', args)).fetchall()
            return [dict(row) for row in rows]

    async def get(self, booking_id):
        async with self.connect() as conn:
            row = await (await conn.execute('''SELECT b.*,c.name,c.phone,c.username FROM bookings b
                JOIN clients c ON c.telegram_id=b.client_id WHERE b.id=?''', (booking_id,))).fetchone()
            return dict(row) if row else None

    async def pending_exports(self):
        async with self.connect() as conn:
            rows = await (await conn.execute('''SELECT b.*,c.name,c.phone,c.username FROM bookings b
                JOIN clients c ON c.telegram_id=b.client_id
                WHERE b.sheet_exported=0 ORDER BY b.id''')).fetchall()
            return [dict(row) for row in rows]

    async def busy(self):
        return [Busy(row['master_id'], moment(row['start_utc']), moment(row['end_utc']))
                for row in await self.bookings()]

    async def book(self, *, business, timezone, client_id, name, phone, username,
                   service_id, master_id, start, request_token, now=None):
        async with self.connect() as conn:
            # Блокировка берётся ДО проверки пересечений: второй клиент ждёт завершения транзакции.
            await conn.execute('BEGIN IMMEDIATE')
            now = now or utcnow()
            row = await (await conn.execute('SELECT * FROM bookings WHERE request_token=?', (request_token,))).fetchone()
            if row:
                if row['client_id'] != client_id:
                    raise ValueError('Ключ подтверждения принадлежит другому клиенту')
                return dict(row), False
            rows = await (await conn.execute("SELECT * FROM bookings WHERE status='active' AND end_utc>?", (stamp(now),))).fetchall()
            busy = [Busy(r['master_id'], moment(r['start_utc']), moment(r['end_utc'])) for r in rows]
            slots = available_slots(business, service_id, start.astimezone(timezone).date(), timezone, now, busy, master_id)
            selected = next((s for s in slots if s.start == start), None)
            if selected is None:
                raise SlotTaken
            await conn.execute('''INSERT INTO clients VALUES (?,?,?,?,?) ON CONFLICT(telegram_id)
                DO UPDATE SET name=excluded.name,phone=excluded.phone,username=excluded.username''',
                               (client_id, name, phone, username, stamp(now)))
            cursor = await conn.execute('''INSERT INTO bookings
                (client_id,service_id,master_id,start_utc,end_utc,created_at,request_token)
                VALUES (?,?,?,?,?,?,?)''',
                (client_id, service_id, master_id, stamp(start), stamp(selected.end), stamp(now), request_token))
            booking_id = cursor.lastrowid
            await conn.commit()
        return await self.get(booking_id), True

    async def cancel(self, booking_id, actor_id, deadline_minutes, *, admin=False, now=None):
        now = now or utcnow()
        async with self.connect() as conn:
            await conn.execute('BEGIN IMMEDIATE')
            row = await (await conn.execute('SELECT * FROM bookings WHERE id=?', (booking_id,))).fetchone()
            if not row or (not admin and row['client_id'] != actor_id):
                return 'missing'
            if row['status'] != 'active':
                return 'already'
            if moment(row['start_utc']) <= now:
                return 'past'
            if not admin and not can_cancel(moment(row['start_utc']), now, deadline_minutes):
                return 'late'
            status = 'cancelled_by_admin' if admin else 'cancelled_by_client'
            await conn.execute('UPDATE bookings SET status=? WHERE id=?', (status, booking_id))
            await conn.commit()
            return 'cancelled'

    async def mark_reminded(self, booking_id, hours):
        if hours not in (2, 24):
            raise ValueError('Неверный тип напоминания')
        async with self.connect() as conn:
            await conn.execute(f'UPDATE bookings SET reminded_{hours}=1 WHERE id=?', (booking_id,))
            await conn.commit()

    async def mark_exported(self, booking_id):
        async with self.connect() as conn:
            await conn.execute('UPDATE bookings SET sheet_exported=1 WHERE id=?', (booking_id,))
            await conn.commit()
