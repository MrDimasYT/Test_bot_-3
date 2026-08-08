import asyncio
import glob
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message
)
from dotenv import load_dotenv

# ================= НАСТРОЙКИ =================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env файле!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", "10"))
WORK_END_HOUR = int(os.getenv("WORK_END_HOUR", "20"))
BOOKING_DAYS_AHEAD = int(os.getenv("BOOKING_DAYS_AHEAD", "14"))
MAX_ACTIVE_BOOKINGS = int(os.getenv("MAX_ACTIVE_BOOKINGS", "1"))
PORTFOLIO_DIR = os.getenv("PORTFOLIO_DIR", "portfolio")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================= ДАННЫЕ =================
@dataclass
class Service:
    name: str
    description: str
    price: str
    duration: int

PRICES = [
    Service("💅 Маникюр", "Классический / аппаратный", "1500 ₽", 60),
    Service("🎨 Покрытие гель-лак", "Однотонное, дизайн по желанию", "1000 ₽", 45),
    Service("✨ Наращивание", "Гель, форма на выбор", "2500 ₽", 90),
    Service("🦶 Педикюр", "Аппаратный + покрытие", "2200 ₽", 80),
    Service("💎 Дизайн", "Френч, стразы, слайдеры", "от 200 ₽", 30),
    Service("🧴 Снятие + уход", "Снятие покрытия, масло, крем", "300 ₽", 20),
]

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ================= БАЗА ДАННЫХ =================
class Database:
    def __init__(self, db_name: str = "bot.db"):
        self.db_name = db_name
        self.init_db()
    
    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Ошибка БД: {e}")
            raise
        finally:
            conn.close()
    
    def init_db(self):
        with self.get_connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                day TEXT,
                time TEXT,
                name TEXT,
                phone TEXT,
                service TEXT DEFAULT '',
                comment TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                created_at TEXT,
                updated_at TEXT
            )""")
            
            conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                rating INTEGER,
                text TEXT,
                is_approved INTEGER DEFAULT 0,
                created_at TEXT
            )""")
            
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TEXT,
                last_activity TEXT
            )""")
            
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_day ON bookings(day)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status)")
    
    def get_active_booking_count(self, user_id: int) -> int:
        with self.get_connection() as conn:
            result = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND status = 'active' AND day >= date('now')",
                (user_id,)
            ).fetchone()
            return result[0] if result else 0
    
    def can_make_booking(self, user_id: int) -> tuple[bool, str]:
        active_count = self.get_active_booking_count(user_id)
        if active_count >= MAX_ACTIVE_BOOKINGS:
            return False, f"❌ У вас уже есть активная запись.\nМожно иметь только {MAX_ACTIVE_BOOKINGS} активную запись."
        return True, "✅ Можно создать запись"
    
    def save_booking(self, user_id: int, day: str, time: str, name: str, 
                     phone: str, service: str = "", comment: str = "") -> tuple[bool, str]:
        can_book, message = self.can_make_booking(user_id)
        if not can_book:
            return False, message
        
        with self.get_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM bookings WHERE day = ? AND time = ? AND status = 'active'",
                (day, time)
            ).fetchone()
            if existing:
                return False, "❌ Это время уже занято."
            
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""INSERT INTO bookings 
                (user_id, day, time, name, phone, service, comment, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (user_id, day, time, name, phone, service, comment, 'active', now, now))
            
            self.update_user_activity(user_id)
            return True, "✅ Запись создана!"
    
    def cancel_booking(self, booking_id: int, user_id: int) -> tuple[bool, str]:
        with self.get_connection() as conn:
            booking = conn.execute(
                "SELECT * FROM bookings WHERE id = ? AND user_id = ? AND status = 'active'",
                (booking_id, user_id)
            ).fetchone()
            if not booking:
                return False, "❌ Запись не найдена"
            
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE bookings SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, booking_id)
            )
            return True, "✅ Запись отменена"
    
    def get_user_bookings(self, user_id: int) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, day, time, name, phone, service, comment, created_at
                FROM bookings 
                WHERE user_id = ? AND status = 'active' AND day >= date('now')
                ORDER BY day, time
            """, (user_id,)).fetchall()
            return [dict(row) for row in rows]
    
    def get_all_bookings(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, user_id, day, time, name, phone, service, comment, created_at
                FROM bookings 
                WHERE status = 'active' AND day >= date('now')
                ORDER BY day, time
            """).fetchall()
            return [dict(row) for row in rows]
    
    def get_booked_slots(self, day: str) -> set:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT time FROM bookings WHERE day = ? AND status = 'active'",
                (day,)
            ).fetchall()
            return {row[0] for row in rows}
    
    def save_feedback(self, user_id: int, username: str, rating: int, text: str):
        with self.get_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""INSERT INTO feedback 
                (user_id, username, rating, text, is_approved, created_at)
                VALUES (?,?,?,?,0,?)""",
                (user_id, username, rating, text, now))
    
    def get_approved_feedback(self, limit: int = 10) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT username, rating, text, created_at
                FROM feedback 
                WHERE is_approved = 1
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]
    
    def get_unapproved_feedback(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, user_id, username, rating, text, created_at
                FROM feedback 
                WHERE is_approved = 0
                ORDER BY created_at DESC
            """).fetchall()
            return [dict(row) for row in rows]
    
    def approve_feedback(self, feedback_id: int):
        with self.get_connection() as conn:
            conn.execute("UPDATE feedback SET is_approved = 1 WHERE id = ?", (feedback_id,))
    
    def update_user_activity(self, user_id: int):
        with self.get_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT OR REPLACE INTO users (user_id, last_activity) 
                VALUES (?, ?)
            """, (user_id, now))

