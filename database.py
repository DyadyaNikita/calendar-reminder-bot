"""Модуль работы с базой данных (SQLite + aiosqlite)."""
import os
import sys
import logging
import aiosqlite
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# Совместимость с Python 3.10: InvalidTimezoneError появился в 3.11
if sys.version_info >= (3, 11):
    from zoneinfo import InvalidTimezoneError
else:
    InvalidTimezoneError = KeyError  # type: ignore

DB_PATH = 'calendar_bot.db'
DEFAULT_TIMEZONE = 'UTC'
logger = logging.getLogger(__name__)


def _init_encryption() -> Fernet:
    """Инициализирует шифр Fernet для токенов."""
    load_dotenv(override=True)
    
    key_str = os.getenv('TOKEN_ENCRYPTION_KEY', '').strip()
    if not key_str or len(key_str) != 44:
        logger.warning(
            "TOKEN_ENCRYPTION_KEY не задан или невалиден (длина=%d). "
            "Используется временный ключ (НЕ для продакшена).", len(key_str)
        )
        key = Fernet.generate_key()
    else:
        key = key_str.encode()

    cipher = Fernet(key)

    try:
        test = cipher.encrypt(b"test")
        cipher.decrypt(test)
        logger.info("Fernet-шифрование инициализировано")
    except Exception as e:
        logger.error("Критическая ошибка шифрования: %s", e)
        raise

    return cipher


_cipher = _init_encryption()


def _encrypt_token(token_json: str) -> str:
    """Шифрует JSON токена."""
    return _cipher.encrypt(token_json.encode()).decode()


def _decrypt_token(encrypted_token: str) -> str:
    """Расшифровывает токен из БД."""
    return _cipher.decrypt(encrypted_token.encode()).decode()


def parse_datetime_string(dt_str: str, target_tz: str | ZoneInfo = DEFAULT_TIMEZONE) -> datetime | None:
    """
    Универсальный парсер строк времени из БД/календаря.
    Поддерживаемые форматы: ISO 8601, "дд.мм ЧЧ:ММ", "ЧЧ:ММ", "ГГГГ-ММ-ДД".
    Возвращает timezone-aware datetime в указанном поясе.
    """
    if not dt_str:
        return None

    if isinstance(target_tz, str):
        try:
            target_tz = ZoneInfo(target_tz)
        except InvalidTimezoneError:  # type: ignore
            logger.warning("Невалидный timezone '%s', использую UTC", target_tz)
            target_tz = ZoneInfo(DEFAULT_TIMEZONE)

    try:
        # ISO-формат с временем
        if 'T' in dt_str:
            normalized = dt_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(normalized)
            return dt.astimezone(target_tz)
        
        # Формат БД: "дд.мм ЧЧ:ММ"
        elif '.' in dt_str and ' ' in dt_str and ':' in dt_str:
            date_part, time_part = dt_str.strip().split(' ')
            day, month = map(int, date_part.split('.'))
            hour, minute = map(int, time_part.split(':'))
            
            now = datetime.now(target_tz)
            year = now.year
            candidate = datetime(year, month, day, hour, minute, tzinfo=target_tz)
            
            # Если дата ушла в прошлое >1 дня → событие следующего года
            if (now - candidate).days > 1:
                candidate = candidate.replace(year=year + 1)
            return candidate
        
        # Только время "ЧЧ:ММ" (обратная совместимость)
        elif ':' in dt_str and '.' not in dt_str and 'T' not in dt_str and len(dt_str.strip()) <= 5:
            now = datetime.now(target_tz)
            hour, minute = map(int, dt_str.strip().split(':'))
            return datetime(now.year, now.month, now.day, hour, minute, tzinfo=target_tz)

        # Только дата: "ГГГГ-ММ-ДД" или "дд.мм.гггг"
        elif '-' in dt_str or (dt_str.count('.') == 2):
            fmt = "%Y-%m-%d" if '-' in dt_str else "%d.%m.%Y"
            dt = datetime.strptime(dt_str, fmt)
            return dt.replace(tzinfo=target_tz)
        
        else:
            logger.warning("Не распознан формат времени: '%s'", dt_str)
            return None
            
    except Exception as e:
        logger.warning("Ошибка парсинга '%s': %s", dt_str, e)
        return None


