import os, logging, asyncio, sqlite3, random, string, threading
from fastapi import FastAPI
import uvicorn
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# === КОНФИГ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
DB_PATH = "data.db"
logging.basicConfig(level=logging.INFO)

# === БАЗА ДАННЫХ (SQLite, синхронная, но быстрая) ===
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, link TEXT UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY AUTOINCREMENT, from_user_id INTEGER, to_user_id INTEGER NOT NULL, text TEXT NOT NULL, is_read BOOLEAN DEFAULT 0, reply_to_id INTEGER, replied_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS blocks (blocker_id INTEGER NOT NULL, blocked_id INTEGER NOT NULL, PRIMARY KEY (blocker_id, blocked_id))")
        c.execute("CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL, question_id INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.commit()

def get_or_create_user(user):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, link FROM users WHERE user_id = ?", (user.id,))
        row = c.fetchone()
        if row:
            c.execute("UPDATE users SET username=?, first_name=?, last_name=? WHERE user_id=?", (user.username, user.first_name, user.last_name, user.id))
            conn.commit()
            return row[0], row[1]
        else:
            link = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
            while True:
                c.execute("SELECT 1 FROM users WHERE link = ?", (link,))
                if not c.fetchone(): break
                link = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
            c.execute("INSERT INTO users (user_id, username, first_name, last_name, link) VALUES (?, ?, ?, ?, ?)", (user.id, user.username, user.first_name, user.last_name, link))
            conn.commit()
            return user.id, link

def get_user_by_link(link):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE link = ?", (link,))
        row = c.fetchone()
        return row[0] if row else None

def save_question(from_user_id, to_user_id, text, reply_to_id=None):
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO questions (from_user_id, to_user_id, text, reply_to_id) VALUES (?, ?, ?, ?)", (from_user_id, to_user_id, text, reply_to_id))
        conn.commit()
        return c.lastrowid

def get_incoming(user_id, limit=10, offset=0):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, from_user_id, text, is_read, created_at, replied_at FROM questions WHERE to_user_id = ? AND reply_to_id IS NULL ORDER BY created_at DESC LIMIT ? OFFSET ?", (user_id, limit, offset))
        return c.fetchall()

def mark_read(q_id):
    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE questions SET is_read = 1 WHERE id = ?", (q_id,))
        conn.commit()

def get_question(q_id):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, from_user_id, to_user_id, text, is_read, reply_to_id, created_at, replied_at FROM questions WHERE id = ?", (q_id,))
        return c.fetchone()

def save_reply(from_user_id, to_user_id, text, reply_to_id):
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO questions (from_user_id, to_user_id, text, reply_to_id) VALUES (?, ?, ?, ?)", (from_user_id, to_user_id, text, reply_to_id))
        conn.commit()
        new_id = c.lastrowid
        c.execute("UPDATE questions SET replied_at = CURRENT_TIMESTAMP WHERE id = ?", (reply_to_id,))
        conn.commit()
        return new_id

def block_user(blocker, blocked):
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)", (blocker, blocked))
        conn.commit()

def unblock_user(blocker, blocked):
    with db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (blocker, blocked))
        conn.commit()

def is_blocked(blocker, blocked):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (blocker, blocked))
        return c.fetchone() is not None

def get_blocked(blocker):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT blocked_id FROM blocks WHERE blocker_id = ?", (blocker,))
        return [r[0] for r in c.fetchall()]

def add_report(reporter, q_id):
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO reports (reporter_id, question_id) VALUES (?, ?)", (reporter, q_id))
        conn.commit()

def get_stats():
    with db() as conn:
        c = conn.cursor()
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        incoming = c.execute("SELECT COUNT(*) FROM questions WHERE reply_to_id IS NULL").fetchone()[0]
        replies = c.execute("SELECT COUNT(*) FROM questions WHERE reply_to_id IS NOT NULL").fetchone()[0]
        reports = c.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        return users, total, incoming, replies, reports

def get_all_users():
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users")
        return [r[0] for r in c.fetchall()]

def get_reports(limit=20):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT r.id, r.reporter_id, r.question_id, r.created_at, q.text FROM reports r JOIN questions q ON r.question_id = q.id ORDER BY r.created_at DESC LIMIT ?", (limit,))
        return c.fetchall()

init_db()

# === БОТ ===
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class AskState(StatesGroup): waiting = State()
class ReplyState(StatesGroup): waiting = State()

# --- Клавиатуры ---
def main_kb(user_id):
    kb = [[KeyboardButton(text="📩 Задать вопрос")], [KeyboardButton(text="📥 Входящие")], [KeyboardButton(text="🔗 Моя ссылка")], [KeyboardButton(text="🚫 Заблокированные")]]
    if user_id in ADMIN_IDS: kb.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def cancel_kb(): return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

def inline_incoming(questions, page=0, has_more=False):
    kb = [[InlineKeyboardButton(text=f"{'✅' if q[3] else '🆕'} {q[2][:30]}...", callback_data=f"view_{q[0]}")] for q in questions]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{page-1}"))
    if has_more: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def question_kb(q_id, answered=False):
    kb = []
    if not answered: kb.append([InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{q_id}")])
    kb.append([InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{q_id}"), InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"report_{q_id}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_incoming")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def blocks_kb(blocked_ids):
    kb = [[InlineKeyboardButton(text=f"Разблокировать {uid}", callback_data=f"unblock_{uid}")] for uid in blocked_ids]
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="⚠️ Жалобы", callback_data="reports_list")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")]
    ])

# --- Хендлеры ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    user = message.from_user
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        target = get_user_by_link(args[1].strip())
        if target:
            await state.update_data(target=target)
            await message.answer("✉️ Напиши свой анонимный вопрос:", reply_markup=cancel_kb())
            await state.set_state(AskState.waiting)
            return
    uid, link = get_or_create_user(user)
    bot_info = await bot.get_me()
    await message.answer(f"👋 Привет, {user.first_name}!\n\n🔗 Твоя ссылка:\n<code>https://t.me/{bot_info.username}?start={link}</code>\n\nОтправь её друзьям.", reply_markup=main_kb(uid), parse_mode="HTML")

@dp.message(F.text == "❌ Отмена", StateFilter(AskState.waiting))
async def cancel_ask(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_kb(message.from_user.id))

@dp.message(AskState.waiting)
async def ask_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target")
    if not target: return await message.answer("Ошибка", reply_markup=main_kb(message.from_user.id))
    if is_blocked(target, message.from_user.id): return await message.answer("Ты заблокирован.", reply_markup=main_kb(message.from_user.id))
    q_id = save_question(None, target, message.text)
    await bot.send_message(target, f"📩 Новый вопрос:\n\n{message.text}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{q_id}")]]))
    await message.answer("✅ Отправлено!", reply_markup=main_kb(message.from_user.id))
    await state.clear()