db = Database()

# ================= КЛАВИАТУРЫ =================
def menu_kb():
    buttons = [
        [InlineKeyboardButton(text="📅 Записаться", callback_data="book")],
        [InlineKeyboardButton(text="💰 Прайс-лист", callback_data="price")],
        [InlineKeyboardButton(text="📸 Примеры работ", callback_data="portfolio")],
        [InlineKeyboardButton(text="⭐ Отзывы", callback_data="reviews")],
        [InlineKeyboardButton(text="👩‍🎨 О мастере", callback_data="about")],
        [InlineKeyboardButton(text="💬 Оставить отзыв", callback_data="feedback")],
        [InlineKeyboardButton(text="📋 Мои записи", callback_data="my_bookings")],
    ]
    if ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]])

def days_kb():
    buttons = []
    today = date.today()
    for i in range(1, BOOKING_DAYS_AHEAD + 1):
        d = today + timedelta(days=i)
        if d == today and datetime.now().hour >= WORK_END_HOUR:
            continue
        label = f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"day_{d.isoformat()}")])
    buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def times_kb(day: str):
    buttons = []
    taken = db.get_booked_slots(day)
    for hour in range(WORK_START_HOUR, WORK_END_HOUR):
        t = f"{hour:02d}:00"
        if t not in taken:
            if day == date.today().isoformat() and hour <= datetime.now().hour:
                continue
            buttons.append([InlineKeyboardButton(text=t, callback_data=f"time_{day}_{t}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к датам", callback_data="book")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_feedback_kb(feedback_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"admin_approve_fb_{feedback_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_fb_{feedback_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_feedback")]
    ])

# ================= FSM =================
class BookingStates(StatesGroup):
    name = State()
    phone = State()
    service = State()
    comment = State()

class FeedbackStates(StatesGroup):
    rating = State()
    text = State()

# ================= ДЕКОРАТОРЫ =================
def admin_only(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        for arg in args:
            if isinstance(arg, Message) and arg.from_user.id != ADMIN_ID:
                await arg.answer("⛔ У вас нет прав администратора")
                return
            if isinstance(arg, CallbackQuery) and arg.from_user.id != ADMIN_ID:
                await arg.answer("⛔ У вас нет прав администратора", show_alert=True)
                return
        return await func(*args, **kwargs)
    return wrapper

# ================= ОСНОВНЫЕ КОМАНДЫ =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    db.update_user_activity(message.from_user.id)
    bookings = db.get_user_bookings(message.from_user.id)
    booking_status = ""
    if bookings:
        booking_status = f"\n📋 У вас есть активная запись:"
        for b in bookings:
            d = date.fromisoformat(b['day'])
            date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
            booking_status += f"\n   📅 {date_label} в {b['time']}"
    
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n"
        "💅 Добро пожаловать в салон красоты!\n\n"
        "Выберите действие в меню ниже:"
        f"{booking_status}",
        reply_markup=menu_kb()
    )

@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"🆔 Твой ID: {message.from_user.id}")

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено ✅", reply_markup=menu_kb())

