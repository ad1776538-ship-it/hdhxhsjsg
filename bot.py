import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from aiogram.client.default import DefaultBotProperties

# ==================== НАСТРОЙКИ ====================
# Токен и админы берутся из переменных окружения (безопасно для деплоя,
# токен не хранится в коде/репозитории). Задаются в Railway → Variables.

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задана переменная окружения BOT_TOKEN. "
        "Локально: экспортируй BOT_TOKEN=твой_токен. "
        "На Railway: добавь переменную в разделе Variables."
    )

START_BALANCE = 1000  # стартовый баланс новых пользователей
DB_PATH = "bank.db"

# Валюта банка
CURRENCY_NAME = "Лядовкоин"  # полное название (для текстов)
CURRENCY_CODE = "Lyaco"  # короткий код валюты (для сумм, как USD/EUR)


def fmt_money(amount: int) -> str:
    """Форматирует сумму с кодом валюты, например: 1000 Lyaco."""
    return f"{amount} {CURRENCY_CODE}"

# ID администраторов бота (узнать свой ID можно у @userinfobot).
# Задаётся в переменной окружения ADMIN_IDS через запятую, например: "111111,222222"
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}


# Команды, которые видят все пользователи в меню бота (кнопка "Menu" / "/")
USER_COMMANDS = [
    BotCommand(command="start", description="Открыть счёт / приветствие"),
    BotCommand(command="bal", description="Узнать баланс"),
    BotCommand(command="send", description="Перевести деньги: /send @user сумма"),
    BotCommand(command="daily", description="Забрать ежедневный бонус"),
    BotCommand(command="hist", description="История операций"),
    BotCommand(command="top", description="Топ пользователей"),
    BotCommand(command="help", description="Список всех команд"),
]

# Дополнительные команды — видны только админам (из ADMIN_IDS), в их личном чате с ботом
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="add", description="[admin] Начислить деньги"),
    BotCommand(command="rem", description="[admin] Списать деньги"),
    BotCommand(command="set", description="[admin] Установить баланс"),
    BotCommand(command="ban", description="[admin] Заблокировать пользователя"),
    BotCommand(command="unban", description="[admin] Разблокировать пользователя"),
    BotCommand(command="users", description="[admin] Список пользователей"),
    BotCommand(command="stats", description="[admin] Статистика банка"),
    BotCommand(command="bc", description="[admin] Рассылка всем"),
]


async def setup_commands():
    """Настраивает меню команд (кнопка '/' в Telegram): обычное для всех,
    расширенное — только для админов в их личном чате с ботом."""
    await bot.set_my_commands(commands=USER_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                commands=ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            # админ ещё не писал боту в личку — меню применится после первого /start
            pass

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ==================== БАЗА ДАННЫХ ====================


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT UNIQUE,
            full_name TEXT,
            balance INTEGER NOT NULL DEFAULT 0,
            banned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER,
            to_id INTEGER,
            amount INTEGER,
            created_at TEXT
        )
        """
    )
    # миграция для баз, созданных до появления колонки banned
    try:
        cur.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # миграция для баз, созданных до появления ежедневного бонуса
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_daily TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def get_or_create_user(user_id: int, username: str | None, full_name: str) -> sqlite3.Row:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cur.fetchone()

    if user is None:
        cur.execute(
            "INSERT INTO users (user_id, username, full_name, balance, banned, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (user_id, username, full_name, START_BALANCE, datetime.utcnow().isoformat()),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cur.fetchone()
    else:
        if username and user["username"] != username:
            cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            conn.commit()
            cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = cur.fetchone()

    conn.close()
    return user


def get_user_by_username(username: str) -> sqlite3.Row | None:
    username = username.lstrip("@")
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
    user = cur.fetchone()
    conn.close()
    return user


def change_balance(user_id: int, delta: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
    conn.commit()
    conn.close()


def set_balance(user_id: int, amount: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def set_banned(user_id: int, banned: bool):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET banned = ? WHERE user_id = ?", (1 if banned else 0, user_id))
    conn.commit()
    conn.close()


def claim_daily(user_id: int, amount: int):
    """Отмечает выдачу ежедневного бонуса и начисляет деньги."""
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET balance = balance + ?, last_daily = ? WHERE user_id = ?",
        (amount, datetime.utcnow().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def log_transaction(from_id, to_id, amount: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions (from_id, to_id, amount, created_at) VALUES (?, ?, ?, ?)",
        (from_id, to_id, amount, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_history(user_id: int, limit: int = 10):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.*,
               uf.username AS from_username,
               ut.username AS to_username
        FROM transactions t
        LEFT JOIN users uf ON uf.user_id = t.from_id
        LEFT JOIN users ut ON ut.user_id = t.to_id
        WHERE t.from_id = ? OR t.to_id = ?
        ORDER BY t.id DESC
        LIMIT ?
        """,
        (user_id, user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_top(limit: int = 10):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY balance DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_users():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY created_at ASC")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_stats():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt, COALESCE(SUM(balance), 0) AS total FROM users")
    users_row = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE banned = 1")
    banned_row = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total FROM transactions")
    tx_row = cur.fetchone()
    conn.close()
    return {
        "users": users_row["cnt"],
        "total_money": users_row["total"],
        "banned": banned_row["cnt"],
        "transactions": tx_row["cnt"],
        "transferred_total": tx_row["total"],
    }