@dp.message(ReplyState.waiting)
async def reply_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q_id = data.get("reply_qid")
    to_user = data.get("reply_to")
    if not q_id or not to_user: return await message.answer("Ошибка", reply_markup=main_kb(message.from_user.id))
    new_q = save_reply(None, to_user, message.text, q_id)
    await bot.send_message(to_user, f"📩 Ответ:\n\n{message.text}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{new_q}")]]))
    await message.answer("✅ Ответ отправлен!", reply_markup=main_kb(message.from_user.id))
    await state.clear()

@dp.message(F.text == "📩 Задать вопрос")
async def ask_button(message: types.Message):
    await message.answer("✏️ Чтобы задать вопрос, перейди по ссылке пользователя. Если уже перешёл — напиши вопрос.", reply_markup=cancel_kb())

@dp.message(F.text == "📥 Входящие")
async def incoming_button(message: types.Message):
    user_id = message.from_user.id
    qs = get_incoming(user_id, 10, 0)
    if not qs: return await message.answer("Нет вопросов.", reply_markup=main_kb(user_id))
    for q in qs: mark_read(q[0])
    has_more = len(get_incoming(user_id, 1, 10)) > 0
    await message.answer("📥 Входящие:", reply_markup=inline_incoming(qs, 0, has_more))

@dp.message(F.text == "🔗 Моя ссылка")
async def mylink_button(message: types.Message):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT link FROM users WHERE user_id=?", (message.from_user.id,))
        row = c.fetchone()
    if row:
        bot_info = await bot.get_me()
        await message.answer(f"🔗 Твоя ссылка:\n<code>https://t.me/{bot_info.username}?start={row[0]}</code>", reply_markup=main_kb(message.from_user.id), parse_mode="HTML")

@dp.message(F.text == "🚫 Заблокированные")
async def blocks_button(message: types.Message):
    blocked = get_blocked(message.from_user.id)
    if not blocked: return await message.answer("Нет заблокированных.", reply_markup=main_kb(message.from_user.id))
    await message.answer("🚫 Заблокированные:", reply_markup=blocks_kb(blocked))