@dp.message(Command("stats"))
@admin_only
async def cmd_stats(message: Message):
    bookings = db.get_all_bookings()
    if not bookings:
        await message.answer("📊 Ближайших записей нет")
        return
    
    text = "📊 СТАТИСТИКА ЗАПИСЕЙ\n\n"
    by_day = {}
    for b in bookings:
        day = b['day']
        if day not in by_day:
            by_day[day] = []
        by_day[day].append(b)
    
    for day, items in sorted(by_day.items()):
        d = date.fromisoformat(day)
        day_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"📅 {day_label} — {len(items)} записей\n"
        for item in items:
            text += f"   🕐 {item['time']} — {item['name']}"
            if item['phone']:
                text += f" 📞 {item['phone']}"
            text += "\n"
        text += "\n"
    
    await message.answer(text)

# ================= МЕНЮ =================
@dp.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("🏠 Главное меню:", reply_markup=menu_kb())
    except:
        await call.message.delete()
        await call.message.answer("🏠 Главное меню:", reply_markup=menu_kb())
    await call.answer()

@dp.callback_query(F.data == "price")
async def cb_price(call: CallbackQuery):
    text = "💰 ПРАЙС-ЛИСТ\n\n"
    for service in PRICES:
        text += f"{service.name}\n"
        text += f"   {service.description}\n"
        text += f"   💵 {service.price}\n"
        text += f"   ⏱ {service.duration} мин.\n\n"
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()

@dp.callback_query(F.data == "reviews")
async def cb_reviews(call: CallbackQuery):
    reviews = db.get_approved_feedback()
    if not reviews:
        text = "⭐ Отзывов пока нет. Будьте первыми!"
    else:
        text = "⭐ ОТЗЫВЫ КЛИЕНТОВ\n\n"
        for r in reviews:
            stars = "⭐" * r['rating'] + "☆" * (5 - r['rating'])
            text += f"{r['username'] or 'Аноним'} {stars}\n"
            text += f"📝 {r['text']}\n"
            text += f"📅 {r['created_at'][:10]}\n\n"
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()

@dp.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery):
    about = (
        "👩‍🎨 О МАСТЕРЕ\n\n"
        "Меня зовут Анна, я профессиональный мастер маникюра с 5-летним опытом.\n\n"
        "✨ Что я предлагаю:\n"
        "• Индивидуальный подход\n"
        "• Качественные материалы\n"
        "• Стерильные инструменты\n"
        "• Уютная атмосфера\n\n"
        "📍 Адрес: ул. Примерная, 15\n"
        "⏰ Режим работы: 10:00–20:00"
    )
    await call.message.edit_text(about, reply_markup=back_kb())
    await call.answer()

@dp.callback_query(F.data == "portfolio")
async def cb_portfolio(call: CallbackQuery):
    paths = sorted(glob.glob(os.path.join(PORTFOLIO_DIR, "*.jpg")) +
                   glob.glob(os.path.join(PORTFOLIO_DIR, "*.png")))
    if not paths:
        await call.message.edit_text(
            "📸 Фото работ пока нет.\n"
            "Положите фото в папку portfolio/",
            reply_markup=back_kb()
        )
        await call.answer()
        return
    
    try:
        media = [InputMediaPhoto(media=FSInputFile(p)) for p in paths[:10]]
        media[0].caption = "📸 Примеры работ"
        await call.message.delete()
        await call.message.answer_media_group(media)
        await call.message.answer("📸 Выберите действие:", reply_markup=menu_kb())
    except Exception as e:
        logger.error(f"Ошибка портфолио: {e}")
        await call.message.edit_text("❌ Ошибка загрузки фото", reply_markup=back_kb())
    await call.answer()

# ================= МОИ ЗАПИСИ =================
@dp.callback_query(F.data == "my_bookings")
async def cb_my_bookings(call: CallbackQuery):
    bookings = db.get_user_bookings(call.from_user.id)
    if not bookings:
        await call.message.edit_text("📋 У вас нет активных записей", reply_markup=back_kb())
        await call.answer()
        return
    
    text = "📋 ВАШИ ЗАПИСИ\n\n"
    kb_buttons = []
    for b in bookings:
        d = date.fromisoformat(b['day'])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"🆔 #{b['id']}\n"
        text += f"📅 {date_label} в {b['time']}\n"
        text += f"👤 {b['name']}\n"
        if b['phone']:
            text += f"📞 {b['phone']}\n"
        if b['service']:
            text += f"💅 {b['service']}\n"
        text += "\n"
        kb_buttons.append([
            InlineKeyboardButton(text=f"❌ Отменить #{b['id']}", callback_data=f"cancel_booking_{b['id']}")
        ])
    
    kb_buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))
    await call.answer()

