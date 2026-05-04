"""Обработчики команд Telegram-бота."""
import logging
import os
import sys
import time
import json
import urllib.parse
import asyncio
from functools import wraps
from datetime import datetime
from zoneinfo import ZoneInfo

# Совместимость с Python 3.10: InvalidTimezoneError появился в 3.11
if sys.version_info >= (3, 11):
    from zoneinfo import InvalidTimezoneError
else:
    InvalidTimezoneError = KeyError  # type: ignore

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from calendar_api import get_upcoming_events, get_past_events, SCOPES
from database import (
    get_last_events,
    save_user_setting,
    get_user_setting,
    get_user_token,
    save_user_token,
    delete_user_token,
    reset_notified_for_window,
    save_user_timezone,
    get_user_timezone,
    parse_datetime_string,
    DEFAULT_TIMEZONE,
    save_event,
)
from google_auth_oauthlib.flow import InstalledAppFlow
from scheduler import mark_reminder_changed, notify_snooze_override

logger = logging.getLogger(__name__)

# Блокировки для предотвращения гонок при быстрой смене настроек
_reminder_locks: dict[int, asyncio.Lock] = {}


def get_reminder_lock(user_id: int) -> asyncio.Lock:
    """Возвращает или создаёт асинхронный лок для пользователя."""
    if user_id not in _reminder_locks:
        _reminder_locks[user_id] = asyncio.Lock()
    return _reminder_locks[user_id]


# Маппинг дружественных названий → IANA timezones
TZ_ALIASES = {
    'москва': 'Europe/Moscow', 'msk': 'Europe/Moscow', 'мск': 'Europe/Moscow',
    'екатеринбург': 'Asia/Yekaterinburg', 'ekb': 'Asia/Yekaterinburg', 'екб': 'Asia/Yekaterinburg',
    'челябинск': 'Asia/Yekaterinburg', 'chel': 'Asia/Yekaterinburg', 'чел': 'Asia/Yekaterinburg',
    'красноярск': 'Asia/Krasnoyarsk', 'krsk': 'Asia/Krasnoyarsk',
    'иркутск': 'Asia/Irkutsk', 'ikrt': 'Asia/Irkutsk',
    'владивосток': 'Asia/Vladivostok', 'vlad': 'Asia/Vladivostok',
    'калининград': 'Europe/Kaliningrad', 'kgd': 'Europe/Kaliningrad',
    'самара': 'Europe/Samara', 'kuf': 'Europe/Samara',
    'омск': 'Asia/Omsk', 'oms': 'Asia/Omsk',
    'новосибирск': 'Asia/Novosibirsk', 'nsk': 'Asia/Novosibirsk',
    'камчатка': 'Asia/Kamchatka', 'pkt': 'Asia/Kamchatka',
    'сахалин': 'Asia/Sakhalin',
    'киев': 'Europe/Kyiv', 'kyiv': 'Europe/Kyiv', 'kiev': 'Europe/Kyiv',
    'минск': 'Europe/Minsk', 'mns': 'Europe/Minsk',
    'алматы': 'Asia/Almaty', 'kz': 'Asia/Almaty', 'астана': 'Asia/Almaty',
    'ташкент': 'Asia/Tashkent', 'uz': 'Asia/Tashkent',
    'лондон': 'Europe/London', 'uk': 'Europe/London', 'gb': 'Europe/London',
    'берлин': 'Europe/Berlin', 'de': 'Europe/Berlin', 'germany': 'Europe/Berlin',
    'париж': 'Europe/Paris', 'fr': 'Europe/Paris', 'france': 'Europe/Paris',
    'варшава': 'Europe/Warsaw', 'pl': 'Europe/Warsaw', 'poland': 'Europe/Warsaw',
    'прага': 'Europe/Prague', 'cz': 'Europe/Prague',
    'рим': 'Europe/Rome', 'it': 'Europe/Rome', 'italy': 'Europe/Rome',
    'мадрид': 'Europe/Madrid', 'es': 'Europe/Madrid', 'spain': 'Europe/Madrid',
    'амстердам': 'Europe/Amsterdam', 'nl': 'Europe/Amsterdam',
    'стокгольм': 'Europe/Stockholm', 'se': 'Europe/Stockholm',
    'хельсинки': 'Europe/Helsinki', 'fi': 'Europe/Helsinki',
    'нью-йорк': 'America/New_York', 'ny': 'America/New_York', 'new york': 'America/New_York',
    'лос-анджелес': 'America/Los_Angeles', 'la': 'America/Los_Angeles', 'los angeles': 'America/Los_Angeles',
    'чикаго': 'America/Chicago', 'денвер': 'America/Denver',
    'торонто': 'America/Toronto', 'ca': 'America/Toronto',
    'дубай': 'Asia/Dubai', 'uae': 'Asia/Dubai', 'доха': 'Asia/Qatar',
    'тель-авив': 'Asia/Jerusalem', 'israel': 'Asia/Jerusalem',
    'сингапур': 'Asia/Singapore', 'sg': 'Asia/Singapore',
    'бангкок': 'Asia/Bangkok', 'th': 'Asia/Bangkok',
    'гоа': 'Asia/Kolkata', 'индия': 'Asia/Kolkata', 'india': 'Asia/Kolkata',
    'пекин': 'Asia/Shanghai', 'china': 'Asia/Shanghai',
    'токио': 'Asia/Tokyo', 'jp': 'Asia/Tokyo', 'japan': 'Asia/Tokyo',
    'сеул': 'Asia/Seoul', 'kr': 'Asia/Seoul',
    'сидней': 'Australia/Sydney', 'au': 'Australia/Sydney',
    'мельбурн': 'Australia/Melbourne',
    'utc': 'UTC', 'gmt': 'UTC', 'greenwich': 'UTC',
}