@dp.message(F.text == "⚙️ Админ-панель")
async def admin_button(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return await message.answer("Нет прав", reply_markup=main_kb(message.from_user.id))
    await message.answer("⚙️ Админ-панель:", reply_markup=admin_kb())

# --- Инлайн колбэки ---
@dp.callback_query(F.data == "back_menu")
async def back_menu(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_kb(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data == "back_incoming")
async def back_incoming(callback: types.CallbackQuery):
    await callback.message.delete()
    user_id = callback.from_user.id
    qs = get_incoming(user_id, 10, 0)
    if not qs: return await callback.message.answer("Нет вопросов.", reply_markup=main_kb(user_id))
    has_more = len(get_incoming(user_id, 1, 10)) > 0
    await callback.message.answer("📥 Входящие:", reply_markup=inline_incoming(qs, 0, has_more))
    await callback.answer()

@dp.callback_query(F.data.startswith("view_"))
async def view_question(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id: return await callback.answer("Недоступно")
    await callback.message.edit_text(f"📩 Вопрос:\n\n{q[3]}", reply_markup=question_kb(q_id, q[7] is not None))
    await callback.answer()

@dp.callback_query(F.data.startswith("reply_"))
async def reply_question(callback: types.CallbackQuery, state: FSMContext):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id: return await callback.answer("Недоступно")
    await state.update_data(reply_qid=q_id, reply_to=q[1])
    await callback.message.answer("✏️ Напиши ответ:", reply_markup=cancel_kb())
    await state.set_state(ReplyState.waiting)
    await callback.answer()

@dp.callback_query(F.data.startswith("block_"))
async def block_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id: return await callback.answer("Недоступно")
    if not q[1]: return await callback.answer("Аноним, нельзя заблокировать")
    block_user(callback.from_user.id, q[1])
    await callback.answer("Заблокирован")
    await callback.message.edit_text("🚫 Заблокирован")
    await back_incoming(callback)

@dp.callback_query(F.data.startswith("unblock_"))
async def unblock_cb(callback: types.CallbackQuery):
    uid = int(callback.data.split("_")[1])
    unblock_user(callback.from_user.id, uid)
    blocked = get_blocked(callback.from_user.id)
    if blocked:
        await callback.message.edit_reply_markup(reply_markup=blocks_kb(blocked))
    else:
        await callback.message.edit_text("Нет заблокированных.")
    await callback.answer("Разблокирован")

@dp.callback_query(F.data.startswith("report_"))
async def report_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id: return await callback.answer("Недоступно")
    add_report(callback.from_user.id, q_id)
    for admin in ADMIN_IDS:
        try: await bot.send_message(admin, f"⚠️ Жалоба на вопрос #{q_id} от {callback.from_user.id}\n\n{q[3]}")
        except: pass
    await callback.answer("Жалоба отправлена")

@dp.callback_query(F.data.startswith("page_"))
async def page_cb(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    qs = get_incoming(user_id, 10, page*10)
    if not qs: return await callback.answer("Нет вопросов")
    has_more = len(get_incoming(user_id, 1, (page+1)*10)) > 0
    await callback.message.edit_reply_markup(reply_markup=inline_incoming(qs, page, has_more))
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def stats_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("Нет прав")
    users, total, incoming, replies, reports = get_stats()
    await callback.message.edit_text(f"📊 Статистика:\n👤 {users}\n📩 {total}\n📥 {incoming}\n✏️ {replies}\n⚠️ {reports}", reply_markup=admin_kb())
    await callback.answer()

@dp.callback_query(F.data == "reports_list")
async def reports_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("Нет прав")
    reports = get_reports(20)
    if not reports: return await callback.message.edit_text("Нет жалоб.", reply_markup=admin_kb())
    text = "⚠️ Жалобы:\n\n" + "\n".join([f"#{r[0]} от {r[1]} на вопрос #{r[2]}\n{r[4][:100]}\n---" for r in reports])
    await callback.message.edit_text(text, reply_markup=admin_kb())
    await callback.answer()

# --- Рассылка ---
@dp.message(Command("broadcast"))
async def broadcast_cmd(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return await message.answer("Нет прав")
    text = message.text.replace("/broadcast", "").strip()
    if not text: return await message.answer("Напиши текст после /broadcast")
    users = get_all_users()
    if not users: return await message.answer("Нет пользователей")
    msg = await message.answer("📨 Рассылка...")
    count = 0
    for uid in users:
        try: await bot.send_message(uid, text); count += 1; await asyncio.sleep(0.05)
        except: pass
    await msg.edit_text(f"✅ Отправлено {count} из {len(users)}")

# === KEEP-ALIVE ===
web_app = FastAPI()
@web_app.get("/")
def health(): return {"status": "ok"}

def run_web():
    uvicorn.run(web_app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())                    chk = await conn.execute("SELECT 1 FROM users WHERE link = ?", (link,))
                    if not await chk.fetchone():
                        break
                    link = self._generate_link()
                await conn.execute(
                    "INSERT INTO users (user_id, username, first_name, last_name, link) VALUES (?, ?, ?, ?, ?)",
                    (user.id, user.username, user.first_name, user.last_name, link)
                )
                await conn.commit()
                return user.id, link

    def _generate_link(self, length=8):
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

    async def get_user_by_link(self, link):
        async with self.connect() as conn:
            row = await conn.execute("SELECT user_id FROM users WHERE link = ?", (link,))
            res = await row.fetchone()
            return res["user_id"] if res else None

    async def get_link(self, user_id):
        async with self.connect() as conn:
            row = await conn.execute("SELECT link FROM users WHERE user_id = ?", (user_id,))
            res = await row.fetchone()
            return res["link"] if res else None

    async def get_all_users(self):
        async with self.connect() as conn:
            rows = await conn.execute("SELECT user_id FROM users")
            return [r["user_id"] async for r in rows]

    # --- Вопросы ---
    async def save_question(self, from_user_id, to_user_id, text, reply_to_id=None):
        async with self.connect() as conn:
            cur = await conn.execute(
                "INSERT INTO questions (from_user_id, to_user_id, text, reply_to_id) VALUES (?, ?, ?, ?)",
                (from_user_id, to_user_id, text, reply_to_id)
            )
            await conn.commit()
            return cur.lastrowid

    async def get_incoming(self, user_id, limit=10, offset=0):
        async with self.connect() as conn:
            rows = await conn.execute(
                "SELECT id, from_user_id, text, is_read, created_at, replied_at FROM questions "
                "WHERE to_user_id = ? AND reply_to_id IS NULL "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset)
            )
            return [dict(r) async for r in rows]

    async def count_unread(self, user_id):
        async with self.connect() as conn:
            row = await conn.execute(
                "SELECT COUNT(*) FROM questions WHERE to_user_id = ? AND is_read = 0 AND reply_to_id IS NULL",
                (user_id,)
            )
            res = await row.fetchone()
            return res[0] if res else 0

    async def mark_read(self, q_id):
        async with self.connect() as conn:
            await conn.execute("UPDATE questions SET is_read = 1 WHERE id = ?", (q_id,))
            await conn.commit()

    async def get_question(self, q_id):
        async with self.connect() as conn:
            row = await conn.execute(
                "SELECT id, from_user_id, to_user_id, text, is_read, reply_to_id, created_at, replied_at "
                "FROM questions WHERE id = ?",
                (q_id,)
            )
            res = await row.fetchone()
            return dict(res) if res else None

    async def save_reply(self, from_user_id, to_user_id, text, reply_to_id):
        async with self.connect() as conn:
            cur = await conn.execute(
                "INSERT INTO questions (from_user_id, to_user_id, text, reply_to_id) VALUES (?, ?, ?, ?)",
                (from_user_id, to_user_id, text, reply_to_id)
            )
            await conn.commit()
            new_id = cur.lastrowid
            await conn.execute(
                "UPDATE questions SET replied_at = CURRENT_TIMESTAMP WHERE id = ?",
                (reply_to_id,)
            )
            await conn.commit()
            return new_id

    # --- Блокировки ---
    async def block_user(self, blocker, blocked):
        async with self.connect() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)",
                (blocker, blocked)
            )
            await conn.commit()

    async def unblock_user(self, blocker, blocked):
        async with self.connect() as conn:
            await conn.execute(
                "DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
                (blocker, blocked)
            )
            await conn.commit()

    async def is_blocked(self, blocker, blocked):
        async with self.connect() as conn:
            row = await conn.execute(
                "SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
                (blocker, blocked)
            )
            return await row.fetchone() is not None

    async def get_blocked_list(self, blocker):
        async with self.connect() as conn:
            rows = await conn.execute("SELECT blocked_id FROM blocks WHERE blocker_id = ?", (blocker,))
            return [r["blocked_id"] async for r in rows]

    # --- Жалобы ---
    async def add_report(self, reporter, q_id):
        async with self.connect() as conn:
            await conn.execute(
                "INSERT INTO reports (reporter_id, question_id) VALUES (?, ?)",
                (reporter, q_id)
            )
            await conn.commit()

    async def get_reports(self, limit=20):
        async with self.connect() as conn:
            rows = await conn.execute(
                "SELECT r.id, r.reporter_id, r.question_id, r.created_at, q.text "
                "FROM reports r JOIN questions q ON r.question_id = q.id "
                "ORDER BY r.created_at DESC LIMIT ?",
                (limit,)
            )
            return [dict(r) async for r in rows]

    # --- Статистика ---
    async def get_stats(self):
        async with self.connect() as conn:
            users = (await (await conn.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            total = (await (await conn.execute("SELECT COUNT(*) FROM questions")).fetchone())[0]
            incoming = (await (await conn.execute("SELECT COUNT(*) FROM questions WHERE reply_to_id IS NULL")).fetchone())[0]
            replies = (await (await conn.execute("SELECT COUNT(*) FROM questions WHERE reply_to_id IS NOT NULL")).fetchone())[0]
            reports = (await (await conn.execute("SELECT COUNT(*) FROM reports")).fetchone())[0]
            return users, total, incoming, replies, reports

db = Database(DB_PATH)

# ======== КЛАВИАТУРЫ ========
def main_keyboard(user_id):
    kb = [
        [KeyboardButton(text="📩 Задать вопрос")],
        [KeyboardButton(text="📥 Входящие")],
        [KeyboardButton(text="🔗 Моя ссылка")],
        [KeyboardButton(text="🚫 Заблокированные")],
    ]
    if user_id in ADMIN_IDS:
        kb.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def question_detail_kb(q_id, answered=False):
    kb = []
    if not answered:
        kb.append([InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{q_id}")])
    kb.append([
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{q_id}"),
        InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"report_{q_id}")
    ])
    kb.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_incoming")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def incoming_list_kb(questions, page=0, has_more=False):
    kb = []
    for q in questions:
        label = f"{'✅' if q['is_read'] else '🆕'} {q['text'][:30]}..."
        kb.append([InlineKeyboardButton(text=label, callback_data=f"view_{q['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{page-1}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{page+1}"))
    if nav:
        kb.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=kb)

def blocks_kb(blocked_ids):
    kb = [[InlineKeyboardButton(text=f"Разблокировать {uid}", callback_data=f"unblock_{uid}")] for uid in blocked_ids]
    kb.append([InlineKeyboardButton(text="🔙 Назад к меню", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_kb():
    kb = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="⚠️ Жалобы", callback_data="reports_list")],
        [InlineKeyboardButton(text="🔙 Назад к меню", callback_data="back_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ======== ХЕНДЛЕРЫ ========
dp = Dispatcher()

class AskState(StatesGroup):
    waiting = State()

class ReplyState(StatesGroup):
    waiting = State()

# ---- Общий возврат в меню ----
async def go_main(message: types.Message, text=None):
    user_id = message.from_user.id
    if text is None:
        text = "Главное меню:"
    await message.answer(text, reply_markup=main_keyboard(user_id))

# ---- /start ----
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user = message.from_user
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        link = args[1].strip()
        target = await db.get_user_by_link(link)
        if target:
            await state.update_data(target=target)
            await message.answer(
                "✉️ Напиши свой анонимный вопрос:",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="❌ Отмена")]],
                    resize_keyboard=True
                )
            )
            await state.set_state(AskState.waiting)
            return
    uid, link = await db.get_or_create_user(user)
    bot_info = await bot.get_me()
    full_link = f"https://t.me/{bot_info.username}?start={link}"
    await message.answer(
        f"👋 Привет, {user.first_name}!\n\n"
        f"🔗 Твоя ссылка для вопросов:\n<code>{full_link}</code>\n\n"
        "Отправь её друзьям — они смогут задать тебе анонимный вопрос.\n\n"
        "Используй кнопки внизу.",
        reply_markup=main_keyboard(uid)
    )

# ---- Обработка отмены в состояниях ----
@dp.message(F.text == "❌ Отмена", StateFilter(AskState.waiting))
async def cancel_ask(message: types.Message, state: FSMContext):
    await state.clear()
    await go_main(message, "Отменено.")

@dp.message(F.text == "❌ Отмена", StateFilter(ReplyState.waiting))
async def cancel_reply(message: types.Message, state: FSMContext):
    await state.clear()
    await go_main(message, "Отменено.")

# ---- Приём вопроса (AskState) ----
@dp.message(AskState.waiting)
async def ask_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target")
    if not target:
        await message.answer("❌ Ошибка, попробуй /start", reply_markup=main_keyboard(message.from_user.id))
        await state.clear()
        return
    if await db.is_blocked(target, message.from_user.id):
        await message.answer("🚫 Ты заблокирован этим пользователем.", reply_markup=main_keyboard(message.from_user.id))
        await state.clear()
        return
    q_id = await db.save_question(None, target, message.text)
    await bot.send_message(
        target,
        f"📩 Новый анонимный вопрос:\n\n{message.text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{q_id}")]])
    )
    await message.answer("✅ Вопрос отправлен анонимно!", reply_markup=main_keyboard(message.from_user.id))
    await state.clear()