@dp.callback_query(F.data.startswith("cancel_booking_"))
async def cb_cancel_booking(call: CallbackQuery):
    booking_id = int(call.data.split("_")[2])
    success, message = db.cancel_booking(booking_id, call.from_user.id)
    await call.message.edit_text(message, reply_markup=menu_kb() if success else back_kb())
    await call.answer()

# ================= ОТЗЫВЫ =================
@dp.callback_query(F.data == "feedback")
async def cb_feedback(call: CallbackQuery, state: FSMContext):
    await state.set_state(FeedbackStates.rating)
    rating_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ 1", callback_data="rating_1"),
            InlineKeyboardButton(text="⭐ 2", callback_data="rating_2"),
            InlineKeyboardButton(text="⭐ 3", callback_data="rating_3"),
            InlineKeyboardButton(text="⭐ 4", callback_data="rating_4"),
            InlineKeyboardButton(text="⭐ 5", callback_data="rating_5")
        ],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu")]
    ])
    await call.message.edit_text("⭐ Оцените работу от 1 до 5:", reply_markup=rating_kb)
    await call.answer()

@dp.callback_query(F.data.startswith("rating_"))
async def cb_rating(call: CallbackQuery, state: FSMContext):
    rating = int(call.data.split("_")[1])
    await state.update_data(rating=rating)
    await state.set_state(FeedbackStates.text)
    await call.message.edit_text(f"⭐ Ваша оценка: {rating}\n\n📝 Напишите отзыв:")
    await call.answer()

@dp.message(FeedbackStates.text)
async def fb_text(message: Message, state: FSMContext):
    data = await state.get_data()
    db.save_feedback(message.from_user.id, message.from_user.username or "", data['rating'], message.text)
    await state.clear()
    await message.answer("✅ Спасибо за отзыв! ❤️\nПосле модерации он появится в разделе отзывов.", reply_markup=menu_kb())
    
    if ADMIN_ID:
        try:
            stars = "⭐" * data['rating']
            await bot.send_message(ADMIN_ID, f"📩 Новый отзыв!\nОценка: {stars}\nТекст: {message.text}")
        except:
            pass

# ================= ЗАПИСЬ =================
@dp.callback_query(F.data == "book")
async def cb_book(call: CallbackQuery, state: FSMContext):
    await state.clear()
    can_book, message = db.can_make_booking(call.from_user.id)
    if not can_book:
        await call.message.edit_text(message, reply_markup=menu_kb())
        await call.answer()
        return
    await call.message.edit_text("📅 Выберите день:", reply_markup=days_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("day_"))
async def cb_day(call: CallbackQuery, state: FSMContext):
    day = call.data.split("_")[1]
    await state.update_data(day=day)
    kb = times_kb(day)
    d = date.fromisoformat(day)
    label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
    if len(kb.inline_keyboard) <= 1:
        await call.message.edit_text(f"😔 На {label} всё занято", reply_markup=days_kb())
    else:
        await call.message.edit_text(f"🕐 {label} — свободное время:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("time_"))
async def cb_time(call: CallbackQuery, state: FSMContext):
    _, day, t = call.data.split("_", 2)
    await state.update_data(day=day, time=t)
    
    service_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s.name, callback_data=f"service_{i}")]
        for i, s in enumerate(PRICES)
    ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="book")]])
    
    await call.message.edit_text(
        f"✅ Вы выбрали: {day} в {t}\n\n💅 Выберите услугу:",
        reply_markup=service_kb
    )
    await call.answer()

@dp.callback_query(F.data.startswith("service_"))
async def cb_service(call: CallbackQuery, state: FSMContext):
    service_idx = int(call.data.split("_")[1])
    service = PRICES[service_idx]
    await state.update_data(service=service.name)
    await state.set_state(BookingStates.name)
    await call.message.edit_text(f"💅 Выбрано: {service.name}\n\n👤 Как к вам обращаться? (имя):")
    await call.answer()

@dp.message(BookingStates.name)
async def bk_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(BookingStates.phone)
    await message.answer("📞 Оставьте номер телефона (или /skip для пропуска):")

@dp.message(BookingStates.phone, Command("skip"))
async def bk_phone_skip(message: Message, state: FSMContext):
    await state.update_data(phone="")
    await show_confirm(message, state)

@dp.message(BookingStates.phone)
async def bk_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await show_confirm(message, state)