def resolve_timezone(user_input: str) -> str | None:
    """
    Распознаёт часовой пояс из ввода пользователя.
    Поддерживает алиасы (москва → Europe/Moscow) и прямые IANA-идентификаторы.
    """
    if not user_input:
        return None

    clean = user_input.strip().lower().replace('.', '').replace('-', ' ').replace('_', ' ')

    if clean in TZ_ALIASES:
        return TZ_ALIASES[clean]
        
    try:
        ZoneInfo(user_input.strip())
        return user_input.strip()
    except InvalidTimezoneError:  # type: ignore
        pass
        
    return None


# Настройки напоминаний
REMINDER_STEP = 5
REMINDER_MIN = 5
REMINDER_MAX = 1440
QUICK_REMINDER_VALUES = [5, 10, 15, 30, 60, 120]

# Rate limit хранилище
_rate_limit_store: dict[int, float] = {}


def rate_limit(seconds: int = 60):
    """Декоратор для ограничения частоты вызова команд."""
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            now = time.time()
            last_call = _rate_limit_store.get(user_id, 0)
            
            if now - last_call < seconds:
                remaining = int(seconds - (now - last_call))
                await update.message.reply_text(
                    f"⏳ Пожалуйста, подожди {remaining} сек. перед следующим запросом."
                )
                logger.debug("Rate limit для user_id=%d, команда=%s", user_id, func.__name__)
                return
                
            _rate_limit_store[user_id] = now
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start — приветствие и регистрация пользователя."""
    user = update.effective_user
    user_id = user.id
    await save_user_setting(user_id, reminder_minutes=15)

    logger.info("Пользователь %d (%s) зарегистрирован", user_id, user.first_name)

    await update.message.reply_html(
        f"👋 Привет, <b>{user.first_name}</b>!\n\n"
        "🤖 Я — бот для уведомлений из Google Calendar.\n\n"
        "<b>📋 Порядок настройки:</b>\n"
        "1️⃣ /auth — подключить Google Calendar\n"
        "2️⃣ /set_timezone — указать ваш часовой пояс (Должен совпадать с поясом Google Calendar!)\n"
        "3️⃣ /set_reminder — выбрать время напоминания\n\n"
        "<b>📅 Работа с календарём:</b>\n"
        "• /upcoming — предстоящие события на 24 часа\n"
        "• /history — история завершённых встреч\n\n"
        "ℹ️ <b>Важно:</b> уведомления приходят только по <b>Мероприятиям</b> Google Calendar. "
        "Задачи и расписания встреч не отслеживаются.\n\n"
        "<b>⚙️ Аккаунт и справка:</b>\n"
        "• /help — подробная справка\n"
        "• /delete_account — удалить все данные и выйти\n\n"
        "<i>✅ Вы зарегистрированы! Начните с подключения календаря: /auth</i>"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help — подробная справка."""
    await update.message.reply_html(
        "<b>📚 Справка по командам:</b>\n\n"
        "<b>🔐 Авторизация:</b>\n"
        "• /auth — получить ссылку для подключения Google Calendar\n"
        "• /auth_code <code>код</code> — завершить авторизацию кодом из браузера\n\n"
        "<b>⚙️ Настройки:</b>\n"
        "• /set_timezone <code>город/IANA</code> — установить часовой пояс (напр. Москва, Челябинск), (Должен совпадать с поясом Google Calendar!)\n"
        "• /set_reminder <code>[мин]</code> — за сколько минут напоминать (5–1440, кратно 5)\n\n"
        "<b>📅 Календарь:</b>\n"
        "• /upcoming — предстоящие события на 24 часа\n"
        "• /history — история завершённых встреч\n\n"
        "<b>👤 Аккаунт:</b>\n"
        "• /delete_account — полностью удалить данные и выйти\n"
        "• /start — главное меню"
    )