# ---- Ответ на вопрос (ReplyState) ----
@dp.message(ReplyState.waiting)
async def reply_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q_id = data.get("reply_qid")
    to_user = data.get("reply_to")
    if not q_id or not to_user:
        await message.answer("❌ Ошибка", reply_markup=main_keyboard(message.from_user.id))
        await state.clear()
        return
    new_q = await db.save_reply(None, to_user, message.text, q_id)
    if to_user:
        await bot.send_message(
            to_user,
            f"📩 Ответ на твой вопрос:\n\n{message.text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{new_q}")]])
        )
    await message.answer("✅ Ответ отправлен анонимно!", reply_markup=main_keyboard(message.from_user.id))
    await state.clear()

# ---- Кнопки главного меню (ReplyKeyboard) ----
@dp.message(F.text == "📩 Задать вопрос")
async def ask_button(message: types.Message):
    await message.answer(
        "✏️ Чтобы задать вопрос, тебе нужна ссылка пользователя.\n"
        "Попроси у него ссылку или перейди по ней.\n\n"
        "Если ты уже перешёл по ссылке, просто напиши вопрос сейчас.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True
        )
    )
    # Здесь мы не устанавливаем состояние, т.к. вопрос принимается только через /start с ссылкой

@dp.message(F.text == "📥 Входящие")
async def incoming_button(message: types.Message):
    user_id = message.from_user.id
    questions = await db.get_incoming(user_id, limit=10)
    if not questions:
        await message.answer("📭 Нет вопросов.", reply_markup=main_keyboard(user_id))
        return
    for q in questions:
        await db.mark_read(q["id"])
    # Проверяем, есть ли ещё страницы
    has_more = len(await db.get_incoming(user_id, limit=1, offset=10)) > 0
    await message.answer(
        "📥 Входящие вопросы:",
        reply_markup=incoming_list_kb(questions, page=0, has_more=has_more)
    )

@dp.message(F.text == "🔗 Моя ссылка")
async def mylink_button(message: types.Message):
    user_id = message.from_user.id
    link = await db.get_link(user_id)
    if link:
        bot_info = await bot.get_me()
        full_link = f"https://t.me/{bot_info.username}?start={link}"
        await message.answer(
            f"🔗 Твоя ссылка:\n<code>{full_link}</code>",
            reply_markup=main_keyboard(user_id)
        )
    else:
        await message.answer("Ошибка, попробуй /start", reply_markup=main_keyboard(user_id))

@dp.message(F.text == "🚫 Заблокированные")
async def blocks_button(message: types.Message):
    user_id = message.from_user.id
    blocked = await db.get_blocked_list(user_id)
    if not blocked:
        await message.answer("Нет заблокированных.", reply_markup=main_keyboard(user_id))
        return
    await message.answer(
        "🚫 Заблокированные:",
        reply_markup=blocks_kb(blocked)
    )

@dp.message(F.text == "⚙️ Админ-панель")
async def admin_button(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.", reply_markup=main_keyboard(message.from_user.id))
        return
    await message.answer("⚙️ Админ-панель:", reply_markup=admin_kb())

# ---- Инлайн-колбэки ----
@dp.callback_query(F.data == "back_menu")
async def back_menu_cb(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_keyboard(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data == "back_incoming")
async def back_incoming_cb(callback: types.CallbackQuery):
    await callback.message.delete()
    user_id = callback.from_user.id
    questions = await db.get_incoming(user_id, limit=10)
    if not questions:
        await callback.message.answer("📭 Нет вопросов.", reply_markup=main_keyboard(user_id))
        await callback.answer()
        return
    has_more = len(await db.get_incoming(user_id, limit=1, offset=10)) > 0
    await callback.message.answer("📥 Входящие вопросы:", reply_markup=incoming_list_kb(questions, page=0, has_more=has_more))
    await callback.answer()

@dp.callback_query(F.data.startswith("view_"))
async def view_question_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = await db.get_question(q_id)
    if not q or q["to_user_id"] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    answered = q["replied_at"] is not None
    await callback.message.edit_text(
        f"📩 Вопрос:\n\n{q['text']}",
        reply_markup=question_detail_kb(q_id, answered)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("reply_"))
async def reply_question_cb(callback: types.CallbackQuery, state: FSMContext):
    q_id = int(callback.data.split("_")[1])
    q = await db.get_question(q_id)
    if not q or q["to_user_id"] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    await state.update_data(reply_qid=q_id, reply_to=q["from_user_id"])
    await callback.message.answer(
        "✏️ Напиши свой ответ:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True
        )
    )
    await state.set_state(ReplyState.waiting)
    await callback.answer()

@dp.callback_query(F.data.startswith("block_"))
async def block_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = await db.get_question(q_id)
    if not q or q["to_user_id"] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    from_user = q["from_user_id"]
    if not from_user:
        await callback.answer("Анонимный вопрос, нельзя заблокировать")
        return
    await db.block_user(callback.from_user.id, from_user)
    await callback.answer("Пользователь заблокирован")
    await callback.message.edit_text("🚫 Пользователь заблокирован")
    # Возвращаемся к списку входящих
    await back_incoming_cb(callback)

@dp.callback_query(F.data.startswith("unblock_"))
async def unblock_cb(callback: types.CallbackQuery):
    uid = int(callback.data.split("_")[1])
    await db.unblock_user(callback.from_user.id, uid)
    blocked = await db.get_blocked_list(callback.from_user.id)
    if blocked:
        await callback.message.edit_reply_markup(reply_markup=blocks_kb(blocked))
    else:
        await callback.message.edit_text("Нет заблокированных.")
    await callback.answer("Разблокирован")

@dp.callback_query(F.data.startswith("report_"))
async def report_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = await db.get_question(q_id)
    if not q or q["to_user_id"] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    await db.add_report(callback.from_user.id, q_id)
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, f"⚠️ Жалоба на вопрос #{q_id} от {callback.from_user.id}\n\nТекст: {q['text']}")
        except:
            pass
    await callback.answer("Жалоба отправлена админу")

