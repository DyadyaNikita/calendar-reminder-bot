"""Обработчики callback-запросов от inline-кнопок."""
import logging
import sys
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

# Совместимость с Python 3.10: InvalidTimezoneError появился в 3.11
if sys.version_info >= (3, 11):
    from zoneinfo import InvalidTimezoneError
else:
    InvalidTimezoneError = KeyError  # type: ignore

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import (
    save_user_setting,
    get_last_events,
    reset_notified,
    reset_notified_for_window,
    get_user_timezone,
    parse_datetime_string,
    DEFAULT_TIMEZONE,
)
from scheduler import (
    register_snooze,
    handle_snooze_callback as scheduler_snooze_handler,
    mark_reminder_changed,
    notify_snooze_override
)

logger = logging.getLogger(__name__)

# Локи для предотвращения гонок при смене настроек
_reminder_locks: dict[int, asyncio.Lock] = {}


def get_reminder_lock(user_id: int) -> asyncio.Lock:
    """Возвращает или создаёт асинхронный лок для пользователя."""
    if user_id not in _reminder_locks:
        _reminder_locks[user_id] = asyncio.Lock()
    return _reminder_locks[user_id]


def _verify_user_access(query, expected_user_id: int) -> bool:
    """
    Проверяет, что пользователь имеет доступ к данным.
    Возвращает False и отправляет сообщение при нарушении доступа.
    """
    if query.from_user.id != expected_user_id:
        logger.warning(
            "Попытка доступа: query.from_user.id=%d, expected=%d, data=%s",
            query.from_user.id, expected_user_id, query.data
        )
        try:
            query.edit_message_text("⚠️ Это не ваши данные.")
        except Exception:
            pass
        return False
    return True


def _build_history_keyboard(user_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Формирует клавиатуру навигации по истории."""
    return InlineKeyboardMarkup([
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


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик кнопок быстрого выбора напоминания.
    Формат callback_data: remind_{user_id}_{minutes}
    """
    query = update.callback_query
    await query.answer()

    try:
        parts = query.data.split('_')
        if len(parts) != 3:
            raise ValueError(f"Неверный формат callback: {query.data}")
        
        _, user_id_str, minutes_str = parts
        user_id = int(user_id_str)
        minutes = int(minutes_str)
        
        if not _verify_user_access(query, user_id):
            return
        
        user_tz = await get_user_timezone(user_id)

        lock = get_reminder_lock(user_id)
        async with lock:
            mark_reminder_changed(user_id)
            await notify_snooze_override(context.bot, user_id)
            await save_user_setting(user_id, minutes)
            await reset_notified_for_window(user_id, minutes, user_timezone=user_tz)
        
        logger.info("Пользователь %d выбрал напоминание: %d мин (кнопка, TZ=%s)", user_id, minutes, user_tz)
        
        await query.edit_message_text(
            f"✅ Напоминание: за <b>{minutes}</b> минут (пояс: <code>{user_tz}</code>)",
            parse_mode='HTML'
        )
        
    except ValueError as e:
        logger.error("Ошибка парсинга callback %s: %s", query.data, e)
        await query.edit_message_text("❌ Ошибка формата. Попробуйте /set_reminder вручную.")
    except InvalidTimezoneError as e:  # type: ignore
        logger.error("Ошибка часового пояса: %s", e)
        await query.edit_message_text("⚠️ Ошибка конфигурации. Обратитесь к поддержке.")
    except Exception as e:
        logger.error("Ошибка в reminder_callback: %s", e, exc_info=True)
        await query.edit_message_text("❌ Ошибка. Попробуйте /set_reminder вручную.")


async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик переключения страниц в /history.
    Формат callback_data: hist_{user_id}_{page}
    """
    query = update.callback_query
    await query.answer()

    try:
        parts = query.data.split('_')
        if len(parts) != 3:
            raise ValueError(f"Неверный формат callback: {query.data}")
        
        _, user_id_str, page_str = parts
        user_id = int(user_id_str)
        page = int(page_str)
        
        if not _verify_user_access(query, user_id):
            return
        
        user_tz = await get_user_timezone(user_id)
        all_events = await get_last_events(user_id, limit=100)
        
        if not all_events:
            await query.edit_message_text(
                f"📭 История пуста.\n<i>Часовой пояс: {user_tz}</i>",
                parse_mode='HTML'
            )
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
        
        keyboard = _build_history_keyboard(user_id, page, total_pages)
        
        await query.edit_message_text(
            msg[:4096], 
            parse_mode='HTML', 
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        
    except ValueError as e:
        logger.error("Ошибка парсинга callback %s: %s", query.data, e)
        await query.edit_message_text("❌ Ошибка навигации. Попробуйте /history заново.")
    except Exception as e:
        logger.error("Ошибка в history_callback: %s", e, exc_info=True)
        await query.edit_message_text("❌ Ошибка навигации. Попробуйте /history заново.")


async def snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик кнопки '🔕 Отложить 5 мин'.
    Формат callback_data: snooze_{user_id}_{event_id}_{version}_{timestamp}
    """
    query = update.callback_query
    await query.answer()
    logger.debug("Получен snooze callback: %s", query.data)

    try:
        parts = query.data.split('_')
        if len(parts) != 5:
            raise ValueError(f"Ожидалось 5 частей, получено {len(parts)}: {query.data}")
        
        _, user_id_str, event_id_str, version_str, ts_str = parts
        user_id = int(user_id_str)
        event_id = int(event_id_str)
        button_version = int(version_str)
        created_ts = int(ts_str)
        
        if not _verify_user_access(query, user_id):
            return 
        
        success = await scheduler_snooze_handler(
            query=query,
            user_id=user_id,
            event_id=event_id,
            button_version=button_version,
            created_ts=created_ts,
            minutes=15
        )
        
        if not success:
            logger.debug("Snooze отклонён или обработан с предупреждением (data=%s)", query.data)
    
    except ValueError as e:
        logger.error("Ошибка парсинга callback %s: %s", query.data, e)
        await query.message.reply_text("❌ Ошибка формата кнопки. Попробуйте /sync для обновления.")
    except Exception as e:
        logger.error("Критическая ошибка в snooze_callback: %s", e, exc_info=True)
        await query.message.reply_text("⚠️ Произошла ошибка при обработке отложки.")


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик для кнопок с callback_data='noop'.
    Предотвращает ошибку Telegram при нажатии на неактивные кнопки.
    """
    query = update.callback_query
    await query.answer()


def register_callbacks(dispatcher):
    """
    Регистрирует все callback-обработчики в dispatcher.
    Для python-telegram-bot>=20: register_callbacks(application)
    """
    from telegram.ext import CallbackQueryHandler

    dispatcher.add_handler(CallbackQueryHandler(reminder_callback, pattern=r'^remind_\d+_\d+$'))
    dispatcher.add_handler(CallbackQueryHandler(history_callback, pattern=r'^hist_\d+_\d+$'))
    dispatcher.add_handler(CallbackQueryHandler(snooze_callback, pattern=r'^snooze_\d+_\d+_\d+_\d+$'))
    dispatcher.add_handler(CallbackQueryHandler(noop_callback, pattern=r'^noop$'))

    logger.info("Callback-обработчики зарегистрированы")