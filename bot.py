# -*- coding: utf-8 -*-
"""VPN-бот для Telegram (всё в одном файле). Запуск: python bot.py
Требуется: Python 3.10+ и пакет aiogram>=3.4.0
"""
import asyncio
import html
import logging
import os
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from math import ceil
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BASE_DIR = Path(__file__).resolve().parent



# ============================================================
# НАСТРОЙКИ
# ============================================================

try:  # .env необязателен: на хостинге можно задать переменные окружения в панели
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

# Основные настройки. Сначала берутся из переменных окружения хостинга (или .env),
# если их нет — используются значения в кавычках справа от "or".
BOT_TOKEN = os.getenv("BOT_TOKEN") or ""            # <-- можно вписать токен бота: "123456:ABC..."
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)          # <-- можно вписать свой Telegram ID цифрами

if not BOT_TOKEN or BOT_TOKEN.startswith("сюда_"):
    raise SystemExit("Не задан BOT_TOKEN. Впишите его в bot.py или в переменные окружения")

# Платежная система (ЮKassa через @BotFather -> Payments)
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN") or ""  # <-- токен ЮKassa от BotFather

# RollyPay (СБП / карта / крипта). API-ключ берётся в личном кабинете rollypay.io
ROLLYPAY_API_KEY = os.getenv("ROLLYPAY_API_KEY") or ""  # <-- можно вписать ключ сюда
RP_METHODS = {"sbp": "🏦 СБП", "card": "💳 Банковская карта", "usdt": "🪙 Криптовалюта (USDT)"}
RP_PAYMENT_TTL = 24 * 3600  # сколько часов ждём оплату по созданному счёту
CURRENCY = "RUB"
# Настройки VPN и подписок
VPN_SUB_BASE_URL = "https://sub.yourdomain.ru/sub/"  # Базовый URL вашей панели (Remnawave / 3x-ui)
MAX_DEVICES = 3
LOCATIONS_TEXT = "🇩🇪 Германия (1 Гбит/с) • 🇳🇱 Нидерланды (500 Мбит/с) • 🇺🇸 США"

# Тарифная сетка: ключ -> (название, дней, цена в рублях)
TARIFFS = {
    "1m": ("1 месяц", 30, 189),
    "3m": ("3 месяца (-20%)", 90, 450),
    "6m": ("6 месяцев (-30%)", 180, 800),
    "12m": ("1 год (ХИТ 🔥)", 365, 1490),
}

# Бонусы и пробный период
TRIAL_DAYS = 3
REF_BONUS_TRIAL_DAYS = 2     # За активацию триала другом
REF_BONUS_BUY_DAYS = 10      # За первую оплату друга

# Ссылки на проекты и поддержку
SUPPORT_URL = "https://t.me/shamerk"
SUPPORT_USERNAME = "@shamerk"        # показывается текстом в боте
SUPPORT_EMAIL = "support@yourdomain.ru"   # почта для обратной связи
REVIEWS_URL = "https://t.me/shamerkvpn_rewievs"

# Юридические документы (должны быть всегда доступны пользователям)
PRIVACY_URL = "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99"
TERMS_URL = "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15"

# Ссылки на приложения для инструкций
APP_LINKS = {
    "ios": "https://apps.apple.com/app/id6504287215",          # Happ / v2rayTun
    "android": "https://play.google.com/store/apps/details?id=com.v2raytun.android",
    "windows": "https://github.com/hiddify/hiddify-next/releases/latest",
    "macos": "https://apps.apple.com/app/id6504287215",
}


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