@dp.callback_query(F.data.startswith("page_"))
async def page_cb(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    limit = 10
    offset = page * limit
    questions = await db.get_incoming(user_id, limit, offset)
    if not questions:
        await callback.answer("Нет вопросов на этой странице")
        return
    has_more = len(await db.get_incoming(user_id, limit=1, offset=offset+limit)) > 0
    await callback.message.edit_reply_markup(reply_markup=incoming_list_kb(questions, page=page, has_more=has_more))
    await callback.answer()

# ---- Админские инлайн-колбэки ----
@dp.callback_query(F.data == "stats")
async def stats_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав")
        return
    users, total, incoming, replies, reports = await db.get_stats()
    text = (
        f"📊 Статистика:\n"
        f"👤 Пользователей: {users}\n"
        f"📩 Всего вопросов: {total}\n"
        f"📥 Входящих: {incoming}\n"
        f"✏️ Ответов: {replies}\n"
        f"⚠️ Жалоб: {reports}"
    )
    await callback.message.edit_text(text, reply_markup=admin_kb())
    await callback.answer()

@dp.callback_query(F.data == "reports_list")
async def reports_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав")
        return
    reports = await db.get_reports(limit=20)
    if not reports:
        await callback.message.edit_text("Нет жалоб.", reply_markup=admin_kb())
        await callback.answer()
        return
    text = "⚠️ Жалобы:\n\n"
    for r in reports:
        text += f"#{r['id']} от {r['reporter_id']} на вопрос #{r['question_id']}\nТекст: {r['text'][:100]}\n---\n"
    await callback.message.edit_text(text, reply_markup=admin_kb())
    await callback.answer()

# ---- БРОДКАСТ (команда) ----
@dp.message(Command("broadcast"))
async def broadcast_cmd(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав")
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("📝 Напиши текст после /broadcast")
        return
    users = await db.get_all_users()
    if not users:
        await message.answer("Нет пользователей")
        return
    msg = await message.answer("📨 Рассылка...")
    count = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await msg.edit_text(f"✅ Отправлено {count} из {len(users)}")

# ======== KEEP-ALIVE ========
web_app = FastAPI()
@web_app.get("/")
def health():
    return {"status": "ok"}

def run_web():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(web_app, host="0.0.0.0", port=port)

async def main():
    await db.init()
    logger.info("База данных инициализирована")
    await dp.start_polling(bot)

if __name__ == "__main__":
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE link = ?", (link,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_question(from_user_id, to_user_id, text, reply_to_id=None):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO questions (from_user_id, to_user_id, text, reply_to_id) VALUES (?, ?, ?, ?)",
              (from_user_id, to_user_id, text, reply_to_id))
    conn.commit()
    q_id = c.lastrowid
    conn.close()
    return q_id

def get_questions(user_id, limit=10, offset=0):
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT id, from_user_id, text, is_read, created_at, replied_at
        FROM questions
        WHERE to_user_id = ? AND reply_to_id IS NULL
        ORDER BY created_at DESC LIMIT ? OFFSET ?
    """, (user_id, limit, offset))
    rows = c.fetchall()
    conn.close()
    return rows

def count_unread(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM questions WHERE to_user_id = ? AND is_read = 0 AND reply_to_id IS NULL", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def get_question(q_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, from_user_id, to_user_id, text, is_read, reply_to_id, created_at, replied_at FROM questions WHERE id = ?", (q_id,))
    row = c.fetchone()
    conn.close()
    return row

def mark_read(q_id):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE questions SET is_read = 1 WHERE id = ?", (q_id,))
    conn.commit()
    conn.close()

def save_reply(from_user_id, to_user_id, text, reply_to_id):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO questions (from_user_id, to_user_id, text, reply_to_id) VALUES (?, ?, ?, ?)",
              (from_user_id, to_user_id, text, reply_to_id))
    conn.commit()
    q_id = c.lastrowid
    c.execute("UPDATE questions SET replied_at = CURRENT_TIMESTAMP WHERE id = ?", (reply_to_id,))
    conn.commit()
    conn.close()
    return q_id

def block_user(blocker, blocked):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)", (blocker, blocked))
    conn.commit()
    conn.close()

def unblock_user(blocker, blocked):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (blocker, blocked))
    conn.commit()
    conn.close()

def is_blocked(blocker, blocked):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (blocker, blocked))
    row = c.fetchone()
    conn.close()
    return row is not None

def get_blocked(blocker):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT blocked_id FROM blocks WHERE blocker_id = ?", (blocker,))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

def add_report(reporter, q_id):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO reports (reporter_id, question_id) VALUES (?, ?)", (reporter, q_id))
    conn.commit()
    conn.close()

def get_stats():
    conn = db()
    c = conn.cursor()
    users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    incoming = c.execute("SELECT COUNT(*) FROM questions WHERE reply_to_id IS NULL").fetchone()[0]
    replies = c.execute("SELECT COUNT(*) FROM questions WHERE reply_to_id IS NOT NULL").fetchone()[0]
    reports = c.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    conn.close()
    return users, total, incoming, replies, reports

def get_reports(limit=20):
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT r.id, r.reporter_id, r.question_id, r.created_at, q.text
        FROM reports r JOIN questions q ON r.question_id = q.id
        ORDER BY r.created_at DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_users():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

init_db()

# === БОТ ===
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class AskState(StatesGroup):
    waiting = State()

class ReplyState(StatesGroup):
    waiting = State()

# --- Reply-клавиатура (всегда внизу) ---
def main_keyboard(user_id):
    kb = [
        [KeyboardButton(text="📩 Задать вопрос")],
        [KeyboardButton(text="📥 Входящие")],
        [KeyboardButton(text="🔗 Моя ссылка")],
        [KeyboardButton(text="🚫 Заблокированные")],
    ]
    if user_id in ADMIN_IDS:
        kb.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- Инлайн-клавиатуры для внутренних действий ---
def question_detail_kb(q_id, answered=False):
    kb = []
    if not answered:
        kb.append([InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{q_id}")])
    kb.append([
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{q_id}"),
        InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"report_{q_id}")
    ])
    kb.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_incoming")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def incoming_list_kb(questions, page=0):
    kb = []
    for q in questions:
        q_id, from_user, text, is_read, created_at, replied_at = q
        label = f"{'✅' if is_read else '🆕'} {text[:30]}..."
        kb.append([InlineKeyboardButton(text=label, callback_data=f"view_{q_id}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{page-1}"))
    if len(questions) == 10:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{page+1}"))
    if nav:
        kb.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=kb)

def blocks_kb(blocked_ids):
    kb = [[InlineKeyboardButton(text=f"Разблокировать {uid}", callback_data=f"unblock_{uid}")] for uid in blocked_ids]
    kb.append([InlineKeyboardButton(text="🔙 Назад к меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_kb():
    kb = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="⚠️ Жалобы", callback_data="reports_list")],
        [InlineKeyboardButton(text="🔙 Назад к меню", callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- Хендлеры команд и текстов ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    user = message.from_user
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        link = args[1].strip()
        target = get_user_by_link(link)
        if target:
            await state.update_data(target=target)
            await message.answer(
                "✉️ Напиши свой анонимный вопрос:",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="❌ Отмена")]],
                    resize_keyboard=True
                )
            )
            await state.set_state(AskState.waiting)
            return
    uid, link = get_or_create_user(user)
    bot_user = await bot.get_me()
    full_link = f"https://t.me/{bot_user.username}?start={link}"
    await message.answer(
        f"👋 Привет, {user.first_name}!\n\n"
        f"🔗 Твоя ссылка для вопросов:\n<code>{full_link}</code>\n\n"
        "Отправь её друзьям — они смогут задать тебе анонимный вопрос.\n\n"
        "Используй кнопки внизу для навигации.",
        reply_markup=main_keyboard(uid)
    )

# --- Обработка текста "❌ Отмена" при ожидании вопроса ---
@dp.message(F.text == "❌ Отмена", StateFilter(AskState.waiting))
async def cancel_ask(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_keyboard(message.from_user.id))

# --- Приём вопроса в состоянии AskState ---
@dp.message(AskState.waiting)
async def ask_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target")
    if not target:
        await message.answer("❌ Ошибка, попробуй /start", reply_markup=main_keyboard(message.from_user.id))
        await state.clear()
        return
    if is_blocked(target, message.from_user.id):
        await message.answer("🚫 Ты заблокирован этим пользователем.", reply_markup=main_keyboard(message.from_user.id))
        await state.clear()
        return
    q_id = save_question(None, target, message.text)
    await bot.send_message(
        target,
        f"📩 Новый анонимный вопрос:\n\n{message.text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{q_id}")]])
    )
    await message.answer("✅ Вопрос отправлен анонимно!", reply_markup=main_keyboard(message.from_user.id))
    await state.clear()

# --- Обработка кнопок главного меню (Reply Keyboard) ---
@dp.message(F.text == "📩 Задать вопрос")
async def ask_button(message: types.Message):
    await message.answer(
        "✏️ Чтобы задать вопрос, тебе нужна ссылка пользователя.\n"
        "Попроси у него ссылку или перейди по ней, если она уже есть.\n\n"
        "Если ты уже перешёл по ссылке, просто напиши свой вопрос сейчас.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True
        )
    )
    # Не переключаем состояние, потому что вопрос будет через ссылку в /start.
    # Но можно оставить как подсказку.

@dp.message(F.text == "📥 Входящие")
async def incoming_button(message: types.Message):
    user_id = message.from_user.id
    questions = get_questions(user_id, limit=10)
    if not questions:
        await message.answer("📭 Нет вопросов.", reply_markup=main_keyboard(user_id))
        return
    for q in questions:
        mark_read(q[0])
    await message.answer(
        "📥 Входящие вопросы:",
        reply_markup=incoming_list_kb(questions)
    )

@dp.message(F.text == "🔗 Моя ссылка")
async def mylink_button(message: types.Message):
    user_id = message.from_user.id
    conn = db()
    c = conn.cursor()
    c.execute("SELECT link FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        bot_user = await bot.get_me()
        link = f"https://t.me/{bot_user.username}?start={row[0]}"
        await message.answer(
            f"🔗 Твоя ссылка:\n<code>{link}</code>",
            reply_markup=main_keyboard(user_id)
        )
    else:
        await message.answer("Ошибка, попробуй /start", reply_markup=main_keyboard(user_id))

@dp.message(F.text == "🚫 Заблокированные")
async def blocks_button(message: types.Message):
    user_id = message.from_user.id
    blocked = get_blocked(user_id)
    if not blocked:
        await message.answer("Нет заблокированных.", reply_markup=main_keyboard(user_id))
        return
    await message.answer(
        "🚫 Заблокированные:",
        reply_markup=blocks_kb(blocked)
    )

@dp.message(F.text == "⚙️ Админ-панель")
async def admin_button(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.", reply_markup=main_keyboard(message.from_user.id))
        return
    await message.answer("⚙️ Админ-панель:", reply_markup=admin_kb())

# --- Обработка инлайн-кнопок (для просмотра, ответа и т.д.) ---
@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_cb(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_keyboard(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data == "back_to_incoming")
async def back_to_incoming_cb(callback: types.CallbackQuery):
    await callback.message.delete()
    user_id = callback.from_user.id
    questions = get_questions(user_id, limit=10)
    if not questions:
        await callback.message.answer("📭 Нет вопросов.", reply_markup=main_keyboard(user_id))
        await callback.answer()
        return
    await callback.message.answer("📥 Входящие вопросы:", reply_markup=incoming_list_kb(questions))
    await callback.answer()

@dp.callback_query(F.data.startswith("view_"))
async def view_question_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    answered = q[7] is not None
    await callback.message.edit_text(
        f"📩 Вопрос:\n\n{q[3]}",
        reply_markup=question_detail_kb(q_id, answered)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("reply_"))
async def reply_question_cb(callback: types.CallbackQuery, state: FSMContext):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    await state.update_data(reply_qid=q_id, reply_to=q[1])
    await callback.message.answer(
        "✏️ Напиши свой ответ:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True
        )
    )
    await state.set_state(ReplyState.waiting)
    await callback.answer()

@dp.message(F.text == "❌ Отмена", StateFilter(ReplyState.waiting))
async def cancel_reply(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_keyboard(message.from_user.id))

@dp.message(ReplyState.waiting)
async def reply_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q_id = data.get("reply_qid")
    to_user = data.get("reply_to")
    if not q_id or not to_user:
        await message.answer("❌ Ошибка", reply_markup=main_keyboard(message.from_user.id))
        await state.clear()
        return
    new_q = save_reply(None, to_user, message.text, q_id)
    if to_user:
        await bot.send_message(
            to_user,
            f"📩 Ответ на твой вопрос:\n\n{message.text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{new_q}")]])
        )
    await message.answer("✅ Ответ отправлен анонимно!", reply_markup=main_keyboard(message.from_user.id))
    await state.clear()

@dp.callback_query(F.data.startswith("block_"))
async def block_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    from_user = q[1]
    if not from_user:
        await callback.answer("Анонимный вопрос, нельзя заблокировать")
        return
    block_user(callback.from_user.id, from_user)
    await callback.answer("Пользователь заблокирован")
    await callback.message.edit_text("🚫 Пользователь заблокирован")
    # Возвращаемся к списку входящих
    await back_to_incoming_cb(callback)

@dp.callback_query(F.data.startswith("unblock_"))
async def unblock_cb(callback: types.CallbackQuery):
    uid = int(callback.data.split("_")[1])
    unblock_user(callback.from_user.id, uid)
    blocked = get_blocked(callback.from_user.id)
    if blocked:
        await callback.message.edit_reply_markup(reply_markup=blocks_kb(blocked))
    else:
        await callback.message.edit_text("Нет заблокированных.")
    await callback.answer("Разблокирован")

@dp.callback_query(F.data.startswith("report_"))
async def report_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    add_report(callback.from_user.id, q_id)
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, f"⚠️ Жалоба на вопрос #{q_id} от {callback.from_user.id}\n\nТекст: {q[3]}")
        except:
            pass
    await callback.answer("Жалоба отправлена админу")

@dp.callback_query(F.data.startswith("page_"))
async def page_cb(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    questions = get_questions(user_id, limit=10, offset=page*10)
    if not questions:
        await callback.answer("Нет вопросов на этой странице")
        return
    await callback.message.edit_reply_markup(reply_markup=incoming_list_kb(questions, page))
    await callback.answer()

# --- Админские инлайн-колбэки ---
@dp.callback_query(F.data == "stats")
async def stats_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав")
        return
    users, total, incoming, replies, reports = get_stats()
    text = f"📊 Статистика:\n👤 Пользователей: {users}\n📩 Всего: {total}\n📥 Входящих: {incoming}\n✏️ Ответов: {replies}\n⚠️ Жалоб: {reports}"
    await callback.message.edit_text(text, reply_markup=admin_kb())
    await callback.answer()

@dp.callback_query(F.data == "reports_list")
async def reports_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав")
        return
    reports = get_reports(limit=20)
    if not reports:
        await callback.message.edit_text("Нет жалоб.", reply_markup=admin_kb())
        await callback.answer()
        return
    text = "⚠️ Жалобы:\n\n"
    for r in reports:
        text += f"#{r[0]} от {r[1]} на вопрос #{r[2]}\nТекст: {r[4][:100]}\n---\n"
    await callback.message.edit_text(text, reply_markup=admin_kb())
    await callback.answer()

# --- БРОДКАСТ ---
@dp.message(Command("broadcast"))
async def broadcast_cmd(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав")
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("📝 Напиши текст после /broadcast")
        return
    users = get_all_users()
    if not users:
        await message.answer("Нет пользователей")
        return
    msg = await message.answer("📨 Рассылка...")
    count = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await msg.edit_text(f"✅ Отправлено {count} из {len(users)}")

# === KEEP-ALIVE ===
web_app = FastAPI()
@web_app.get("/")
def health():
    return {"status": "ok"}

def run_web():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(web_app, host="0.0.0.0", port=port)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE link = ?", (link,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_question(from_user_id, to_user_id, text, reply_to_id=None):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO questions (from_user_id, to_user_id, text, reply_to_id) VALUES (?, ?, ?, ?)",
              (from_user_id, to_user_id, text, reply_to_id))
    conn.commit()
    q_id = c.lastrowid
    conn.close()
    return q_id

def get_questions(user_id, limit=10, offset=0):
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT id, from_user_id, text, is_read, created_at, replied_at
        FROM questions
        WHERE to_user_id = ? AND reply_to_id IS NULL
        ORDER BY created_at DESC LIMIT ? OFFSET ?
    """, (user_id, limit, offset))
    rows = c.fetchall()
    conn.close()
    return rows