def format_for_db(dt: datetime, tz: str | ZoneInfo = DEFAULT_TIMEZONE) -> str:
    """Форматирует datetime для хранения в БД: "дд.мм ЧЧ:ММ"."""
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    local_dt = dt.astimezone(tz)
    return local_dt.strftime("%d.%m %H:%M")


async def init_db():
    """Создаёт таблицы и выполняет миграции схемы."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                link TEXT,
                description TEXT,
                notified BOOLEAN DEFAULT 0,
                google_event_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, google_event_id, start_time)
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                reminder_minutes INTEGER DEFAULT 15,
                timezone TEXT DEFAULT 'UTC',
                updated_at TEXT NOT NULL
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_tokens (
                user_id INTEGER PRIMARY KEY,
                token_json TEXT NOT NULL,
                expires_at TEXT,
                calendar_id TEXT DEFAULT 'primary',
                updated_at TEXT NOT NULL
            )
        ''')
        
        # Миграция: добавление колонки timezone
        try:
            await db.execute('ALTER TABLE user_settings ADD COLUMN timezone TEXT DEFAULT "UTC"')
            logger.info("Миграция: добавлена колонка timezone в user_settings")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise

        # Миграция: добавление колонки location
        try:
            await db.execute('ALTER TABLE events ADD COLUMN location TEXT DEFAULT ""')
            logger.info("Миграция: добавлена колонка location в events")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
        
        await db.commit()
        logger.info("База данных инициализирована")


async def save_event(
    user_id: int,
    event: dict,
    google_event_id: str = None,
    user_timezone: str = DEFAULT_TIMEZONE
):
    """
    Сохраняет или обновляет событие.
    Сохраняет статус notified при синхронизации, если время не изменилось.
    """
    g_id = google_event_id or event.get('id')
    start_dt = parse_datetime_string(event['start_time'], user_timezone)
    start_db = format_for_db(start_dt, user_timezone) if start_dt else event['start_time']
    end_dt = parse_datetime_string(event['end_time'], user_timezone)
    end_db = format_for_db(end_dt, user_timezone) if end_dt else event['end_time']
    now_iso = datetime.now(ZoneInfo(user_timezone)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT id, notified, start_time FROM events WHERE user_id=? AND google_event_id=?',
            (user_id, g_id)
        )
        row = await cursor.fetchone()
        
        if row:
            old_id, old_notified, old_start = row
            new_notified = 0 if old_start != start_db else old_notified
            await db.execute('''
                UPDATE events SET title=?, start_time=?, end_time=?, link=?, description=?, location=?, notified=?, created_at=?
                WHERE id=?
            ''', (event['title'], start_db, end_db, event.get('link',''), event.get('description',''), event.get('location', ''), new_notified, now_iso, old_id))
        else:
            await db.execute('''
                INSERT INTO events (user_id, title, start_time, end_time, link, description, location, notified, google_event_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ''', (user_id, event['title'], start_db, end_db, event.get('link',''), event.get('description',''), event.get('location', ''), g_id, now_iso))
            
        await db.commit()


async def get_last_events(user_id: int, limit: int = 100, user_timezone: str = DEFAULT_TIMEZONE):
    """Возвращает завершённые события (end_time < now), отсортированные по убыванию."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            'SELECT title, start_time, end_time, link, description FROM events WHERE user_id = ?',
            (user_id,)
        )
        rows = await cursor.fetchall()
        now = datetime.now(ZoneInfo(user_timezone))
        past_events = []
        
        for row in rows:
            end_dt = parse_datetime_string(row['end_time'], user_timezone)
            if end_dt and end_dt < now:
                past_events.append(dict(row))
                
        past_events.sort(
            key=lambda x: parse_datetime_string(x['end_time'], user_timezone) or datetime.min.replace(tzinfo=ZoneInfo(user_timezone)),
            reverse=True
        )
        return past_events[:limit]


