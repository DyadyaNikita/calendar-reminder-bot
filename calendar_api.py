"""Модуль взаимодействия с Google Calendar API."""
import os.path
import logging
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from database import get_user_token, save_user_token

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'
logger = logging.getLogger(__name__)


def parse_iso_datetime(dt_str: str) -> datetime | None:
    """Парсит ISO-строку от Google API, обрабатывая суффикс 'Z'."""
    if not dt_str:
        return None
    if dt_str.endswith('Z'):
        dt_str = dt_str[:-1] + '+00:00'
    return datetime.fromisoformat(dt_str)


def _retry_with_backoff(func, max_attempts: int = 3, base_delay: float = 1.0):
    """Выполняет функцию с экспоненциальной задержкой при ошибках 429/5xx."""
    for attempt in range(max_attempts):
        try:
            return func()
        except HttpError as e:
            if e.resp.status in [429, 500, 502, 503, 504]:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "HttpError %s: попытка %d/%d, задержка %.1fс", 
                    e.resp.status, attempt + 1, max_attempts, delay
                )
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "Ошибка: %s, попытка %d/%d, задержка %.1fс", 
                e, attempt + 1, max_attempts, delay
            )
            time.sleep(delay)
    return None


def _build_service(creds: Credentials):
    """Создаёт клиент Google Calendar API v3."""
    return build('calendar', 'v3', credentials=creds)


def get_calendar_service():
    """Получает сервис для глобального токена (файл token.json)."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
                    f.write(creds.to_json())
                logger.info("Токен обновлён (файл)")
            except Exception as e:
                logger.warning("Не удалось обновить токен: %s", e)
                creds = None
        else:
            logger.info("Запуск OAuth-авторизации (файл)...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
            if creds:
                with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
                    f.write(creds.to_json())
    
    return _build_service(creds) if creds else None


async def get_service_for_user(user_id: int):
    """Получает сервис для пользователя из БД с авто-обновлением токена."""
    token_data = await get_user_token(user_id)
    if not token_data:
        logger.debug("Токен не найден для user_id=%s", user_id)
        return None
    
    try:
        creds = Credentials.from_authorized_user_info(
            json.loads(token_data['token_json']), 
            SCOPES
        )
    except Exception as e:
        logger.error("Ошибка парсинга токена user_id=%s: %s", user_id, e)
        return None
    
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            await save_user_token(
                user_id=user_id,
                token_json=creds.to_json(),
                expires_at=creds.expiry.isoformat() if creds.expiry else None,
                calendar_id=token_data.get('calendar_id', 'primary')
            )
            logger.info("Токен обновлён для user_id=%s", user_id)
        except Exception as e:
            logger.error("Не удалось обновить токен user_id=%s: %s", user_id, e)
            return None
    
    if not creds.valid:
        logger.warning("Токен недействителен для user_id=%s", user_id)
        return None
    
    return _build_service(creds)


async def get_upcoming_events(
    hours: int = 24, 
    calendar_id: str = 'primary', 
    user_id: int = None, 
    user_timezone: str = 'America/New_York'
) -> list[dict]:
    """
    Получает предстоящие события из Google Calendar.
    Возвращает список событий с временем в целевом часовом поясе.
    """
    try:
        service = await get_service_for_user(user_id) if user_id else get_calendar_service()
        if not service:
            return []
        
        user_tz = ZoneInfo(user_timezone)
        now_user = datetime.now(user_tz)
        end_user = now_user + timedelta(hours=hours)

        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=now_user.isoformat(),
            timeMax=end_user.isoformat(),
            singleEvents=True,
            orderBy='startTime',
            timeZone=user_timezone
        ).execute()

        result = []
        for event in events_result.get('items', []):
            start_raw = event['start'].get('dateTime', event['start'].get('date'))
            end_raw = event['end'].get('dateTime', event['end'].get('date'))
            
            if 'T' in start_raw:
                start_dt = parse_iso_datetime(start_raw)
                end_dt = parse_iso_datetime(end_raw)
                
                if start_dt is None or end_dt is None:
                    continue
                
                start_local = start_dt.astimezone(user_tz)
                end_local = end_dt.astimezone(user_tz)
                start_str = start_local.strftime("%d.%m %H:%M")
                end_str = end_local.strftime("%d.%m %H:%M")
            else:
                start_str = start_raw
                end_str = end_raw

            result.append({
                'title': event.get('summary', 'Без названия'),
                'start_time': start_str,
                'end_time': end_str,
                'link': event.get('hangoutLink', ''),
                'description': event.get('description', ''),
                'location': event.get('location', ''),
                'id': event.get('id'),
            })
        return result
        
    except HttpError as e:
        logger.error(
            "Ошибка Google API: %s (status=%s)", 
            e, e.resp.status if hasattr(e, 'resp') else 'N/A'
        )
        return []
    except Exception as e:
        logger.error("Неожиданная ошибка: %s", e, exc_info=True)
        return []


async def get_past_events(
    limit: int = 10, 
    calendar_id: str = 'primary', 
    user_id: int = None, 
    user_timezone: str = 'America/New_York'
) -> list[dict]:
    """
    Получает прошедшие события из Google Calendar за последние 14 дней.
    Возвращает отсортированный список (сначала самые свежие).
    """
    try:
        service = await get_service_for_user(user_id) if user_id else get_calendar_service()
        if not service:
            return []
        
        user_tz = ZoneInfo(user_timezone)
        now_user = datetime.now(user_tz)
        time_min = (now_user - timedelta(days=14)).isoformat()
        time_max = now_user.isoformat()

        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
            timeZone=user_timezone
        ).execute()

        events = events_result.get('items', [])
        past_events = []

        for event in events:
            start_raw = event['start'].get('dateTime', event['start'].get('date'))
            end_raw = event['end'].get('dateTime', event['end'].get('date'))
            
            if 'T' in end_raw:
                end_dt = parse_iso_datetime(end_raw)
                if end_dt is None:
                    continue
                    
                if end_dt < now_user:
                    if 'T' in start_raw:
                        start_dt = parse_iso_datetime(start_raw)
                        if start_dt:
                            start_local = start_dt.astimezone(user_tz)
                            end_local = end_dt.astimezone(user_tz)
                            start_str = start_local.strftime("%d.%m %H:%M")
                            end_str = end_local.strftime("%d.%m %H:%M")
                        else:
                            start_str = start_raw
                            end_str = end_raw
                    else:
                        start_str = start_raw
                        end_str = end_raw
                    
                    past_events.append({
                        'title': event.get('summary', 'Без названия'),
                        'start_time': start_str,
                        'end_time': end_str,
                        'link': event.get('hangoutLink', ''),
                        'description': event.get('description', ''),
                        'id': event.get('id'),
                        '_end_dt': end_dt
                    })

        past_events.sort(key=lambda x: x['_end_dt'], reverse=True)
        
        for e in past_events:
            del e['_end_dt']
            
        return past_events[:limit]
        
    except Exception as e:
        logger.error("Ошибка при получении прошедших событий: %s", e, exc_info=True)
        return []