def count_unread(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM questions WHERE to_user_id = ? AND is_read = 0 AND reply_to_id IS NULL", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def get_question(q_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, from_user_id, to_user_id, text, is_read, reply_to_id, created_at, replied_at FROM questions WHERE id = ?", (q_id,))
    row = c.fetchone()
    conn.close()
    return row

def mark_read(q_id):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE questions SET is_read = 1 WHERE id = ?", (q_id,))
    conn.commit()
    conn.close()

def save_reply(from_user_id, to_user_id, text, reply_to_id):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO questions (from_user_id, to_user_id, text, reply_to_id) VALUES (?, ?, ?, ?)",
              (from_user_id, to_user_id, text, reply_to_id))
    conn.commit()
    q_id = c.lastrowid
    c.execute("UPDATE questions SET replied_at = CURRENT_TIMESTAMP WHERE id = ?", (reply_to_id,))
    conn.commit()
    conn.close()
    return q_id

def block_user(blocker, blocked):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)", (blocker, blocked))
    conn.commit()
    conn.close()

def unblock_user(blocker, blocked):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (blocker, blocked))
    conn.commit()
    conn.close()

def is_blocked(blocker, blocked):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (blocker, blocked))
    row = c.fetchone()
    conn.close()
    return row is not None

def get_blocked(blocker):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT blocked_id FROM blocks WHERE blocker_id = ?", (blocker,))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