# ==================== ВСПОМОГАТЕЛЬНОЕ ====================


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def parse_username_and_amount(args: list[str]):
    """Парсит ['@user', '100'] -> ('user', 100). Возвращает None при ошибке."""
    if len(args) != 2:
        return None
    username, amount_str = args
    if not username.startswith("@"):
        return None
    if not amount_str.lstrip("-").isdigit():
        return None
    return username.lstrip("@"), int(amount_str)


# ==================== ОБЫЧНЫЕ КОМАНДЫ ====================


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )

    # если это первый /start от админа — теперь можно установить ему расширенное меню команд
    if is_admin(message.from_user.id):
        try:
            await bot.set_my_commands(
                commands=ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=message.from_user.id)
            )
        except Exception:
            pass

    if message.chat.type != ChatType.PRIVATE:
        await message.reply(
            f"👋 Привет, {message.from_user.full_name}! Счёт создан, баланс: <b>{fmt_money(user['balance'])} 💰</b>\n"
            f"Все команды работают и в группе."
        )
        return

    await message.answer(
        f"👋 Добро пожаловать в <b>Лядов Банк</b>!\n\n"
        f"Твой счёт создан. Стартовый баланс: <b>{fmt_money(user['balance'])} 💰</b>\n\n"
        f"⚠️ Чтобы получать переводы по юзернейму, у тебя должен быть публичный юзернейм.\n\n"
        f"Команды:\n"
        f"/bal — узнать баланс\n"
        f"/send @username сумма — перевести деньги\n"
        f"/daily — забрать ежедневный бонус (есть шанс на джекпот 🎰)\n"
        f"/hist — история операций\n"
        f"/top — топ пользователей\n"
        f"/help — помощь"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 <b>Команды бота:</b>\n\n"
        "/start — регистрация / приветствие\n"
        "/bal — узнать баланс\n"
        "/send @username сумма — перевести деньги\n"
        "/send id_пользователя сумма — перевод по числовому ID (если нет юзернейма)\n"
        "(в группе можно ответить на сообщение человека и написать <code>/send сумма</code>)\n"
        "/daily — забрать ежедневный бонус (раз в 24 часа, есть шанс на джекпот)\n"
        "/hist — последние 10 операций\n"
        "/top — топ-10 самых богатых пользователей"
    )
    if is_admin(message.from_user.id):
        text += (
            "\n\n🛠 <b>Админ-команды:</b>\n"
            "/add @username сумма — начислить деньги\n"
            "/rem @username сумма — списать деньги\n"
            "/set @username сумма — установить точный баланс\n"
            "/ban @username — заблокировать пользователя\n"
            "/unban @username — разблокировать\n"
            "/users — список всех пользователей\n"
            "/stats — статистика банка\n"
            "/bc текст — рассылка всем пользователям"
        )
    await message.answer(text)