async def auth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерация персональной OAuth-ссылки для авторизации."""
    user_id = update.effective_user.id
    logger.info("Запрос авторизации от user_id=%d", user_id)
    
    existing = await get_user_token(user_id)
    if existing:
        user_tz = await get_user_timezone(user_id)
        await update.message.reply_html(
            "✅ <b>Уже авторизован!</b>\n\n"
            f"Твой Google Calendar подключён (часовой пояс: <code>{user_tz}</code>).\n\n"
            "⚠️ <b>Не забудь настроить:</b>\n"
            "• /set_timezone — если пояс указан неверно (Должен совпадать с поясом Google Calendar!)\n"
            "• /set_reminder — выбрать время напоминания\n\n"
            "Используй:\n"
            "• /upcoming — посмотреть предстоящие события\n"
            "• /history — посмотреть историю встреч"
        )
        return

    try:
        with open('credentials.json', 'r', encoding='utf-8') as f:
            creds_data = json.load(f)
        client_config = creds_data.get('installed', creds_data.get('web', {}))
        client_id = client_config.get('client_id', '')
    except FileNotFoundError:
        await update.message.reply_html(
            "❌ Файл <code>credentials.json</code> не найден.\n"
            "Пожалуйста, настрой проект в Google Cloud Console и добавь файл в корень бота."
        )
        return
    except Exception as e:
        logger.error("Ошибка чтения credentials.json: %s", e)
        await update.message.reply_text("❌ Ошибка конфигурации. Обратись к администратору.")
        return

    if not client_id:
        await update.message.reply_text("❌ Не найден client_id в credentials.json")
        return

    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={client_id}&"
        f"redirect_uri=http://localhost&"
        f"response_type=code&"
        f"scope={urllib.parse.quote(SCOPES[0], safe='')}&"
        f"access_type=offline&"
        f"prompt=consent"
    )

    await update.message.reply_html(
        f"🔐 <b>Авторизация в Google Calendar</b>\n\n"
        f"1️⃣ Скопируй ссылку и открой в ⚠️своём браузере:\n"
        f"<code>{auth_url}</code>\n\n"
        f"2️⃣ Выбери аккаунт и нажми <b>Разрешить</b>.\n\n"
        f"3️⃣ После разрешения браузер перенаправит на <code>http://localhost</code>.\n"
        f"   • Если страница не загрузилась — это нормально (ошибка соединения).\n"
        f"   • <b>Скопируй код из адресной строки</b>:\n"
        f"      <code>http://localhost/?code=<u>4/0AX4XfWh...</u>&scope=...</code>\n"
        f"     Нужна только часть после <code>code=</code> до первого <code>&</code>.\n\n"
        f"4️⃣ Отправь код боту: <code>/auth_code ваш_код</code>\n\n"
        f"<i>🔒 Токен сохранится зашифрованным в базе данных.</i>",
        disable_web_page_preview=True
    )


async def auth_code_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кода авторизации от пользователя."""
    user_id = update.effective_user.id
    raw_input = ' '.join(context.args) if context.args else None
    
    if not raw_input:
        await update.message.reply_html(
            "❌ Отправь код авторизации так:\n"
            "<code>/auth_code 4/0AX4XfWh...</code>"
        )
        return

    try:
        if 'code=' in raw_input:
            parsed = urllib.parse.urlparse(raw_input)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get('code', [None])[0]
            if not code:
                raise ValueError("Код не найден в URL")
        else:
            code = urllib.parse.unquote(raw_input.strip())
        
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        flow.redirect_uri = 'http://localhost'
        flow.fetch_token(code=code)
        creds = flow.credentials
        
        await save_user_token(
            user_id=user_id,
            token_json=creds.to_json(),
            expires_at=creds.expiry.isoformat() if creds.expiry else None,
            calendar_id='primary'
        )
        
        current_tz = await get_user_timezone(user_id)
        if not current_tz or current_tz == DEFAULT_TIMEZONE:
            await save_user_timezone(user_id, DEFAULT_TIMEZONE)
        
        logger.info("Токен сохранён для user_id=%d", user_id)
        
        await update.message.reply_html(
            "✅ <b>Авторизация завершена!</b>\n\n"
            "Теперь бот может читать твой календарь и присылать уведомления.\n\n"
            "⚠️ <b>ВАЖНО:</b> укажите свой часовой пояс командой <code>/set_timezone</code> (Должен совпадать с поясом Google Calendar!) "
            "и настройте интервал напоминаний через <code>/set_reminder</code>.\n\n"
            "Используйте:\n"
            "• /upcoming — предстоящие события\n"
            "• /history — история встреч\n"
            "• /delete_account — полностью удалить данные и выйти"
        )
        
    except FileNotFoundError:
        logger.error("credentials.json не найден")
        await update.message.reply_html(
            "❌ Файл <code>credentials.json</code> не найден.\n"
            "Настрой проект в Google Cloud Console и добавь файл в корень бота."
        )
    except Exception as e:
        logger.error("Ошибка обмена кода на токен user_id=%d: %s", user_id, e, exc_info=True)
        await update.message.reply_text(
            "❌ Не удалось завершить авторизацию. Код одноразовый и живёт ~10 минут.\n"
            "Попробуй /auth ещё раз."
        )