DB_PATH = str(BASE_DIR / "vpn_bot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with closing(get_conn()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                sub_token TEXT UNIQUE,
                sub_end INTEGER NOT NULL DEFAULT 0,
                trial_used INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                ref_trial_rewarded INTEGER NOT NULL DEFAULT 0,
                ref_buy_rewarded INTEGER NOT NULL DEFAULT 0,
                ref_count INTEGER NOT NULL DEFAULT 0,
                ref_days_earned INTEGER NOT NULL DEFAULT 0,
                exp_notified INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tariff_id TEXT NOT NULL,
                days INTEGER NOT NULL,
                amount_rub REAL NOT NULL,
                payment_method TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'paid',
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promocodes (
                code TEXT PRIMARY KEY,
                days INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_uses (
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                PRIMARY KEY (user_id, code)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rolly_payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                tariff_id TEXT NOT NULL,
                amount_rub REAL NOT NULL,
                method TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()


# ---------- Платежи RollyPay ----------

def create_rolly_payment(payment_id: str, user_id: int, tariff_id: str, amount: float, method: str):
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO rolly_payments (payment_id, user_id, tariff_id, amount_rub, method, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (payment_id, user_id, tariff_id, amount, method, int(time.time())),
        )
        conn.commit()


def get_rolly_payment(payment_id: str) -> dict | None:
    with closing(get_conn()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM rolly_payments WHERE payment_id = ?", (payment_id,)).fetchone()
        return dict(row) if row else None


def get_pending_rolly_payments() -> list[dict]:
    with closing(get_conn()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM rolly_payments WHERE status = 'pending'").fetchall()
        return [dict(r) for r in rows]


def set_rolly_status(payment_id: str, new_status: str) -> bool:
    """Меняет статус только у ещё не закрытого платежа. True — если именно этот вызов его закрыл
    (защита от двойного начисления дней)."""
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "UPDATE rolly_payments SET status = ? WHERE payment_id = ? AND status = 'pending'",
            (new_status, payment_id),
        )
        conn.commit()
        return cur.rowcount == 1


# ---------- Пользователи и подписки ----------

def register_user(user_id: int, username: str, referrer_id: int | None = None) -> bool:
    """Регистрирует пользователя. Возвращает True, если пользователь новый."""
    now = int(time.time())
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if cur.fetchone():
            conn.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            conn.commit()
            return False

        token = secrets.token_urlsafe(12)
        valid_ref = referrer_id if (referrer_id and referrer_id != user_id) else None

        conn.execute(
            """
            INSERT INTO users (user_id, username, sub_token, referred_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, username, token, valid_ref, now),
        )
        if valid_ref:
            conn.execute(
                "UPDATE users SET ref_count = ref_count + 1 WHERE user_id = ?",
                (valid_ref,),
            )
        conn.commit()
        return True


def get_user(user_id: int) -> dict | None:
    with closing(get_conn()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def add_subscription_days(user_id: int, days: int) -> int:
    """Добавляет дни к текущей подписке (или от текущего момента, если она истекла).
    Возвращает новую дату окончания (Unix timestamp)."""
    now = int(time.time())
    seconds_to_add = days * 86400
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT sub_end FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        current_end = row[0] if row else 0
        base_time = max(now, current_end)
        new_end = base_time + seconds_to_add

        conn.execute(
            "UPDATE users SET sub_end = ?, exp_notified = 0 WHERE user_id = ?",
            (new_end, user_id),
        )
        conn.commit()
        return new_end


def activate_trial(user_id: int, trial_days: int, ref_bonus_days: int) -> tuple[bool, int | None]:
    """Активирует триал. Возвращает (успех, id_реферера_для_уведомления)."""
    user = get_user(user_id)
    if not user or user["trial_used"]:
        return False, None

    add_subscription_days(user_id, trial_days)
    rewarded_referrer = None

    with closing(get_conn()) as conn:
        conn.execute("UPDATE users SET trial_used = 1 WHERE user_id = ?", (user_id,))
        if user["referred_by"] and not user["ref_trial_rewarded"]:
            ref_id = user["referred_by"]
            conn.execute(
                "UPDATE users SET ref_trial_rewarded = 1 WHERE user_id = ?", (user_id,)
            )
            conn.execute(
                "UPDATE users SET ref_days_earned = ref_days_earned + ? WHERE user_id = ?",
                (ref_bonus_days, ref_id),
            )
            rewarded_referrer = ref_id
        conn.commit()

    if rewarded_referrer:
        add_subscription_days(rewarded_referrer, ref_bonus_days)

    return True, rewarded_referrer


def record_order_and_reward_referrer(
    user_id: int, tariff_id: str, days: int, amount_rub: float, method: str, ref_buy_days: int
) -> int | None:
    """Сохраняет заказ, начисляет дни и вознаграждает реферера за первую покупку."""
    now = int(time.time())
    add_subscription_days(user_id, days)
    user = get_user(user_id)
    rewarded_referrer = None

    with closing(get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO orders (user_id, tariff_id, days, amount_rub, payment_method, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, tariff_id, days, amount_rub, method, now),
        )
        if user and user["referred_by"] and not user["ref_buy_rewarded"]:
            ref_id = user["referred_by"]
            conn.execute("UPDATE users SET ref_buy_rewarded = 1 WHERE user_id = ?", (user_id,))
            conn.execute(
                "UPDATE users SET ref_days_earned = ref_days_earned + ? WHERE user_id = ?",
                (ref_buy_days, ref_id),
            )
            rewarded_referrer = ref_id
        conn.commit()

    if rewarded_referrer:
        add_subscription_days(rewarded_referrer, ref_buy_days)

    return rewarded_referrer


# ---------- Промокоды ----------

def create_promocode(code: str, days: int, max_uses: int):
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO promocodes (code, days, max_uses, used_count) VALUES (?, ?, ?, 0)",
            (code.upper(), days, max_uses),
        )
        conn.commit()


def redeem_promocode(user_id: int, code: str) -> tuple[bool, str, int]:
    """Возвращает (успех, сообщение, начислено_дней)."""
    code = code.strip().upper()
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "SELECT days, max_uses, used_count FROM promocodes WHERE code = ?", (code,)
        )
        promo = cur.fetchone()
        if not promo:
            return False, "❌ Такого промокода не существует.", 0

        days, max_uses, used_count = promo
        if used_count >= max_uses:
            return False, "❌ Лимит активаций этого промокода исчерпан.", 0

        cur = conn.execute(
            "SELECT 1 FROM promo_uses WHERE user_id = ? AND code = ?", (user_id, code)
        )
        if cur.fetchone():
            return False, "⚠️ Вы уже активировали этот промокод ранее.", 0

        conn.execute("INSERT INTO promo_uses (user_id, code) VALUES (?, ?)", (user_id, code))
        conn.execute(
            "UPDATE promocodes SET used_count = used_count + 1 WHERE code = ?", (code,)
        )
        conn.commit()

    add_subscription_days(user_id, days)
    return True, f"🎉 Промокод активирован! Вам начислено +{days} дн. подписки.", days


# ---------- Уведомления и Админка ----------

def get_expiring_users() -> list[tuple[int, int]]:
    """Ищет пользователей, у которых подписка истекает менее чем через 24 часа."""
    now = int(time.time())
    soon = now + 86400
    with closing(get_conn()) as conn:
        cur = conn.execute(
            """
            SELECT user_id, sub_end FROM users
            WHERE sub_end > ? AND sub_end <= ? AND exp_notified = 0
            """,
            (now, soon),
        )
        rows = cur.fetchall()
        for uid, _ in rows:
            conn.execute("UPDATE users SET exp_notified = 1 WHERE user_id = ?", (uid,))
        conn.commit()
        return rows


def get_all_user_ids() -> list[int]:
    with closing(get_conn()) as conn:
        return [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]


def get_stats() -> dict:
    now = int(time.time())
    day_ago = now - 86400
    with closing(get_conn()) as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_subs = conn.execute(
            "SELECT COUNT(*) FROM users WHERE sub_end > ?", (now,)
        ).fetchone()[0]
        trials_used = conn.execute(
            "SELECT COUNT(*) FROM users WHERE trial_used = 1"
        ).fetchone()[0]
        orders_cnt, total_rev = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_rub), 0) FROM orders"
        ).fetchone()
        today_cnt, today_rev = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_rub), 0) FROM orders WHERE created_at >= ?",
            (day_ago,),
        ).fetchone()

        return {
            "total_users": total_users,
            "active_subs": active_subs,
            "trials_used": trials_used,
            "orders_cnt": orders_cnt,
            "total_rev": total_rev,
            "today_cnt": today_cnt,
            "today_rev": today_rev,
        }


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_menu_kb(trial_available: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if trial_available:
        builder.row(
            InlineKeyboardButton(
                text=f"🎁 Попробовать бесплатно ({TRIAL_DAYS} дня)", callback_data="get_trial"
            )
        )
    builder.row(
        InlineKeyboardButton(text="🔑 Мой VPN", callback_data="my_vpn"),
        InlineKeyboardButton(text="💳 Купить / Продлить", callback_data="tariffs"),
    )
    builder.row(
        InlineKeyboardButton(text="📱 Инструкция", callback_data="guides"),
        InlineKeyboardButton(text="👥 Пригласить друга", callback_data="referrals"),
    )
    builder.row(
        InlineKeyboardButton(text="🎟 Промокод", callback_data="enter_promo"),
        InlineKeyboardButton(text="💬 Отзывы", url=REVIEWS_URL),
    )
    builder.row(
        InlineKeyboardButton(text="🛟 Поддержка", url=SUPPORT_URL),
        InlineKeyboardButton(text="📄 Документы", callback_data="legal"),
    )
    builder.row(
        InlineKeyboardButton(text="🔒 Политика", url=PRIVACY_URL),
        InlineKeyboardButton(text="📜 Соглашение", url=TERMS_URL),
    )
    return builder.as_markup()


def legal_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=PRIVACY_URL))
    builder.row(InlineKeyboardButton(text="📜 Пользовательское соглашение (оферта)", url=TERMS_URL))
    builder.row(InlineKeyboardButton(text="🛟 Написать в поддержку", url=SUPPORT_URL))
    builder.row(InlineKeyboardButton(text="◀ В главное меню", callback_data="main_menu"))
    return builder.as_markup()


def back_to_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="◀ В главное меню", callback_data="main_menu")
    return builder.as_markup()


def my_vpn_kb(sub_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🌐 Открыть страницу подписки", url=sub_url))
    builder.row(
        InlineKeyboardButton(text="📱 Как подключить", callback_data="guides"),
        InlineKeyboardButton(text="⏳ Продлить доступ", callback_data="tariffs"),
    )
    builder.row(InlineKeyboardButton(text="◀ В главное меню", callback_data="main_menu"))
    return builder.as_markup()


def tariffs_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for t_id, (title, days, price) in TARIFFS.items():
        builder.button(
            text=f"⚡ {title} — {price}₽",
            callback_data=f"select_tariff:{t_id}",
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="◀ В главное меню", callback_data="main_menu"))
    return builder.as_markup()


def payment_methods_kb(tariff_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if ROLLYPAY_API_KEY:
        for method, label in RP_METHODS.items():
            builder.button(text=label, callback_data=f"pay_rp:{tariff_id}:{method}")
    if PAYMENTS_PROVIDER_TOKEN:
        builder.button(text="💳 Картой РФ / СБП (ЮKassa)", callback_data=f"pay_card:{tariff_id}")
    builder.button(text="📜 Пользовательское соглашение", url=TERMS_URL)
    builder.button(text="🔒 Политика конфиденциальности", url=PRIVACY_URL)
    builder.button(text="◀ Назад к тарифам", callback_data="tariffs")
    builder.adjust(1)
    return builder.as_markup()


def rolly_pay_kb(pay_url: str, payment_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Перейти к оплате", url=pay_url)
    builder.button(text="✅ Я оплатил — проверить", callback_data=f"chk_rp:{payment_id}")
    builder.button(text="◀ В главное меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def guides_os_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🍏 iPhone / iPad (iOS)", callback_data="guide_os:ios")
    builder.button(text="🤖 Android", callback_data="guide_os:android")
    builder.button(text="💻 Windows", callback_data="guide_os:windows")
    builder.button(text="🖥 macOS", callback_data="guide_os:macos")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="🔑 Перейти в Мой VPN", callback_data="my_vpn"))
    builder.row(InlineKeyboardButton(text="◀ В главное меню", callback_data="main_menu"))
    return builder.as_markup()


def os_instruction_kb(os_name: str, sub_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="1️⃣ Скачать приложение", url=APP_LINKS.get(os_name, SUPPORT_URL))
    )
    builder.row(InlineKeyboardButton(text="2️⃣ Скопировать/Открыть подписку", url=sub_url))
    builder.row(InlineKeyboardButton(text="◀ К выбору устройства", callback_data="guides"))
    return builder.as_markup()


def admin_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Обновить статистику", callback_data="admin_stats")
    builder.button(text="📢 Сделать рассылку", callback_data="admin_broadcast")
    builder.button(text="🎟 Создать промокод", callback_data="admin_create_promo")
    builder.button(text="🎁 Выдать дни по ID", callback_data="admin_gift_days")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="◀ В главное меню", callback_data="main_menu"))
    return builder.as_markup()


# ============================================================
# ОБРАБОТЧИКИ
# ============================================================

log = logging.getLogger(__name__)
router = Router()


class UserStates(StatesGroup):
    waiting_promo = State()


class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_promo_data = State()
    waiting_gift_data = State()


# ---------- Вспомогательные функции ----------

def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def days_left(sub_end: int) -> int:
    return max(0, ceil((sub_end - int(time.time())) / 86400))


def sub_url(user: dict) -> str:
    return f"{VPN_SUB_BASE_URL}{user['sub_token']}"


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def show(call: CallbackQuery, text: str, markup=None):
    """Редактирует сообщение, а если нельзя — отправляет новое."""
    try:
        await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await call.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def safe_send(bot: Bot, user_id: int, text: str) -> bool:
    try:
        await bot.send_message(user_id, text)
        return True
    except Exception as e:  # пользователь заблокировал бота и т.п.
        log.warning("Не удалось отправить сообщение %s: %s", user_id, e)
        return False


def legal_footer() -> str:
    return (
        f'Продолжая, вы принимаете <a href="{TERMS_URL}">Пользовательское соглашение</a> '
        f'и <a href="{PRIVACY_URL}">Политику конфиденциальности</a>.'
    )


def contacts_text() -> str:
    return (
        "🛟 <b>Поддержка</b>\n"
        f"Telegram: {SUPPORT_USERNAME}\n"
        f"Email: {SUPPORT_EMAIL}\n\n"
        "Отвечаем на вопросы по подключению, оплате и возвратам."
    )


def legal_text() -> str:
    return (
        "📄 <b>Документы и контакты</b>\n\n"
        f'🔒 <a href="{PRIVACY_URL}">Политика конфиденциальности</a>\n'
        f'📜 <a href="{TERMS_URL}">Пользовательское соглашение (публичная оферта)</a>\n\n'
        + contacts_text()
    )


def main_menu_text(user: dict) -> str:
    now = int(time.time())
    if user["sub_end"] > now:
        status = f"✅ Активна до <b>{fmt_date(user['sub_end'])}</b> (осталось {days_left(user['sub_end'])} дн.)"
    else:
        status = "❌ Подписка не активна"
    return (
        "🔐 <b>Добро пожаловать в VPN!</b>\n\n"
        f"📍 Локации: {LOCATIONS_TEXT}\n"
        f"📱 Устройств в подписке: до {MAX_DEVICES}\n\n"
        f"Статус: {status}\n\n"
        + legal_footer()
    )


async def send_main_menu(target: Message | CallbackQuery, user_id: int):
    user = get_user(user_id)
    text = main_menu_text(user)
    markup = main_menu_kb(trial_available=not user["trial_used"])
    if isinstance(target, CallbackQuery):
        await show(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


# ---------- /start и главное меню ----------

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    referrer_id = None
    if command.args and command.args.startswith("ref_"):
        try:
            referrer_id = int(command.args[4:])
        except ValueError:
            pass
    if referrer_id and not get_user(referrer_id):
        referrer_id = None

    is_new = register_user(
        message.from_user.id, message.from_user.username or message.from_user.full_name, referrer_id
    )
    if is_new and referrer_id and referrer_id != message.from_user.id:
        await safe_send(bot, referrer_id, "👥 По вашей ссылке зарегистрировался новый пользователь!")

    await send_main_menu(message, message.from_user.id)


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    register_user(call.from_user.id, call.from_user.username or call.from_user.full_name)
    await send_main_menu(call, call.from_user.id)
    await call.answer()


# ---------- Пробный период ----------

@router.callback_query(F.data == "get_trial")
async def cb_trial(call: CallbackQuery, bot: Bot):
    ok, ref_id = activate_trial(call.from_user.id, TRIAL_DAYS, REF_BONUS_TRIAL_DAYS)
    if not ok:
        await call.answer("Пробный период уже был использован.", show_alert=True)
        await send_main_menu(call, call.from_user.id)
        return
    if ref_id:
        await safe_send(
            bot, ref_id, f"🎁 Ваш друг активировал пробный период! Вам начислено +{REF_BONUS_TRIAL_DAYS} дн."
        )
    user = get_user(call.from_user.id)
    await show(
        call,
        f"🎉 <b>Пробный период на {TRIAL_DAYS} дн. активирован!</b>\n\n"
        f"Ваша подписка:\n<code>{sub_url(user)}</code>\n\n"
        "Нажмите «Как подключить», чтобы настроить устройство.",
        my_vpn_kb(sub_url(user)),
    )
    await call.answer()


# ---------- Мой VPN ----------

@router.callback_query(F.data == "my_vpn")
async def cb_my_vpn(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user:
        register_user(call.from_user.id, call.from_user.username or call.from_user.full_name)
        user = get_user(call.from_user.id)
    now = int(time.time())
    if user["sub_end"] > now:
        status = f"✅ Активна до <b>{fmt_date(user['sub_end'])}</b> (осталось {days_left(user['sub_end'])} дн.)"
    else:
        status = "❌ Подписка не активна. Оформите тариф или возьмите пробный период."
    await show(
        call,
        f"🔑 <b>Мой VPN</b>\n\n{status}\n\n"
        f"Ваша ссылка-подписка (нажмите, чтобы скопировать):\n<code>{sub_url(user)}</code>\n\n"
        f"📱 Устройств: до {MAX_DEVICES}",
        my_vpn_kb(sub_url(user)),
    )
    await call.answer()


# ---------- Документы и поддержка ----------

@router.callback_query(F.data == "legal")
async def cb_legal(call: CallbackQuery):
    await show(call, legal_text(), legal_kb())
    await call.answer()


@router.message(Command("terms", "privacy", "docs"))
async def cmd_docs(message: Message):
    await message.answer(legal_text(), reply_markup=legal_kb(), disable_web_page_preview=True)


@router.message(Command("support", "help"))
async def cmd_support(message: Message):
    await message.answer(contacts_text(), reply_markup=legal_kb(), disable_web_page_preview=True)


# ---------- Тарифы и оплата ----------

@router.callback_query(F.data == "tariffs")
async def cb_tariffs(call: CallbackQuery):
    await show(call, "💳 <b>Выберите тариф:</b>", tariffs_kb())
    await call.answer()


@router.callback_query(F.data.startswith("select_tariff:"))
async def cb_select_tariff(call: CallbackQuery):
    tariff_id = call.data.split(":", 1)[1]
    if tariff_id not in TARIFFS:
        await call.answer("Тариф не найден", show_alert=True)
        return
    title, days, price = TARIFFS[tariff_id]
    await show(
        call,
        f"🛒 <b>Тариф:</b> {title}\n📅 Срок: {days} дн.\n💰 Цена: <b>{price} ₽</b>\n\n"
        "Выберите способ оплаты.\n" + legal_footer(),
        payment_methods_kb(tariff_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pay_card:"))
async def cb_pay_card(call: CallbackQuery):
    tariff_id = call.data.split(":", 1)[1]
    if tariff_id not in TARIFFS:
        await call.answer("Тариф не найден", show_alert=True)
        return
    if not PAYMENTS_PROVIDER_TOKEN or PAYMENTS_PROVIDER_TOKEN.startswith("сюда_"):
        await call.answer("Оплата временно недоступна. Обратитесь в поддержку.", show_alert=True)
        return
    title, days, price = TARIFFS[tariff_id]
    await call.message.answer_invoice(
        title=f"VPN — {title}",
        description=f"Подписка на {days} дн. Доступ на {MAX_DEVICES} устройства.",
        payload=tariff_id,
        provider_token=PAYMENTS_PROVIDER_TOKEN,
        currency=CURRENCY,
        prices=[LabeledPrice(label=title, amount=price * 100)],
    )
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    ok = query.invoice_payload in TARIFFS
    await query.answer(ok=ok, error_message=None if ok else "Тариф не найден, попробуйте снова.")


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot):
    pay = message.successful_payment
    tariff_id = pay.invoice_payload
    if tariff_id not in TARIFFS:
        log.error("Оплата с неизвестным тарифом: %s", pay)
        await message.answer("Платёж получен, но тариф не распознан. Напишите в поддержку.")
        return
    title, days, _ = TARIFFS[tariff_id]
    ref_id = record_order_and_reward_referrer(
        message.from_user.id, tariff_id, days, pay.total_amount / 100, "yookassa", REF_BONUS_BUY_DAYS
    )
    if ref_id:
        await safe_send(
            bot, ref_id, f"💸 Ваш друг оформил подписку! Вам начислено +{REF_BONUS_BUY_DAYS} дн."
        )
    user = get_user(message.from_user.id)
    await message.answer(
        f"✅ <b>Оплата прошла успешно!</b>\n\nТариф: {title}\n"
        f"Подписка активна до <b>{fmt_date(user['sub_end'])}</b>",
        reply_markup=my_vpn_kb(sub_url(user)),
    )
    if ADMIN_ID:
        await safe_send(
            bot, ADMIN_ID,
            f"💰 Новая оплата: {pay.total_amount / 100:.0f} ₽, тариф {title}, "
            f"user_id <code>{message.from_user.id}</code>",
        )


# ---------- Оплата через RollyPay (без домена: статус проверяем сами) ----------

_rolly_client = None


def get_rolly_client():
    """Клиент создаётся при первом обращении, чтобы бот работал и без пакета rollypay."""
    global _rolly_client
    if _rolly_client is None and ROLLYPAY_API_KEY:
        from rollypay import RollyPayClient
        _rolly_client = RollyPayClient(api_key=ROLLYPAY_API_KEY)
    return _rolly_client


RP_FAILED_STATUSES = {"failed", "cancelled", "canceled", "expired", "rejected", "declined"}


async def check_rolly_payment(bot: Bot, p: dict) -> str:
    """Спрашивает у RollyPay статус платежа и при оплате начисляет подписку.
    Возвращает: 'paid' | 'pending' | 'failed'."""
    client = get_rolly_client()
    data = await asyncio.to_thread(client.payments.get, p["payment_id"])
    status = str(data.get("status", "")).lower()

    if status == "succeeded":
        if not set_rolly_status(p["payment_id"], "paid"):
            return "paid"  # уже начислено ранее
        title, days, _ = TARIFFS[p["tariff_id"]]
        ref_id = record_order_and_reward_referrer(
            p["user_id"], p["tariff_id"], days, p["amount_rub"], f"rollypay_{p['method']}", REF_BONUS_BUY_DAYS
        )
        user = get_user(p["user_id"])
        try:
            await bot.send_message(
                p["user_id"],
                f"✅ <b>Оплата прошла успешно!</b>\n\nТариф: {title}\n"
                f"Подписка активна до <b>{fmt_date(user['sub_end'])}</b>",
                reply_markup=my_vpn_kb(sub_url(user)),
            )
        except Exception as e:
            log.warning("Не удалось уведомить об оплате %s: %s", p["user_id"], e)
        if ref_id:
            await safe_send(bot, ref_id, f"💸 Ваш друг оформил подписку! Вам начислено +{REF_BONUS_BUY_DAYS} дн.")
        if ADMIN_ID:
            await safe_send(
                bot, ADMIN_ID,
                f"💰 Новая оплата (RollyPay, {p['method']}): {p['amount_rub']:.0f} ₽, тариф {title}, "
                f"user_id <code>{p['user_id']}</code>",
            )
        return "paid"

    if status in RP_FAILED_STATUSES:
        set_rolly_status(p["payment_id"], "failed")
        return "failed"
    return "pending"


@router.callback_query(F.data.startswith("pay_rp:"))
async def cb_pay_rollypay(call: CallbackQuery, bot: Bot):
    try:
        _, tariff_id, method = call.data.split(":")
    except ValueError:
        return await call.answer()
    client = get_rolly_client()
    if not client or tariff_id not in TARIFFS or method not in RP_METHODS:
        return await call.answer("Этот способ оплаты сейчас недоступен.", show_alert=True)

    title, days, price = TARIFFS[tariff_id]
    me = await bot.get_me()
    order_id = f"{call.from_user.id}-{tariff_id}-{int(time.time())}"
    try:
        payment = await asyncio.to_thread(
            client.payments.create,
            amount=f"{price:.2f}",
            order_id=order_id,
            payment_method=method,
            description=f"VPN — {title}",
            customer_id=str(call.from_user.id),
            redirect_url=f"https://t.me/{me.username}",
        )
        payment_id, pay_url = payment["payment_id"], payment["pay_url"]
    except Exception:
        log.exception("RollyPay: не удалось создать платёж")
        return await call.answer("Не удалось создать платёж. Попробуйте позже или другим способом.", show_alert=True)

    create_rolly_payment(payment_id, call.from_user.id, tariff_id, price, method)
    await call.message.answer(
        f"🧾 <b>Счёт создан</b>\n\nТариф: {title} ({days} дн.)\nСумма: <b>{price} ₽</b>\n"
        f"Способ: {RP_METHODS[method]}\n\n"
        "Нажмите «Перейти к оплате». После оплаты подписка активируется автоматически "
        "в течение минуты, либо нажмите «Я оплатил — проверить».",
        reply_markup=rolly_pay_kb(pay_url, payment_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("chk_rp:"))
async def cb_check_rollypay(call: CallbackQuery, bot: Bot):
    p = get_rolly_payment(call.data.split(":", 1)[1])
    if not p or p["user_id"] != call.from_user.id:
        return await call.answer("Платёж не найден.", show_alert=True)
    if p["status"] == "paid":
        return await call.answer("Этот платёж уже оплачен и начислен ✅", show_alert=True)
    if p["status"] == "failed":
        return await call.answer("Платёж отменён или истёк. Создайте новый.", show_alert=True)
    try:
        result = await check_rolly_payment(bot, p)
    except Exception:
        log.exception("RollyPay: ошибка проверки платежа")
        return await call.answer("Не удалось проверить платёж. Попробуйте через минуту.", show_alert=True)
    if result == "pending":
        await call.answer("Оплата пока не поступила. Если уже оплатили — подождите минуту.", show_alert=True)
    elif result == "failed":
        await call.answer("Платёж отменён или истёк. Создайте новый.", show_alert=True)
    else:
        await call.answer("Оплата получена ✅")


async def rolly_poller(bot: Bot):
    """Фоновая проверка неоплаченных счетов — вместо вебхука, поэтому домен не нужен."""
    tick = 0
    while True:
        tick += 1
        try:
            now = int(time.time())
            for p in get_pending_rolly_payments():
                age = now - p["created_at"]
                if age > RP_PAYMENT_TTL:
                    set_rolly_status(p["payment_id"], "failed")
                    continue
                # свежие счета проверяем каждые 20 сек, старше 30 минут — раз в 5 минут
                if age > 1800 and tick % 15 != 0:
                    continue
                try:
                    await check_rolly_payment(bot, p)
                except Exception:
                    log.exception("RollyPay: ошибка проверки %s", p["payment_id"])
                await asyncio.sleep(0.5)
        except Exception:
            log.exception("Ошибка в проверке платежей RollyPay")
        await asyncio.sleep(20)


# ---------- Инструкции ----------

@router.callback_query(F.data == "guides")
async def cb_guides(call: CallbackQuery):
    await show(call, "📱 <b>Выберите устройство:</b>", guides_os_kb())
    await call.answer()


GUIDE_TEXT = {
    "ios": "🍏 <b>iPhone / iPad</b>",
    "android": "🤖 <b>Android</b>",
    "windows": "💻 <b>Windows</b>",
    "macos": "🖥 <b>macOS</b>",
}


@router.callback_query(F.data.startswith("guide_os:"))
async def cb_guide_os(call: CallbackQuery):
    os_name = call.data.split(":", 1)[1]
    user = get_user(call.from_user.id)
    text = (
        f"{GUIDE_TEXT.get(os_name, os_name)}\n\n"
        "1️⃣ Скачайте приложение по кнопке ниже и установите его.\n"
        "2️⃣ Нажмите «Скопировать/Открыть подписку» — ссылка откроется в браузере, "
        "оттуда её можно добавить в приложение. Либо скопируйте ссылку:\n"
        f"<code>{sub_url(user)}</code>\n"
        "3️⃣ В приложении выберите «Добавить подписку из буфера обмена» (или по ссылке).\n"
        "4️⃣ Выберите локацию и нажмите «Подключить». Готово! 🚀"
    )
    await show(call, text, os_instruction_kb(os_name, sub_url(user)))
    await call.answer()


# ---------- Рефералы ----------

@router.callback_query(F.data == "referrals")
async def cb_referrals(call: CallbackQuery, bot: Bot):
    user = get_user(call.from_user.id)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
    await show(
        call,
        "👥 <b>Приглашайте друзей и получайте дни подписки!</b>\n\n"
        f"• Друг активировал пробный период — вам <b>+{REF_BONUS_TRIAL_DAYS} дн.</b>\n"
        f"• Друг оплатил первую подписку — вам <b>+{REF_BONUS_BUY_DAYS} дн.</b>\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{user['ref_count']}</b>\n"
        f"Заработано дней: <b>{user['ref_days_earned']}</b>",
        back_to_menu_kb(),
    )
    await call.answer()


# ---------- Промокоды ----------

@router.callback_query(F.data == "enter_promo")
async def cb_enter_promo(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_promo)
    await show(call, "🎟 Отправьте промокод одним сообщением:", back_to_menu_kb())
    await call.answer()


@router.message(UserStates.waiting_promo, F.text)
async def process_promo(message: Message, state: FSMContext):
    ok, text, _ = redeem_promocode(message.from_user.id, message.text)
    await state.clear()
    await message.answer(text)
    await send_main_menu(message, message.from_user.id)


# ---------- Админка ----------

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(stats_text(), reply_markup=admin_kb())


def stats_text() -> str:
    s = get_stats()
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{s['total_users']}</b>\n"
        f"✅ Активных подписок: <b>{s['active_subs']}</b>\n"
        f"🎁 Пробных использовано: <b>{s['trials_used']}</b>\n\n"
        f"🧾 Заказов всего: <b>{s['orders_cnt']}</b> на <b>{s['total_rev']:.0f} ₽</b>\n"
        f"📅 За 24 часа: <b>{s['today_cnt']}</b> на <b>{s['today_rev']:.0f} ₽</b>"
    )


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer()
    await show(call, stats_text(), admin_kb())
    await call.answer("Обновлено")


@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminStates.waiting_broadcast)
    await call.message.answer("📢 Отправьте текст рассылки (поддерживается HTML). /cancel — отмена.")
    await call.answer()


@router.message(AdminStates.waiting_broadcast, F.text)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    if message.text == "/cancel":
        return await message.answer("Отменено.")
    ids = get_all_user_ids()
    await message.answer(f"Начинаю рассылку на {len(ids)} пользователей…")
    sent = failed = 0
    for uid in ids:
        if await safe_send(bot, uid, message.text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)  # лимиты Telegram (~30 сообщений/сек)
    await message.answer(f"✅ Рассылка завершена. Доставлено: {sent}, ошибок: {failed}.")


@router.callback_query(F.data == "admin_create_promo")
async def cb_admin_create_promo(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminStates.waiting_promo_data)
    await call.message.answer(
        "🎟 Отправьте данные промокода в формате:\n<code>КОД ДНИ МАКС_АКТИВАЦИЙ</code>\n"
        "Например: <code>WELCOME 7 100</code>\n/cancel — отмена."
    )
    await call.answer()


@router.message(AdminStates.waiting_promo_data, F.text)
async def process_admin_promo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("Отменено.")
    try:
        code, days, max_uses = message.text.split()
        days, max_uses = int(days), int(max_uses)
        assert days > 0 and max_uses > 0
    except (ValueError, AssertionError):
        return await message.answer("❌ Неверный формат. Пример: <code>WELCOME 7 100</code>")
    create_promocode(code, days, max_uses)
    await state.clear()
    await message.answer(
        f"✅ Промокод <code>{code.upper()}</code> создан: +{days} дн., лимит {max_uses} активаций."
    )


@router.callback_query(F.data == "admin_gift_days")
async def cb_admin_gift(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminStates.waiting_gift_data)
    await call.message.answer(
        "🎁 Отправьте <code>ID_ПОЛЬЗОВАТЕЛЯ ДНИ</code>\nНапример: <code>123456789 30</code>\n/cancel — отмена."
    )
    await call.answer()


@router.message(AdminStates.waiting_gift_data, F.text)
async def process_admin_gift(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("Отменено.")
    try:
        uid, days = map(int, message.text.split())
        assert days != 0
    except (ValueError, AssertionError):
        return await message.answer("❌ Неверный формат. Пример: <code>123456789 30</code>")
    if not get_user(uid):
        return await message.answer("❌ Пользователь с таким ID не найден.")
    new_end = add_subscription_days(uid, days)
    await state.clear()
    await message.answer(f"✅ Пользователю {uid} выдано {days} дн. Подписка до {fmt_date(new_end)}.")
    await safe_send(bot, uid, f"🎁 Администратор начислил вам +{days} дн. подписки!")


# ============================================================
# ЗАПУСК
# ============================================================

EXPIRY_CHECK_INTERVAL = 600  # проверка истекающих подписок каждые 10 минут


async def expiry_notifier(bot: Bot):
    """Предупреждает пользователей, у которых подписка заканчивается менее чем через 24 часа."""
    while True:
        try:
            for user_id, _ in get_expiring_users():
                try:
                    await bot.send_message(
                        user_id,
                        "⏳ <b>Ваша подписка закончится менее чем через 24 часа.</b>\n"
                        "Продлите её в боте, чтобы не потерять доступ: /start → «Купить / Продлить».",
                    )
                except Exception as e:
                    logging.warning("Уведомление %s не доставлено: %s", user_id, e)
                await asyncio.sleep(0.05)
        except Exception:
            logging.exception("Ошибка в проверке подписок")
        await asyncio.sleep(EXPIRY_CHECK_INTERVAL)


async def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="support", description="Поддержка и контакты"),
            BotCommand(command="docs", description="Политика и соглашение"),
        ]
    )

    notifier = asyncio.create_task(expiry_notifier(bot))
    rolly_task = asyncio.create_task(rolly_poller(bot)) if ROLLYPAY_API_KEY else None
    me = await bot.get_me()
    logging.info("Бот @%s запущен", me.username)
    try:
        await dp.start_polling(bot)
    finally:
        notifier.cancel()
        if rolly_task:
            rolly_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