@dp.message(Command("bal"))
async def cmd_balance(message: Message):
    user = get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    if user["banned"]:
        await message.reply("🚫 Твой счёт заблокирован администратором.")
        return
    await message.reply(f"💰 Твой баланс: <b>{fmt_money(user['balance'])}</b>")


@dp.message(Command("send"))
async def cmd_transfer(message: Message):
    sender = get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )

    if sender["banned"]:
        await message.reply("🚫 Твой счёт заблокирован, переводы недоступны.")
        return

    args = message.text.split()[1:]
    receiver = None
    amount = None

    # --- вариант 1: перевод ответом на сообщение (удобно в группах) ---
    if message.reply_to_message and len(args) == 1 and args[0].lstrip("-").isdigit():
        amount = int(args[0])
        target_user = message.reply_to_message.from_user

        if target_user.is_bot:
            await message.reply("❗ Нельзя перевести деньги боту")
            return

        receiver = get_or_create_user(target_user.id, target_user.username, target_user.full_name)

        if receiver["user_id"] == sender["user_id"]:
            await message.reply("❗ Нельзя перевести деньги самому себе")
            return

    # --- вариант 2: перевод по @username или по числовому Telegram ID ---
    elif len(args) == 2:
        target_raw, amount_str = args

        if not amount_str.lstrip("-").isdigit():
            await message.reply(
                "❗ Использование: <code>/send @username сумма</code>\n"
                "Или <code>/send id_пользователя сумма</code>\n"
                "Или ответь (reply) на сообщение человека и напиши <code>/send сумма</code>"
            )
            return
        amount = int(amount_str)

        if target_raw.startswith("@"):
            # перевод по юзернейму
            receiver = get_user_by_username(target_raw)
            if receiver is None:
                await message.reply(
                    f"❗ Пользователь {target_raw} не найден.\n"
                    f"Он должен хотя бы раз запустить бота командой /start."
                )
                return
        elif target_raw.isdigit():
            # перевод по числовому user_id — работает даже без юзернейма
            receiver = get_user_by_id(int(target_raw))
            if receiver is None:
                await message.reply(
                    f"❗ Пользователь с ID {target_raw} не найден.\n"
                    f"Он должен хотя бы раз запустить бота командой /start."
                )
                return
        else:
            await message.reply(
                "❗ Использование: <code>/send @username сумма</code>\n"
                "Или <code>/send id_пользователя сумма</code>\n"
                "Или ответь (reply) на сообщение человека и напиши <code>/send сумма</code>"
            )
            return

        if receiver["user_id"] == sender["user_id"]:
            await message.reply("❗ Нельзя перевести деньги самому себе")
            return
    else:
        await message.reply(
            "❗ Использование: <code>/send @username сумма</code>\n"
            "Или <code>/send id_пользователя сумма</code>\n"
            "Или ответь (reply) на сообщение человека и напиши <code>/send сумма</code>"
        )
        return

    if amount <= 0:
        await message.reply("❗ Сумма должна быть положительным целым числом")
        return

    if amount > sender["balance"]:
        await message.reply(f"❗ Недостаточно средств. Твой баланс: {fmt_money(sender['balance'])}")
        return

    if receiver["banned"]:
        await message.reply("❗ Счёт получателя заблокирован, перевод невозможен")
        return

    change_balance(sender["user_id"], -amount)
    change_balance(receiver["user_id"], amount)
    log_transaction(sender["user_id"], receiver["user_id"], amount)

    receiver_label = f"@{receiver['username']}" if receiver["username"] else receiver["full_name"]

    await message.reply(
        f"✅ Перевод выполнен!\n"
        f"Отправлено: <b>{fmt_money(amount)}</b> пользователю {receiver_label}\n"
        f"Новый баланс: <b>{fmt_money(sender['balance'] - amount)}</b>"
    )

    try:
        sender_label = f"@{sender['username']}" if sender["username"] else sender["full_name"]
        await bot.send_message(
            receiver["user_id"], f"💸 Тебе перевели <b>{fmt_money(amount)}</b> от {sender_label}!"
        )
    except Exception:
        pass  # получатель мог заблокировать бота / не писал ему в личку


