"""Планировщик уведомлений на основе APScheduler."""
import logging
import asyncio
import sys
import html
import re
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

# Совместимость с Python 3.10: InvalidTimezoneError появился в 3.11
if sys.version_info >= (3, 11):
    from zoneinfo import InvalidTimezoneError
else:
    InvalidTimezoneError = KeyError  # type: ignore

from database import (
    get_unnotified_upcoming,
    mark_as_notified,
    reset_notified,
    get_user_setting,
    get_all_users,
    get_user_timezone,
    parse_datetime_string,
    DEFAULT_TIMEZONE,
    reset_notified_for_window,
    get_event_info,
    get_event_by_id,
    set_event_snooze_until,
    get_pending_snoozes
)

logger = logging.getLogger(__name__)

# Кэш отложенных уведомлений: ключ "user_id_event_id", значение с until/version
_snooze_cache: dict[str, dict] = {}
# Версия настроек напоминаний для инвалидации устаревших кнопок
_user_reminder_version: dict[int, int] = {}

_scheduler_instance: AsyncIOScheduler | None = None
_bot_instance = None

_notification_locks: dict[int, asyncio.Lock] = {}


def get_notification_lock(event_id: int) -> asyncio.Lock:
    """Возвращает или создаёт лок для конкретного события."""
    if event_id not in _notification_locks:
        _notification_locks[event_id] = asyncio.Lock()
    return _notification_locks[event_id]


def get_user_reminder_version(user_id: int) -> int:
    """Возвращает текущую версию настроек напоминаний пользователя."""
    return _user_reminder_version.get(user_id, 0)


def mark_reminder_changed(user_id: int):
    """Инкрементирует версию настроек при /set_reminder для инвалидации старых кнопок."""
    _user_reminder_version[user_id] = get_user_reminder_version(user_id) + 1


def register_snooze(user_id: int, event_id: int, minutes: int = 5):
    """Регистрирует откладку уведомления с привязкой к версии настроек."""
    key = f"{user_id}_{event_id}"
    now_utc = datetime.now(ZoneInfo("UTC"))
    _snooze_cache[key] = {
        'until': now_utc + timedelta(minutes=minutes),
        'registered_at': now_utc,
        'version': get_user_reminder_version(user_id)
    }
    logger.debug("Snooze: %s до %s (UTC), ver=%d", key, _snooze_cache[key]['until'], _snooze_cache[key]['version'])


def is_snoozed(user_id: int, event_id: int) -> str:
    """
    Проверяет статус snooze: 'active' / 'expired' / 'overridden' / 'none'.
    Инвалидирует запись при смене версии настроек.
    """
    key = f"{user_id}_{event_id}"
    if key not in _snooze_cache:
        return 'none'
    
    data = _snooze_cache[key]
    now_utc = datetime.now(ZoneInfo("UTC"))

    if get_user_reminder_version(user_id) > data.get('version', 0):
        del _snooze_cache[key]
        return 'overridden'

    if now_utc < data['until']:
        return 'active'
    else:
        del _snooze_cache[key]
        logger.debug("Snooze истёк для %s", key)
        return 'expired'


def clear_snooze(user_id: int, event_id: int):
    """Принудительно удаляет запись из snooze-кэша."""
    key = f"{user_id}_{event_id}"
    if key in _snooze_cache:
        del _snooze_cache[key]
        logger.debug("Snooze-кэш очищен для %s", key)


async def notify_snooze_override(bot, user_id: int) -> bool:
    """
    Уведомляет пользователя об отмене активных отложек при смене /set_reminder.
    Возвращает True, если были найдены и сброшены активные snooze.
    """
    from scheduler import _scheduler_instance

    keys_to_clear = [
        key for key, data in _snooze_cache.items()
        if key.startswith(f"{user_id}_") and data.get('until') > datetime.now(ZoneInfo("UTC"))
    ]

    try:
        from database import get_pending_snoozes
        pending = await get_pending_snoozes()
        for item in pending:
            if item['user_id'] == user_id:
                key = f"{user_id}_{item['id']}"
                if key not in keys_to_clear:
                    keys_to_clear.append(key)
    except Exception as e:
        logger.warning("Не удалось получить pending snoozes: %s", e)

    if not keys_to_clear:
        return False
        
    for k in keys_to_clear:
        del _snooze_cache[k]
        
        parts = k.split('_')
        if len(parts) >= 3 and _scheduler_instance:
            try:
                e_id = int(parts[1])
                job_id = f"snooze_job_{user_id}_{e_id}"
                if _scheduler_instance.get_job(job_id):
                    _scheduler_instance.remove_job(job_id)
                    logger.debug("Удалена задача snooze %s из планировщика", job_id)
            except (ValueError, KeyError) as e:
                logger.warning("Не удалось удалить задачу snooze %s: %s", k, e)

        try:
            from database import set_event_snooze_until
            await set_event_snooze_until(e_id, None)
            logger.debug("✅ Очищено snooze_until в БД для события %d", e_id)
        except Exception as e:
            logger.warning("Не удалось очистить snooze_until в БД: %s", e)

        await set_event_snooze_until(int(parts[1]), None)

    await bot.send_message(
        chat_id=user_id,
        text="⚠️ <b>Отложка на 5 минут отменена.</b>\nВы ввели команду /set_reminder с новым значением, поэтому ранее активированное отложение не сработает.",
        parse_mode='HTML'
    )
    logger.info("User %d: отправлено уведомление об отмене snooze из-за /set_reminder", user_id)
    return True


