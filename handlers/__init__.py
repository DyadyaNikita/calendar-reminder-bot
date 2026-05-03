"""Пакет обработчиков команд Telegram-бота."""
from .commands import (
    start,
    help_command,
    auth_command,
    auth_code_command,
    set_reminder,
    set_timezone,
    upcoming_command,
    history_command,
    delete_account_command,
)

__all__ = [
    'start',
    'help_command',
    'auth_command',
    'auth_code_command',
    'set_reminder',
    'set_timezone',
    'upcoming_command',
    'history_command',
    'delete_account_command',
]