async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /set_timezone — установка часового пояса пользователя."""
    user_id = update.effective_user.id
    args = context.args
    
    if not args:
        current_tz = await get_user_timezone(user_id)
        await update.message.reply_html(
            f"🌍 Текущий часовой пояс: <code>{current_tz}</code>\n\n"
            f"<b>Как указать пояс:</b>\n"
            f"• Город: <code>/set_timezone Москва</code>\n"
            f"• Код: <code>/set_timezone МСК</code>\n"
            f"• Полный: <code>/set_timezone Europe/Moscow</code>\n\n"
            f"<b>Примеры:</b> Москва, МСК, Екатеринбург, Лондон, Нью-Йорк, Дубай"
        )
        return

    tz_input = args[0]
    resolved_tz = resolve_timezone(tz_input)

    if not resolved_tz:
        await update.message.reply_html(
            "❌ Не удалось распознать часовой пояс.\n\n"
            "<b>Попробуйте:</b>\n"
            "• Город: <code>Москва</code>, <code>Лондон</code>, <code>Нью-Йорк</code>\n"
            "• Код: <code>МСК</code>, <code>EKT</code>, <code>NY</code>\n"
            "• IANA: <code>Europe/Moscow</code>, <code>America/New_York</code>\n\n"
            "<i>Полный список: /help</i>"
        )
        return

    try:
        await save_user_timezone(user_id, resolved_tz)
        reminder_min = await get_user_setting(user_id)
        await reset_notified_for_window(user_id, reminder_min, user_timezone=resolved_tz)
        
        logger.info("User %d сменил пояс на %s (ввод: '%s')", user_id, resolved_tz, tz_input)
        
        await update.message.reply_html(
            f"✅ Часовой пояс установлен: <b>{resolved_tz}</b>\n"
            f"(вы указали: <code>{tz_input}</code>)\n"
            f"Уведомления теперь будут приходить в вашем локальном времени."
        )
    except Exception as e:
        logger.error("Ошибка сохранения пояса: %s", e)
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте ещё раз.")


async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Установка интервала напоминаний.
    Поддерживает кнопки быстрого выбора и ручной ввод любого значения, кратного 5.
    """
    user_id = update.effective_user.id
    args = context.args
    msg = update.effective_message
    
    if not args:
        current = await get_user_setting(user_id)
        user_tz = await get_user_timezone(user_id)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("5 мин", callback_data=f"remind_{user_id}_5"),
             InlineKeyboardButton("10 мин", callback_data=f"remind_{user_id}_10"),
             InlineKeyboardButton("15 мин", callback_data=f"remind_{user_id}_15")],
            [InlineKeyboardButton("30 мин", callback_data=f"remind_{user_id}_30"),
             InlineKeyboardButton("1 час", callback_data=f"remind_{user_id}_60"),
             InlineKeyboardButton("2 часа", callback_data=f"remind_{user_id}_120")],
        ])
        
        await msg.reply_html(
            f"⏰ Текущее: за <b>{current}</b> мин (пояс: <code>{user_tz}</code>)\n\n"
            f"<b>Выберите интервал кнопками</b> или укажите своё значение:\n"
            f"<code>/set_reminder 25</code> — кратно 5, от 5 до 1440 мин",
            reply_markup=keyboard
        )
        return
    
    try:
        raw_input = args[0].strip()
        if not raw_input.isdigit():
            await msg.reply_html("❌ Ошибка: укажите целое число.\nПример: <code>/set_reminder 35</code>")
            return
            
        minutes = int(raw_input)
        
        if minutes < 5 or minutes > 1440 or minutes % 5 != 0:
            await msg.reply_html(
                f"❌ Недопустимое значение: <b>{minutes}</b> мин.\n\n"
                f"<b>Правила:</b>\n"
                f"• Диапазон: от 5 до 1440 мин (24 часа)\n"
                f"• Шаг: кратно 5 (5, 10, 15, 20, 25, 30...)\n\n"
                f"<b>Примеры:</b>\n<code>/set_reminder 20</code>\n<code>/set_reminder 45</code>"
            )
            return
        
        user_tz = await get_user_timezone(user_id)
        
        lock = get_reminder_lock(user_id)
        async with lock:
            mark_reminder_changed(user_id)
            await notify_snooze_override(context.bot, user_id)
            await save_user_setting(user_id, minutes)
            await reset_notified_for_window(user_id, minutes, user_timezone=user_tz)
            
        logger.info("Пользователь %d установил напоминание: %d мин (TZ=%s)", user_id, minutes, user_tz)
        
        conflict_events = []
        now = datetime.now(ZoneInfo(user_tz))
        fast_check = await get_upcoming_events(
            hours=1, calendar_id='primary', user_id=user_id, user_timezone=user_tz
        )
        for ev in fast_check:
            start_dt = parse_datetime_string(ev['start_time'], user_tz)
            if start_dt:
                diff_min = (start_dt - now).total_seconds() / 60
                if 0.5 < diff_min < minutes:
                    conflict_events.append(f"• {ev['title']} (осталось {int(diff_min)} мин)")
        
        base_msg = f"✅ Напоминание установлено: за <b>{minutes}</b> минут до встречи."
        if conflict_events:
            warn_text = (
                f"\n\n⚠️ <b>Внимание:</b> для следующих событий правило не применится "
                f"(осталось &lt; {minutes} мин):\n" + "\n".join(conflict_events[:3])
            )
            if len(conflict_events) > 3:
                warn_text += f"\n...и ещё {len(conflict_events) - 3}"
            await msg.reply_html(base_msg + warn_text)
        else:
            await msg.reply_html(base_msg)
            
    except Exception as e:
        logger.error("Ошибка в set_reminder: %s", e, exc_info=True)
        await msg.reply_text("❌ Не удалось установить напоминание. Проверь формат команды или попробуй позже.")