async def get_event_info(event_id: int) -> dict | None:
    """Возвращает start_time и статус notified для валидации кнопок."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            'SELECT start_time, notified FROM events WHERE id = ?', (event_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_unnotified_upcoming(
    user_id: int,
    reminder_minutes: int,
    user_timezone: str = DEFAULT_TIMEZONE
):
    """
    Возвращает предстоящие события в окне уведомления.
    Окно: 0.5 мин < время_до_старта <= reminder_minutes.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM events
            WHERE user_id = ? AND notified = 0
            ORDER BY start_time ASC
        ''', (user_id,))
        rows = await cursor.fetchall()
        now = datetime.now(ZoneInfo(user_timezone))
        upcoming = []

        for row in rows:
            try:
                st = parse_datetime_string(row['start_time'], user_timezone)
                et = parse_datetime_string(row['end_time'], user_timezone)
                
                if st is None or et is None:
                    continue
                if et < now:
                    continue
                
                diff_min = (st - now).total_seconds() / 60
                
                if 0.5 < diff_min <= reminder_minutes:
                    upcoming.append(dict(row))
                    
            except Exception as e:
                logger.warning(
                    "Пропуск события (user_id=%s): время='%s', ошибка: %s", 
                    user_id, row['start_time'], e
                )
                continue
                
        return upcoming


async def mark_as_notified(event_id: int):
    """Помечает событие как уведомлённое."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE events SET notified = 1 WHERE id = ?', (event_id,))
        await db.commit()


async def reset_notified(event_id: int):
    """Сбрасывает флаг уведомления для повторной отправки."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE events SET notified = 0 WHERE id = ?', (event_id,))
        await db.commit()
    logger.debug("Событие %d помечено как не уведомлённое", event_id)


async def reset_notified_for_window(
    user_id: int,
    new_minutes: int,
    user_timezone: str = DEFAULT_TIMEZONE
):
    """
    Сбрасывает notified=0 для всех будущих событий при смене интервала.
    Планировщик применит новое окно при следующем тике.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT id, start_time FROM events WHERE user_id = ? AND notified = 1',
            (user_id,)
        )
        rows = await cursor.fetchall()
        now = datetime.now(ZoneInfo(user_timezone))
        reset_count = 0

        for event_id, start_str in rows:
            start_dt = parse_datetime_string(start_str, user_timezone)
            if not start_dt:
                continue
            diff_min = (start_dt - now).total_seconds() / 60
            if diff_min > 0.5:
                await db.execute('UPDATE events SET notified = 0 WHERE id = ?', (event_id,))
                reset_count += 1
                
        if reset_count:
            await db.commit()
            logger.info(
                "Сброшено notified для %d будущих событий user_id=%d (новое окно: %d мин)", 
                reset_count, user_id, new_minutes
            )
        else:
            logger.debug("Для user_id=%d нет будущих событий для сброса", user_id)
            
        return reset_count


async def cleanup_desynced_events(user_id: int, valid_google_ids: list[str], user_timezone: str = DEFAULT_TIMEZONE):
    """
    Удаляет из локальной БД события, которых больше нет в Google Calendar.
    Вызывается после каждой синхронизации.
    """
    if not valid_google_ids:
        return 0
        
    placeholders = ','.join('?' for _ in valid_google_ids)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f'DELETE FROM events WHERE user_id=? AND google_event_id NOT IN ({placeholders})',
            (user_id, *valid_google_ids)
        )
        await db.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info("Удалено %d рассинхронизированных событий для user_id=%d", deleted, user_id)
        return deleted


async def save_user_timezone(user_id: int, timezone: str):
    """Сохраняет часовой пояс пользователя с валидацией."""
    try:
        ZoneInfo(timezone)
    except InvalidTimezoneError:  # type: ignore
        logger.error("Невалидный timezone '%s' для user_id=%d", timezone, user_id)
        raise ValueError(f"Invalid timezone: {timezone}")
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO user_settings 
            (user_id, timezone, updated_at)
            VALUES (?, ?, ?)
        ''', (user_id, timezone, datetime.now(ZoneInfo(timezone)).isoformat()))
        await db.commit()
    logger.info("Сохранён timezone '%s' для user_id=%d", timezone, user_id)