async def show_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    d = date.fromisoformat(data["day"])
    date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]}) {data['time']}"
    
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="menu")]
    ])
    
    await message.answer(
        f"📋 ПРОВЕРЬТЕ ЗАПИСЬ:\n\n"
        f"📅 {date_label}\n"
        f"💅 {data.get('service', 'Не указана')}\n"
        f"👤 {data['name']}\n"
        f"📞 {data.get('phone') or '—'}\n\n"
        "Всё верно?",
        reply_markup=confirm_kb
    )

@dp.callback_query(F.data == "confirm_yes")
async def cb_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    success, message = db.save_booking(
        call.from_user.id,
        data["day"],
        data["time"],
        data["name"],
        data.get("phone", ""),
        data.get("service", "")
    )
    
    if success:
        d = date.fromisoformat(data["day"])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]}) {data['time']}"
        await call.message.edit_text(
            f"✅ ЗАПИСЬ ПОДТВЕРЖДЕНА!\n\n"
            f"📅 {date_label}\n"
            f"👤 {data['name']}\n"
            f"💅 {data.get('service', 'Не указана')}\n\n"
            "✨ Ждём вас!",
            reply_markup=menu_kb()
        )
        if ADMIN_ID:
            try:
                await bot.send_message(ADMIN_ID, f"🆕 Новая запись!\n📅 {date_label}\n👤 {data['name']}")
            except:
                pass
    else:
        await call.message.edit_text(message, reply_markup=menu_kb())
    
    await state.clear()
    await call.answer()

# ================= АДМИН-ПАНЕЛЬ =================
@dp.callback_query(F.data == "admin_panel")
@admin_only
async def cb_admin_panel(call: CallbackQuery):
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Все записи", callback_data="admin_bookings")],
        [InlineKeyboardButton(text="⭐ Отзывы на модерации", callback_data="admin_feedback")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]
    ])
    await call.message.edit_text("⚙️ АДМИН-ПАНЕЛЬ\n\nВыберите действие:", reply_markup=admin_kb)
    await call.answer()

@dp.callback_query(F.data == "admin_bookings")
@admin_only
async def cb_admin_bookings(call: CallbackQuery):
    bookings = db.get_all_bookings()
    if not bookings:
        await call.message.edit_text("📋 Нет активных записей", reply_markup=back_kb())
        await call.answer()
        return
    
    text = "📋 ВСЕ ЗАПИСИ\n\n"
    for b in bookings[:20]:
        d = date.fromisoformat(b['day'])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"🆔 #{b['id']} {date_label} {b['time']}\n"
        text += f"   👤 {b['name']}"
        if b['phone']:
            text += f" 📞 {b['phone']}"
        text += "\n"
        if b['service']:
            text += f"   💅 {b['service']}\n"
        text += "\n"
    
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()

@dp.callback_query(F.data == "admin_feedback")
@admin_only
async def cb_admin_feedback(call: CallbackQuery):
    feedbacks = db.get_unapproved_feedback()
    if not feedbacks:
        await call.message.edit_text("⭐ Нет отзывов на модерации", reply_markup=back_kb())
        await call.answer()
        return
    
    fb = feedbacks[0]
    stars = "⭐" * fb['rating'] + "☆" * (5 - fb['rating'])
    await call.message.edit_text(
        f"⭐ ОТЗЫВ #{fb['id']}\n\n"
        f"👤 {fb['username'] or fb['user_id']}\n"
        f"Рейтинг: {stars}\n"
        f"📝 {fb['text']}\n"
        f"📅 {fb['created_at']}\n\n"
        f"Осталось {len(feedbacks) - 1} отзывов",
        reply_markup=admin_feedback_kb(fb['id'])
    )
    await call.answer()

@dp.callback_query(F.data.startswith("admin_approve_fb_"))
@admin_only
async def cb_admin_approve_feedback(call: CallbackQuery):
    fb_id = int(call.data.split("_")[3])
    db.approve_feedback(fb_id)
    await call.message.edit_text("✅ Отзыв одобрен", reply_markup=back_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("admin_reject_fb_"))
@admin_only
async def cb_admin_reject_feedback(call: CallbackQuery):
    fb_id = int(call.data.split("_")[3])
    with db.get_connection() as conn:
        conn.execute("DELETE FROM feedback WHERE id = ?", (fb_id,))
    await call.message.edit_text("❌ Отзыв отклонен", reply_markup=back_kb())
    await call.answer()

# ================= ЗАПУСК =================
async def main():
    try:
        db.init_db()
        logger.info("✅ База данных инициализирована")
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Вебхук удален")
        logger.info("🚀 Бот запущен!")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