@rate_limit(seconds=10)
async def upcoming_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /upcoming — получение предстоящих событий."""
    user_id = update.effective_user.id
    logger.info("Запрос предстоящих событий от user_id=%d", user_id)
    await update.message.reply_text("🔄 Загружаю события из календаря...")

    try:
        user_tz = await get_user_timezone(user_id)
        
        events = await get_upcoming_events(
            hours=24, calendar_id='primary', user_id=user_id, user_timezone=user_tz
        )
        
        if not events:
            await update.message.reply_html(
                f"📭 <b>Нет предстоящих событий</b> на ближайшие 24 часа.\n"
                f"<i>(часовой пояс: {user_tz})</i>"
            )
            return
        
        msg = f"📅 <b>Предстоящие события</b> (пояс: <code>{user_tz}</code>):\n\n"
        for ev in events:
            link = f"\n🔗 <a href='{ev['link']}'>Подключиться</a>" if ev.get('link') else ""
            msg += f"• <b>{ev['title']}</b>\n   {ev['start_time']} - {ev['end_time']}{link}\n\n"
        
        await update.message.reply_html(msg[:4096], disable_web_page_preview=True)
        
    except Exception as e:
        logger.error("Ошибка в /upcoming для user_id=%d: %s", user_id, e, exc_info=True)
        await update.message.reply_text(
            "❌ Не удалось загрузить события.\n"
            "Возможные причины:\n"
            "• Не пройдена авторизация (/auth)\n"
            "• Временная ошибка Google API — попробуй позже"
        )


@rate_limit(seconds=10)
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /history — получение последних встреч с пагинацией."""
    user_id = update.effective_user.id
    logger.info("Запрос истории от user_id=%d", user_id)
    
    try:
        user_tz = await get_user_timezone(user_id)
        
        all_events = await get_last_events(user_id, limit=100, user_timezone=user_tz)
        
        if not all_events:
            await update.message.reply_html(
                "📭 <b>История пуста</b>.\n\n"
                "Проведённые встречи появятся здесь после синхронизации.\n"
                f"<i>Часовой пояс отображения: {user_tz}</i>"
            )
            return
        
        PAGE_SIZE = 10
        page = 1
        
        start_idx = (page - 1) * PAGE_SIZE
        end_idx = start_idx + PAGE_SIZE
        page_events = all_events[start_idx:end_idx]
        total_pages = (len(all_events) + PAGE_SIZE - 1) // PAGE_SIZE
        
        msg = f"📜 <b>Последние встречи</b> (пояс: <code>{user_tz}</code>, стр. {page}/{total_pages}):\n\n"
        for ev in page_events:
            link = f"\n🔗 <a href='{ev.get('link', '')}'>Ссылка</a>" if ev.get('link') else ""
            msg += f"• <b>{ev['title']}</b>\n  🕐 {ev['start_time']} - {ev['end_time']}{link}\n\n"
        
        if total_pages > 1:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "◀️", 
                        callback_data=f"hist_{user_id}_{page-1}" if page > 1 else "noop"
                    ),
                    InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"),
                    InlineKeyboardButton(
                        "▶️", 
                        callback_data=f"hist_{user_id}_{page+1}" if page < total_pages else "noop"
                    ),
                ],
            ])
            await update.message.reply_html(msg[:4096], reply_markup=keyboard, disable_web_page_preview=True)
        else:
            await update.message.reply_html(msg[:4096], disable_web_page_preview=True)
        
    except Exception as e:
        logger.error("Ошибка в /history для user_id=%d: %s", user_id, e, exc_info=True)
        await update.message.reply_text("❌ Не удалось загрузить историю. Попробуй позже.")