async def get_user_timezone(user_id: int) -> str:
    """Получает часовой пояс пользователя с фоллбэком на UTC."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            'SELECT timezone FROM user_settings WHERE user_id = ?',
            (user_id,)
        )
        row = await cursor.fetchone()
        tz = row['timezone'] if row and row['timezone'] else DEFAULT_TIMEZONE
        
        try:
            ZoneInfo(tz)
            return tz
        except InvalidTimezoneError:  # type: ignore
            logger.warning("Битый timezone '%s' в БД, возвращаю дефолт", tz)
            return DEFAULT_TIMEZONE


async def save_user_setting(user_id: int, reminder_minutes: int):
    """Сохраняет интервал напоминаний, сохраняя существующий часовой пояс."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT timezone FROM user_settings WHERE user_id = ?', (user_id,))
        row = await cursor.fetchone()
        current_tz = row[0] if row else DEFAULT_TIMEZONE
        
        await db.execute('''
            INSERT INTO user_settings (user_id, reminder_minutes, timezone, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                reminder_minutes = excluded.reminder_minutes,
                updated_at = excluded.updated_at
        ''', (
            user_id, 
            reminder_minutes, 
            current_tz, 
            datetime.now(ZoneInfo(current_tz)).isoformat()
        ))
        await db.commit()
    logger.debug("Обновлён reminder_minutes=%d для user_id=%d (TZ сохранён: %s)", reminder_minutes, user_id, current_tz)


async def get_user_setting(user_id: int) -> int:
    """Получает интервал напоминаний пользователя (дефолт: 15)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            'SELECT reminder_minutes FROM user_settings WHERE user_id = ?',
            (user_id,)
        )
        row = await cursor.fetchone()
        return row['reminder_minutes'] if row else 15


async def get_user_full_settings(user_id: int) -> dict:
    """Получает все настройки пользователя одним запросом."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            'SELECT reminder_minutes, timezone, updated_at FROM user_settings WHERE user_id = ?',
            (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {'reminder_minutes': 15, 'timezone': DEFAULT_TIMEZONE, 'updated_at': None}
        return dict(row)


async def save_user_token(
    user_id: int,
    token_json: str,
    expires_at: str = None,
    calendar_id: str = 'primary'
):
    """Сохраняет зашифрованный токен пользователя."""
    encrypted = _encrypt_token(token_json)
    user_tz = await get_user_timezone(user_id)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO user_tokens 
            (user_id, token_json, expires_at, calendar_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            user_id, 
            encrypted, 
            expires_at, 
            calendar_id, 
            datetime.now(ZoneInfo(user_tz)).isoformat()
        ))
        await db.commit()


async def get_user_token(user_id: int) -> dict | None:
    """Возвращает расшифрованный токен пользователя или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            'SELECT token_json, expires_at, calendar_id FROM user_tokens WHERE user_id = ?',
            (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            return {
                'token_json': _decrypt_token(row['token_json']),
                'expires_at': row['expires_at'],
                'calendar_id': row['calendar_id'] or 'primary'
            }
        except Exception as e:
            logger.error("Ошибка расшифровки токена user_id=%d: %s", user_id, e)
            return None


async def delete_user_token(user_id: int):
    """Удаляет токен пользователя из БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM user_tokens WHERE user_id = ?', (user_id,))
        await db.commit()
    logger.info("Удалён токен для user_id=%d", user_id)


async def get_all_users() -> list[int]:
    """Возвращает список всех user_id с настройками."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT DISTINCT user_id FROM user_settings')
        return [row[0] for row in await cursor.fetchall()]


async def get_user_events_count(user_id: int) -> int:
    """Возвращает количество событий пользователя в БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT COUNT(*) FROM events WHERE user_id = ?',
            (user_id,)
        )
        result = await cursor.fetchone()
        return result[0] if result else 0


async def delete_user_data(user_id: int):
    """Полностью удаляет все данные пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM events WHERE user_id = ?', (user_id,))
        await db.execute('DELETE FROM user_settings WHERE user_id = ?', (user_id,))
        await db.execute('DELETE FROM user_tokens WHERE user_id = ?', (user_id,))
        await db.commit()
    logger.info("Полностью удалены данные для user_id=%d", user_id)