def add_report(reporter, q_id):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO reports (reporter_id, question_id) VALUES (?, ?)", (reporter, q_id))
    conn.commit()
    conn.close()

def get_stats():
    conn = db()
    c = conn.cursor()
    users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    incoming = c.execute("SELECT COUNT(*) FROM questions WHERE reply_to_id IS NULL").fetchone()[0]
    replies = c.execute("SELECT COUNT(*) FROM questions WHERE reply_to_id IS NOT NULL").fetchone()[0]
    reports = c.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    conn.close()
    return users, total, incoming, replies, reports

def get_reports(limit=20):
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT r.id, r.reporter_id, r.question_id, r.created_at, q.text
        FROM reports r JOIN questions q ON r.question_id = q.id
        ORDER BY r.created_at DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_users():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

init_db()

# === БОТ ===
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class AskState(StatesGroup):
    waiting = State()

class ReplyState(StatesGroup):
    waiting = State()

# --- Клавиатуры ---
def main_kb(user_id):
    unread = count_unread(user_id)
    kb = [
        [InlineKeyboardButton(text="📩 Задать вопрос", callback_data="ask")],
        [InlineKeyboardButton(text=f"📥 Входящие ({unread})" if unread else "📥 Входящие", callback_data="incoming")],
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="mylink")],
        [InlineKeyboardButton(text="🚫 Заблокированные", callback_data="blocks")],
    ]
    if user_id in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def incoming_kb(questions, page=0):
    kb = []
    for q in questions:
        q_id, from_user, text, is_read, created_at, replied_at = q
        label = f"{'✅' if is_read else '🆕'} {text[:30]}..."
        kb.append([InlineKeyboardButton(text=label, callback_data=f"view_{q_id}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{page-1}"))
    if len(questions) == 10:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def question_kb(q_id, answered=False):
    kb = []
    if not answered:
        kb.append([InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{q_id}")])
    kb.append([
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{q_id}"),
        InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"report_{q_id}")
    ])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="incoming")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def blocks_kb(blocked_ids):
    kb = [[InlineKeyboardButton(text=f"Разблокировать {uid}", callback_data=f"unblock_{uid}")] for uid in blocked_ids]
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_kb():
    kb = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="⚠️ Жалобы", callback_data="reports_list")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- Хендлеры ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    user = message.from_user
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        link = args[1].strip()
        target = get_user_by_link(link)
        if target:
            await state.update_data(target=target)
            await message.answer(
                "✉️ Напиши свой анонимный вопрос:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])
            )
            await state.set_state(AskState.waiting)
            return
    uid, link = get_or_create_user(user)
    bot_user = await bot.get_me()
    full_link = f"https://t.me/{bot_user.username}?start={link}"
    await message.answer(
        f"👋 Привет, {user.first_name}!\n\n"
        f"🔗 Твоя ссылка для вопросов:\n<code>{full_link}</code>\n\n"
        "Отправь её друзьям — они смогут задать тебе анонимный вопрос.",
        reply_markup=main_kb(uid)
    )