async def handle_reminder_callback(query, user_id: int, minutes: int):
    """Обработчик нажатия кнопки быстрого выбора напоминания."""
    try:
        user_tz = await get_user_timezone(user_id)
        
        if minutes < REMINDER_MIN or minutes > REMINDER_MAX or minutes % REMINDER_STEP != 0:
            await query.answer("❌ Недопустимое значение", show_alert=True)
            return False
        
        lock = get_reminder_lock(user_id)
        async with lock:
            mark_reminder_changed(user_id)
            await notify_snooze_override(query.get_bot(), user_id)
            await save_user_setting(user_id, minutes)
            await reset_notified_for_window(user_id, minutes, user_timezone=user_tz)
        
        await query.answer(f"✅ Напоминание: за {minutes} мин")
        await query.edit_message_text(
            f"⏰ Напоминание установлено: за <b>{minutes}</b> минут (пояс: <code>{user_tz}</code>)",
            parse_mode='HTML'
        )
        
        logger.info("User %d установил напоминание %d мин через кнопку", user_id, minutes)
        return True
        
    except Exception as e:
        logger.error("Ошибка в callback reminder: %s", e)
        await query.answer("⚠️ Произошла ошибка", show_alert=True)
        return False


async def handle_history_pagination(query, user_id: int, page: int):
    """Обработчик пагинации истории."""
    try:
        user_tz = await get_user_timezone(user_id)
        all_events = await get_last_events(user_id, limit=100)
        
        if not all_events:
            await query.answer("📭 История пуста", show_alert=True)
            return
        
        PAGE_SIZE = 10
        total_pages = (len(all_events) + PAGE_SIZE - 1) // PAGE_SIZE
        
        page = max(1, min(page, total_pages))
        
        start_idx = (page - 1) * PAGE_SIZE
        end_idx = start_idx + PAGE_SIZE
        page_events = all_events[start_idx:end_idx]
        
        msg = f"📜 <b>Последние встречи</b> (пояс: <code>{user_tz}</code>, стр. {page}/{total_pages}):\n\n"
        for ev in page_events:
            link = f"\n🔗 <a href='{ev.get('link', '')}'>Ссылка</a>" if ev.get('link') else ""
            msg += f"• <b>{ev['title']}</b>\n  🕐 {ev['start_time']} - {ev['end_time']}{link}\n\n"
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "◀️", 
                    callback_data=f"hist_{user_id}_{page-1}" if page > 1 else "noop"
                ),
                InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"),
                InlineKeyboardButton(
                    "▶️", 
                    callback_data=f"hist_{user_id}_{page+1}" if page < total_pages else "noop"
                ),
            ],
        ])
        
        await query.edit_message_text(
            msg[:4096], 
            parse_mode='HTML', 
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        await query.answer()
        
    except Exception as e:
        logger.error("Ошибка пагинации истории: %s", e)
        await query.answer("⚠️ Ошибка загрузки", show_alert=True)


async def handle_cleanup_callback(query, user_id: int):
    """Обработчик кнопки очистки старых событий."""
    try:
        user_tz = await get_user_timezone(user_id)
        from database import cleanup_old_events
        
        deleted = await cleanup_old_events(user_id=user_id, days_ago=90, user_timezone=user_tz)
        
        await query.answer(f"🗑️ Удалено: {deleted}")
        await query.edit_message_reply_markup(reply_markup=None)
        
        logger.info("User %d очистил %d старых событий", user_id, deleted)
        
    except Exception as e:
        logger.error("Ошибка очистки: %s", e)
        await query.answer("⚠️ Ошибка", show_alert=True)


async def delete_account_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полное удаление данных пользователя, токена и очистка кэшей."""
    user_id = update.effective_user.id
    logger.info("Запрос на полное удаление данных от user_id=%d", user_id)

    from database import delete_user_data
    await delete_user_data(user_id)

    from scheduler import _snooze_cache, _user_reminder_version
    keys_to_del = [k for k in _snooze_cache if k.startswith(f"{user_id}_")]
    for k in keys_to_del:
        del _snooze_cache[k]
    _user_reminder_version.pop(user_id, None)
    _rate_limit_store.pop(user_id, None)

    await update.message.reply_html(
        "✅ <b>Все данные удалены.</b>\n\n"
        "Ваш токен, события, настройки и история полностью стёрты из базы данных.\n"
        "Для повторного использования бота пройдите авторизацию заново: /start → /auth"
    )