@dp.message(Command("hist"))
async def cmd_history(message: Message):
    user = get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    rows = get_history(user["user_id"])

    if not rows:
        await message.reply("📭 У тебя пока нет операций")
        return

    lines = ["📜 <b>Последние операции:</b>\n"]
    for r in rows:
        date = r["created_at"][:16].replace("T", " ")
        if r["from_id"] == user["user_id"]:
            to_name = r["to_username"] or "неизвестно"
            lines.append(f"➖ {date} — отправлено {r['amount']} → @{to_name}")
        else:
            from_name = r["from_username"] or "администратор"
            lines.append(f"➕ {date} — получено {r['amount']} ← @{from_name}")

    await message.reply("\n".join(lines))


@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    user = get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    if user["banned"]:
        await message.reply("🚫 Твой счёт заблокирован администратором.")
        return

    now = datetime.utcnow()
    if user["last_daily"]:
        last = datetime.fromisoformat(user["last_daily"])
        elapsed = (now - last).total_seconds()
        if elapsed < 24 * 3600:
            remaining = 24 * 3600 - elapsed
            hours = int(remaining // 3600)
            minutes = int((remaining % 3600) // 60)
            await message.reply(
                f"⏳ Бонус уже получен. Следующий будет доступен через {hours} ч {minutes} мин."
            )
            return

    # 5% шанс на джекпот, иначе обычный разброс 50–200
    if random.random() < 0.05:
        amount = random.randint(500, 1000)
        text = f"🎰 <b>ДЖЕКПОТ!</b> Лядов Банк расщедрился: +{fmt_money(amount)} 💰"
    else:
        amount = random.randint(50, 200)
        text = f"🎁 Ежедневный бонус от Лядов Банка: +{fmt_money(amount)} 💰"

    claim_daily(user["user_id"], amount)
    await message.reply(f"{text}\nПриходи завтра за новым бонусом!")


@dp.message(Command("top"))
async def cmd_top(message: Message):
    rows = get_top()
    if not rows:
        await message.reply("Пока никто не зарегистрирован")
        return

    lines = ["🏆 <b>Топ пользователей:</b>\n"]
    for i, r in enumerate(rows, start=1):
        name = f"@{r['username']}" if r["username"] else r["full_name"]
        lines.append(f"{i}. {name} — {fmt_money(r['balance'])} 💰")

    await message.reply("\n".join(lines))


# ==================== АДМИН-КОМАНДЫ ====================


@dp.message(Command("add"))
async def cmd_addmoney(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    parsed = parse_username_and_amount(args)
    if parsed is None or parsed[1] <= 0:
        await message.reply("❗ Использование: <code>/add @username сумма</code>")
        return

    username, amount = parsed
    receiver = get_user_by_username(username)
    if receiver is None:
        await message.reply(f"❗ Пользователь @{username} не найден в базе")
        return

    change_balance(receiver["user_id"], amount)
    log_transaction(None, receiver["user_id"], amount)
    await message.reply(f"✅ Начислено {fmt_money(amount)} пользователю @{username}")

    try:
        await bot.send_message(
            receiver["user_id"], f"💰 Администратор начислил тебе <b>{fmt_money(amount)}</b>!"
        )
    except Exception:
        pass


@dp.message(Command("rem"))
async def cmd_removemoney(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    parsed = parse_username_and_amount(args)
    if parsed is None or parsed[1] <= 0:
        await message.reply("❗ Использование: <code>/rem @username сумма</code>")
        return

    username, amount = parsed
    receiver = get_user_by_username(username)
    if receiver is None:
        await message.reply(f"❗ Пользователь @{username} не найден в базе")
        return

    change_balance(receiver["user_id"], -amount)
    log_transaction(receiver["user_id"], None, amount)
    await message.reply(f"✅ Списано {fmt_money(amount)} у пользователя @{username}")


@dp.message(Command("set"))
async def cmd_setbalance(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    parsed = parse_username_and_amount(args)
    if parsed is None or parsed[1] < 0:
        await message.reply("❗ Использование: <code>/set @username сумма</code>")
        return

    username, amount = parsed
    receiver = get_user_by_username(username)
    if receiver is None:
        await message.reply(f"❗ Пользователь @{username} не найден в базе")
        return

    set_balance(receiver["user_id"], amount)
    await message.reply(f"✅ Баланс @{username} установлен: {fmt_money(amount)}")


@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    if len(args) != 1 or not args[0].startswith("@"):
        await message.reply("❗ Использование: <code>/ban @username</code>")
        return

    target = get_user_by_username(args[0])
    if target is None:
        await message.reply(f"❗ Пользователь {args[0]} не найден в базе")
        return

    set_banned(target["user_id"], True)
    await message.reply(f"🚫 Пользователь @{target['username']} заблокирован")


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    if len(args) != 1 or not args[0].startswith("@"):
        await message.reply("❗ Использование: <code>/unban @username</code>")
        return

    target = get_user_by_username(args[0])
    if target is None:
        await message.reply(f"❗ Пользователь {args[0]} не найден в базе")
        return

    set_banned(target["user_id"], False)
    await message.reply(f"✅ Пользователь @{target['username']} разблокирован")


@dp.message(Command("users"))
async def cmd_users(message: Message):
    if not is_admin(message.from_user.id):
        return

    rows = get_all_users()
    if not rows:
        await message.reply("Пользователей пока нет")
        return

    lines = [f"👥 <b>Всего пользователей: {len(rows)}</b>\n"]
    for r in rows[:50]:
        name = f"@{r['username']}" if r["username"] else r["full_name"]
        status = " 🚫" if r["banned"] else ""
        lines.append(f"{name} — {fmt_money(r['balance'])}{status} (id: {r['user_id']})")

    if len(rows) > 50:
        lines.append(f"\n… и ещё {len(rows) - 50}")

    await message.reply("\n".join(lines))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    s = get_stats()
    await message.reply(
        f"📊 <b>Статистика банка</b>\n\n"
        f"Пользователей: {s['users']}\n"
        f"Заблокировано: {s['banned']}\n"
        f"Денег в системе: {fmt_money(s['total_money'])}\n"
        f"Всего операций: {s['transactions']}\n"
        f"Сумма переводов: {fmt_money(s['transferred_total'])}"
    )


@dp.message(Command("bc"))
async def cmd_broadcast(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("❗ Использование: <code>/bc текст сообщения</code>")
        return

    text = parts[1]
    rows = get_all_users()

    sent, failed = 0, 0
    status_msg = await message.reply(f"⏳ Рассылка запущена ({len(rows)} получателей)...")

    for r in rows:
        try:
            await bot.send_message(r["user_id"], f"📢 <b>Объявление:</b>\n\n{text}")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # чтобы не упереться в лимиты Telegram

    await status_msg.edit_text(f"✅ Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}")


# ==================== ГРУППЫ: ПРИВЕТСТВИЕ ПРИ ДОБАВЛЕНИИ БОТА ====================


@dp.message(F.new_chat_members)
async def on_bot_added_to_group(message: Message):
    for member in message.new_chat_members:
        if member.id == bot.id:
            await message.answer(
                "👋 Привет! Это <b>Лядов Банк</b> 🏦\n"
                "Используйте /start чтобы открыть счёт, /bal — баланс, "
                "/send @username сумма (или ответом на сообщение) — перевод, "
                "/help — все команды.\n\n"
                "⚠️ Чтобы я нормально видел команды в группе, у меня должен быть выключен "
                "Group Privacy Mode (настраивается у @BotFather → Bot Settings → Group Privacy → Turn off)."
            )


# ==================== ЗАПУСК ====================


async def main():
    init_db()
    me = await bot.get_me()
    await setup_commands()
    logging.info(f"Бот запущен: @{me.username}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