@dp.message(AskState.waiting)
async def ask_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target")
    if not target:
        await message.answer("❌ Ошибка, попробуй /start")
        await state.clear()
        return
    if is_blocked(target, message.from_user.id):
        await message.answer("🚫 Ты заблокирован этим пользователем.")
        await state.clear()
        return
    q_id = save_question(None, target, message.text)
    await bot.send_message(
        target,
        f"📩 Новый анонимный вопрос:\n\n{message.text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{q_id}")]])
    )
    await message.answer("✅ Вопрос отправлен анонимно!")
    await state.clear()

@dp.callback_query(F.data == "cancel", StateFilter(AskState.waiting))
async def cancel_ask(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("❌ Отменено.")
    await callback.answer()

@dp.callback_query(F.data == "menu")
async def menu_cb(callback: types.CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_kb(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data == "ask")
async def ask_cb(callback: types.CallbackQuery):
    await callback.message.answer("✏️ Напиши свой вопрос (сначала нужно получить ссылку у того, кому хочешь написать)")
    await callback.answer()

@dp.callback_query(F.data == "incoming")
async def incoming_cb(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    questions = get_questions(user_id, limit=10)
    if not questions:
        await callback.message.answer("📭 Нет вопросов.")
        await callback.answer()
        return
    for q in questions:
        mark_read(q[0])
    await callback.message.edit_text("📥 Входящие вопросы:", reply_markup=incoming_kb(questions))
    await callback.answer()

@dp.callback_query(F.data.startswith("page_"))
async def page_cb(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    questions = get_questions(user_id, limit=10, offset=page*10)
    if not questions:
        await callback.answer("Нет вопросов на этой странице")
        return
    await callback.message.edit_reply_markup(reply_markup=incoming_kb(questions, page))
    await callback.answer()

@dp.callback_query(F.data.startswith("view_"))
async def view_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    answered = q[7] is not None
    await callback.message.edit_text(f"📩 Вопрос:\n\n{q[3]}", reply_markup=question_kb(q_id, answered))
    await callback.answer()

@dp.callback_query(F.data.startswith("reply_"))
async def reply_cb(callback: types.CallbackQuery, state: FSMContext):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    await state.update_data(reply_qid=q_id, reply_to=q[1])
    await callback.message.answer("✏️ Напиши свой ответ:")
    await state.set_state(ReplyState.waiting)
    await callback.answer()

@dp.message(ReplyState.waiting)
async def reply_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q_id = data.get("reply_qid")
    to_user = data.get("reply_to")
    if not q_id or not to_user:
        await message.answer("❌ Ошибка")
        await state.clear()
        return
    new_q = save_reply(None, to_user, message.text, q_id)
    if to_user:
        await bot.send_message(
            to_user,
            f"📩 Ответ на твой вопрос:\n\n{message.text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply_{new_q}")]])
        )
    await message.answer("✅ Ответ отправлен анонимно!")
    await state.clear()

@dp.callback_query(F.data.startswith("block_"))
async def block_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    from_user = q[1]
    if not from_user:
        await callback.answer("Анонимный вопрос, нельзя заблокировать")
        return
    block_user(callback.from_user.id, from_user)
    await callback.answer("Пользователь заблокирован")
    await callback.message.edit_text("🚫 Пользователь заблокирован", reply_markup=main_kb(callback.from_user.id))

@dp.callback_query(F.data == "blocks")
async def blocks_cb(callback: types.CallbackQuery):
    blocked = get_blocked(callback.from_user.id)
    if not blocked:
        await callback.message.answer("Нет заблокированных")
        await callback.answer()
        return
    await callback.message.edit_text("🚫 Заблокированные:", reply_markup=blocks_kb(blocked))
    await callback.answer()

@dp.callback_query(F.data.startswith("unblock_"))
async def unblock_cb(callback: types.CallbackQuery):
    uid = int(callback.data.split("_")[1])
    unblock_user(callback.from_user.id, uid)
    blocked = get_blocked(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=blocks_kb(blocked))
    await callback.answer("Разблокирован")

@dp.callback_query(F.data.startswith("report_"))
async def report_cb(callback: types.CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = get_question(q_id)
    if not q or q[2] != callback.from_user.id:
        await callback.answer("Недоступно")
        return
    add_report(callback.from_user.id, q_id)
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, f"⚠️ Жалоба на вопрос #{q_id} от {callback.from_user.id}\n\nТекст: {q[3]}")
        except:
            pass
    await callback.answer("Жалоба отправлена админу")

@dp.callback_query(F.data == "mylink")
async def mylink_cb(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    conn = db()
    c = conn.cursor()
    c.execute("SELECT link FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        bot_user = await bot.get_me()
        link = f"https://t.me/{bot_user.username}?start={row[0]}"
        await callback.message.answer(f"🔗 Твоя ссылка:\n<code>{link}</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin")
async def admin_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав")
        return
    await callback.message.edit_text("⚙️ Админ-панель:", reply_markup=admin_kb())
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def stats_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав")
        return
    users, total, incoming, replies, reports = get_stats()
    text = f"📊 Статистика:\n👤 Пользователей: {users}\n📩 Всего: {total}\n📥 Входящих: {incoming}\n✏️ Ответов: {replies}\n⚠️ Жалоб: {reports}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin")]]))
    await callback.answer()

@dp.callback_query(F.data == "reports_list")
async def reports_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав")
        return
    reports = get_reports(limit=20)
    if not reports:
        await callback.message.answer("Нет жалоб")
        await callback.answer()
        return
    text = "⚠️ Жалобы:\n\n"
    for r in reports:
        text += f"#{r[0]} от {r[1]} на вопрос #{r[2]}\nТекст: {r[4][:100]}\n---\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin")]]))
    await callback.answer()

# === БРОДКАСТ ===
@dp.message(Command("broadcast"))
async def broadcast_cmd(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав")
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("📝 Напиши текст после /broadcast")
        return
    users = get_all_users()
    if not users:
        await message.answer("Нет пользователей")
        return
    msg = await message.answer("📨 Рассылка...")
    count = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await msg.edit_text(f"✅ Отправлено {count} из {len(users)}")

# === KEEP-ALIVE ===
web_app = FastAPI()
@web_app.get("/")
def health():
    return {"status": "ok"}

def run_web():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(web_app, host="0.0.0.0", port=port)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())