def _build_notification_keyboard(
    user_id: int,
    event_id: int,
    start_time_str: str,
    user_timezone: str = DEFAULT_TIMEZONE
) -> InlineKeyboardMarkup | None:
    """Формирует inline-клавиатуру с кнопкой отложки или статусом."""
    try:
        start_dt = parse_datetime_string(start_time_str, user_timezone)
        if start_dt is None:
            return None
        
        now_local = datetime.now(ZoneInfo(user_timezone))
        minutes_left = (start_dt - now_local).total_seconds() / 60
        
        if minutes_left <= 5:
            btn = InlineKeyboardButton("⏳ Скоро начнётся", callback_data="noop")
        else:
            current_ver = get_user_reminder_version(user_id)
            created_ts = int(datetime.now(ZoneInfo("UTC")).timestamp())
            btn = InlineKeyboardButton(
                "🔕 Отложить 5 мин", 
                callback_data=f"snooze_{user_id}_{event_id}_{current_ver}_{created_ts}"
            )
        return InlineKeyboardMarkup([[btn]])
    except Exception as e:
        logger.warning("Ошибка построения клавиатуры: %s", e)
        return None


def _clean_gcal_html(raw: str) -> str:
    """Очищает описание/место от HTML-тегов и сущностей Google Calendar."""
    if not raw:
        return ""
    clean = re.sub(r'<[^>]+>', '', raw)
    clean = html.unescape(clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


async def send_notification(
    bot,
    user_id: int,
    event: dict,
    reply_markup=None,
    retry_count: int = 3
) -> bool:
    """Отправляет уведомление с повторными попытками при ошибках."""
    title_safe = html.escape(event.get('title', 'Без названия'))
    link_safe = html.escape(event.get('link', ''), quote=True)
    loc_clean = _clean_gcal_html(event.get('location', ''))
    desc_clean = _clean_gcal_html(event.get('description', ''))

    loc_text = f"\n📍 Место: {html.escape(loc_clean)}" if loc_clean else ""
    desc_text = f"\n📝 Описание: {html.escape(desc_clean)}" if desc_clean else ""
    if len(desc_text) > 400:
        desc_text = desc_text[:397] + "..."
    link_text = f"\n🔗 <a href='{link_safe}'>Подключиться</a>" if link_safe else ""

    text = (
        f"⏰ <b>Напоминание!</b>\n\n"
        f"📅 <b>{title_safe}</b>\n"
        f"🕐 Начало: {event['start_time']}\n"
        f"🏁 Конец: {event['end_time']}"
        f"{loc_text}{desc_text}{link_text}"
    )

    for attempt in range(retry_count):
        try:
            await bot.send_message(
                chat_id=user_id, 
                text=text, 
                parse_mode='HTML', 
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            return True
        except Exception as e:
            wait_time = 2 ** attempt
            logger.warning("Попытка %d/%d не удалась: %s. Ждём %dс...", attempt + 1, retry_count, e, wait_time)
            await asyncio.sleep(wait_time)

    logger.error("Не удалось отправить уведомление после %d попыток", retry_count)
    return False

async def auto_sync_events():
    """
    Периодическая синхронизация событий для всех пользователей.
    Использует индивидуальный часовой пояс каждого пользователя.
    """
    logger.info("Запуск авто-синхронизации...")
    try:
        from calendar_api import get_upcoming_events, get_past_events
        from database import save_event
        
        users = await get_all_users()
        total_saved = 0
        
        for user_id in users:
            try:
                user_tz = await get_user_timezone(user_id)
                logger.debug("Синхронизация user_id=%d, timezone=%s", user_id, user_tz)
                
                upcoming = await get_upcoming_events(
                    hours=24, calendar_id='primary', user_id=user_id, user_timezone=user_tz
                )
                past = await get_past_events(
                    limit=20, calendar_id='primary', user_id=user_id, user_timezone=user_tz
                )
                
                for ev in upcoming + past:
                    await save_event(user_id=user_id, event=ev, google_event_id=ev.get('id'), user_timezone=user_tz)
                    total_saved += 1
            except Exception as e:
                logger.error("Ошибка синхронизации для user_id=%d: %s", user_id, e, exc_info=True)
                continue
        
        logger.info("Авто-синхронизация завершена: %d событий сохранено", total_saved)
    except Exception as e:
        logger.error("Критическая ошибка в auto_sync_events: %s", e, exc_info=True)


def _get_next_5min_mark_utc(user_tz_str: str) -> datetime:
    """Вычисляет ближайший 5-минутный рубеж в TZ пользователя и возвращает время в UTC."""
    user_tz = ZoneInfo(user_tz_str)
    now_local = datetime.now(user_tz)
    mins_to_add = 5 - (now_local.minute % 5)
    run_at_local = now_local.replace(second=0, microsecond=0) + timedelta(minutes=mins_to_add)
    return run_at_local.astimezone(ZoneInfo("UTC"))


async def _deliver_snooze_reminder(user_id: int, event_id: int):
    """Отправляет отложенное уведомление с защитой от дублей."""
    lock = get_notification_lock(event_id)

    try:
        acquired = await asyncio.wait_for(lock.acquire(), timeout=2.0)
        if not acquired:
            logger.debug("Не удалось захватить лок для %d, пропускаем", event_id)
            return
    except asyncio.TimeoutError:
        logger.debug("Таймаут лока для %d, пропускаем", event_id)
        return

    try:
        snooze_status = is_snoozed(user_id, event_id)
        if snooze_status in ('overridden', 'none'):
            await set_event_snooze_until(event_id, None)
            clear_snooze(user_id, event_id)
            logger.debug("Snooze отменён, задача %d-%d пропущена", user_id, event_id)
            return

        event = await get_event_by_id(event_id)
        if not event or event.get('notified'):
            await set_event_snooze_until(event_id, None)
            clear_snooze(user_id, event_id)
            return

        if event.get('snooze_until') is None:
            logger.debug("snooze_until=NULL в БД, задача %d-%d пропущена", user_id, event_id)
            clear_snooze(user_id, event_id)
            return

        user_tz = await get_user_timezone(user_id)
        markup = _build_notification_keyboard(user_id, event_id, event['start_time'], user_timezone=user_tz)
        
        await mark_as_notified(event_id)
        
        success = await send_notification(_bot_instance, user_id, event, reply_markup=markup)
        
        if success:
            logger.info("✅ Snooze доставлено: user=%d, event=%d", user_id, event_id)
        else:
            await reset_notified(event_id)
            logger.warning("Событие %d: отправка не удалась, флаг notified сброшен", event_id)
        
        await set_event_snooze_until(event_id, None)
        clear_snooze(user_id, event_id)
        
    except Exception as e:
        logger.error("Ошибка доставки snooze: %s", e, exc_info=True)
        try:
            await reset_notified(event_id)
        except:
            pass
    finally:
        if lock.locked():
            lock.release()


async def _restore_snooze_jobs(bot, scheduler: AsyncIOScheduler):
    """Восстанавливает таймеры отложек из БД после перезапуска."""
    try:
        pending = await get_pending_snoozes()
        now_utc = datetime.now(ZoneInfo("UTC"))
        for item in pending:
            run_at = datetime.fromisoformat(item['snooze_until'])
            # Если время уже прошло более 10 мин назад → очищаем, чтобы не спамить
            if run_at < now_utc - timedelta(minutes=10):
                await set_event_snooze_until(item['id'], None)
                continue
                
            scheduler.add_job(
                _deliver_snooze_reminder,
                trigger='date',
                run_date=run_at,
                args=[item['user_id'], item['id']],
                id=f"snooze_job_{item['user_id']}_{item['id']}",
                replace_existing=True,
                misfire_grace_time=60
            )
        if pending:
            logger.info("🔄 Восстановлено %d snooze-таймеров из БД", len(pending))
    except Exception as e:
        logger.error("Ошибка восстановления snooze из БД: %s", e, exc_info=True)


async def check_reminders(bot):
    """
    Задача APScheduler: проверка БД и отправка уведомлений с учётом snooze.
    Расчёт времени производится в часовом поясе каждого пользователя.
    """
    logger.info("Запуск проверки напоминаний...")
    try:
        users = await get_all_users()
        if not users:
            logger.debug("Пользователи не найдены в БД. Пропускаем.")
            return

        for user_id in users:
            try:
                reminder_min = await get_user_setting(user_id)
                user_tz = await get_user_timezone(user_id)
                
                upcoming = await get_unnotified_upcoming(user_id, reminder_min, user_timezone=user_tz)
                
                logger.debug("Юзер %d: окно=%d мин, найдено событий=%d, TZ=%s", user_id, reminder_min, len(upcoming), user_tz)
                
                for event in upcoming:
                    event_id = event['id']

                    try:
                        start_dt = parse_datetime_string(event['start_time'], user_tz)
                        now_local = datetime.now(ZoneInfo(user_tz))
                        if start_dt and start_dt <= now_local:
                            await mark_as_notified(event_id)
                            logger.debug("Пропуск (событие уже началось): %s", event['title'])
                            continue
                    except Exception as parse_err:
                        logger.warning("Не удалось распарсить время для %s: %s", event['title'], parse_err)
                     
                    snooze_status = is_snoozed(user_id, event_id)
                    if snooze_status == 'active':
                        logger.debug("Пропуск (snooze активен): %s", event['title'])
                        continue
                    
                    lock = get_notification_lock(event_id)
                    try:
                        acquired = await asyncio.wait_for(lock.acquire(), timeout=2.0)
                        if not acquired:
                            logger.debug("Пропуск %d: не удалось захватить лок", event_id)
                            continue
                    except asyncio.TimeoutError:
                        logger.debug("Таймаут лока для %d в check_reminders", event_id)
                        continue

                    try:
                        if event.get('notified'):
                            logger.debug("Событие %d уже уведомлено (double-check), пропускаем", event_id)
                            continue
                        
                        if is_snoozed(user_id, event_id) == 'active':
                            logger.debug("Snooze активировался во время ожидания лока для %d", event_id)
                            continue

                        markup = _build_notification_keyboard(user_id, event_id, event['start_time'], user_timezone=user_tz)
                         
                        logger.info("Отправка уведомления (check_reminders): %s | start=%s | TZ=%s", 
                                  event['title'], event['start_time'], user_tz)
                        
                        await mark_as_notified(event_id)
                        
                        success = await send_notification(bot, user_id, event, reply_markup=markup)
                        
                        if success:
                            logger.debug("Событие %d помечено как уведомлённое (check_reminders)", event_id)
                        else:
                            await reset_notified(event_id)
                            logger.warning("Событие %d: отправка в check_reminders не удалась, флаг сброшен", event_id)
                            
                    except Exception as e:
                        logger.error("Ошибка при отправке уведомления %d: %s", event_id, e, exc_info=True)
                        try:
                            await reset_notified(event_id)
                        except:
                            pass
                    finally:
                        if lock.locked():
                            lock.release()
                        
            except Exception as user_err:
                logger.error("Ошибка обработки юзера %d: %s", user_id, user_err, exc_info=True)
                continue
                
        logger.info("Проверка напоминаний завершена.")
    except Exception as e:
        logger.error("Критическая ошибка в планировщике: %s", e, exc_info=True)


def start_scheduler(bot) -> AsyncIOScheduler:
    """
    Инициализирует и запускает AsyncIOScheduler.
    Все задачи работают в UTC, внутри используют часовой пояс пользователя.
    """
    global _scheduler_instance, _bot_instance
    scheduler = AsyncIOScheduler(timezone='UTC')
    _scheduler_instance = scheduler
    _bot_instance = bot

    scheduler.add_job(
        auto_sync_events,
        trigger=CronTrigger(minute='*/5', second=0, timezone='UTC'),
        id='auto_sync_events',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=30
    )

    scheduler.add_job(
        check_reminders, 
        trigger=CronTrigger(minute='*/5', second=15, timezone='UTC'), 
        args=[bot],
        id='check_reminders',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=30
    )

    scheduler.add_job(
        _restore_snooze_jobs,
        trigger='date',
        run_date=datetime.now(ZoneInfo("UTC")),
        args=[bot, scheduler]
    )

    scheduler.start()
    logger.info(
        "Планировщик запущен (UTC): синхр=*/5+0с, проверка=*/5+15с, очистка=1-го числа 03:00"
    )
    return scheduler


async def shutdown_scheduler(scheduler: AsyncIOScheduler):
    """Корректно останавливает планировщик и очищает кэш при завершении."""
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("Планировщик остановлен")
    
    _snooze_cache.clear()
    _user_reminder_version.clear()
    logger.debug("Snooze-кэш и трекинг настроек очищены")


async def _safe_query_answer(query, text: str = "", show_alert: bool = False):
    """Безопасный ответ на callback (игнорирует ошибки при повторном ответе)."""
    try:
        await query.answer(text, show_alert=show_alert)
    except Exception:
        pass


async def handle_snooze_callback(
    query,
    user_id: int,
    event_id: int,
    button_version: int,
    created_ts: int,
    minutes: int = 5
):
    """Обработчик нажатия кнопки 'Отложить 5 мин' с валидацией."""
    try:
        now_ts = int(datetime.now(ZoneInfo("UTC")).timestamp())
        if now_ts - created_ts > 5 * 60:
            await _safe_query_answer(query, "⏳ Время отложки истекло", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "🔕 Отложить не получится: прошло более 5 минут с момента уведомления.",
                parse_mode='HTML'
            )
            return False

        event_info = await get_event_info(event_id)
        if not event_info:
            await _safe_query_answer(query, "⚠️ Событие не найдено", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            return False

        user_tz = await get_user_timezone(user_id)
        start_dt = parse_datetime_string(event_info['start_time'], user_tz)
        now_local = datetime.now(ZoneInfo(user_tz))

        if start_dt and start_dt <= now_local:
            await _safe_query_answer(query, "⏳ Событие уже началось", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "🔕 Отложить не получилось: мероприятие уже началось или завершилось.",
                parse_mode='HTML'
            )
            return False

        current_ver = get_user_reminder_version(user_id)
        if button_version < current_ver:
            await _safe_query_answer(query, "⚠️ Настройки изменены", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "🔕 Отложить на 5 минут не получится, так как уже указан новый интервал через <b>/set_reminder</b> в новом сообщении.",
                parse_mode='HTML'
            )
            return False

        status = is_snoozed(user_id, event_id)
        if status == 'active':
            await _safe_query_answer(query, "⏰ Уведомление уже отложено")
            return True

        register_snooze(user_id, event_id, minutes)

        run_at_utc = _get_next_5min_mark_utc(user_tz)

        await set_event_snooze_until(event_id, run_at_utc.isoformat())

        key = f"{user_id}_{event_id}"
        _snooze_cache[key] = {
            'until': run_at_utc,
            'registered_at': datetime.now(ZoneInfo("UTC")),
            'version': get_user_reminder_version(user_id)
        }
        await reset_notified(event_id)

        global _scheduler_instance
        if _scheduler_instance:
            _scheduler_instance.add_job(
                _deliver_snooze_reminder,
                trigger='date',
                run_date=run_at_utc,
                args=[user_id, event_id],
                id=f"snooze_job_{user_id}_{event_id}",
                replace_existing=True,
                misfire_grace_time=30
        )

        local_fire_time = run_at_utc.astimezone(ZoneInfo(user_tz)).strftime('%H:%M')
        await _safe_query_answer(query, f"⏰ Напомню ровно в {local_fire_time}")
        await query.edit_message_reply_markup(reply_markup=None)
    
        logger.info("User %d отложил событие %d до %s (UTC)", user_id, event_id, run_at_utc.isoformat())
        return True
        
    except Exception as e:
        logger.error("Ошибка обработки snooze: %s", e, exc_info=True)
        await _safe_query_answer(query, "⚠️ Произошла ошибка", show_alert=True)
        return False


async def handle_timezone_change(user_id: int, new_timezone: str):
    """
    Обработчик смены часового пояса: сбрасывает уведомления для событий в новом окне.
    """
    try:
        ZoneInfo(new_timezone)
        reminder_min = await get_user_setting(user_id)
        await reset_notified_for_window(user_id, reminder_min, user_timezone=new_timezone)
        logger.info("User %d сменил пояс на %s", user_id, new_timezone)
        return True
    except InvalidTimezoneError:
        logger.error("Невалидный timezone '%s' для user_id=%d", new_timezone, user_id)
        return False
    except Exception as e:
        logger.error("Ошибка при смене пояса: %s", e)
        return False