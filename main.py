"""Точка входа Telegram-бота для уведомлений из Google Calendar."""
import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from handlers.commands import delete_account_command

# Загружаем .env до инициализации модулей
load_dotenv(override=True)

# Проверка ключа шифрования до импорта зависимых модулей
_KEY = os.getenv('TOKEN_ENCRYPTION_KEY', '')
if not _KEY or len(_KEY) != 44:
    print("FATAL: TOKEN_ENCRYPTION_KEY не задан или неверен в .env (ожидается 44 символа)")
    sys.exit(1)

# Настройка логирования с ротацией файлов
LOG_FILE = 'logs/bot.log'
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5

# Создаём директорию для логов, если не существует
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# Форматтер для единообразия вывода
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# RotatingFileHandler: ротация по размеру, хранение 5 архивных файлов
file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding='utf-8',
    delay=True  # Отложенное создание файла до первой записи
)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

# StreamHandler для вывода в консоль (удобно при разработке)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.INFO)

# Базовая конфигурация с двумя хендлерами
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import NetworkError, BadRequest
from database import init_db, get_all_users, DEFAULT_TIMEZONE
from handlers.commands import (
    start, help_command, auth_command, auth_code_command, 
    set_reminder, upcoming_command, history_command,
    set_timezone
)
from handlers.callbacks import (
    reminder_callback, history_callback, 
    snooze_callback, noop_callback
)
from scheduler import start_scheduler, shutdown_scheduler

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Тихая обработка сетевых сбоев и стандартных ошибок PTB."""
    if isinstance(context.error, NetworkError):
        logger.warning("Сетевой сбой Telegram API: %s. Авто-переподключение...", context.error)
        return
    if isinstance(context.error, BadRequest):
        logger.warning("BadRequest от Telegram: %s", context.error)
        return
    logger.error("Необработанная ошибка: %s", context.error, exc_info=True)


def register_handlers(application: Application):
    """Регистрирует обработчики команд и callback-запросов."""
    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("auth", auth_command))
    application.add_handler(CommandHandler("auth_code", auth_code_command))
    application.add_handler(CommandHandler("set_reminder", set_reminder))
    application.add_handler(CommandHandler("set_timezone", set_timezone))
    application.add_handler(CommandHandler("upcoming", upcoming_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("delete_account", delete_account_command))

    # Callback-запросы от inline-кнопок
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=r'^noop$'))
    application.add_handler(CallbackQueryHandler(reminder_callback, pattern=r'^remind_\d+_\d+$'))
    application.add_handler(CallbackQueryHandler(history_callback, pattern=r'^hist_\d+_\d+$'))
    application.add_handler(CallbackQueryHandler(snooze_callback, pattern=r'^snooze_'))
    
    logger.debug("Обработчики зарегистрированы")


_scheduler_instance = None


async def on_startup(application: Application):
    """Хук после инициализации бота, до начала polling."""
    global _scheduler_instance
    await init_db()
    logger.info("База данных инициализирована")
    
    _scheduler_instance = start_scheduler(application.bot)
    logger.info("Планировщик запущен")


async def on_shutdown(application: Application):
    """Хук при корректном завершении работы бота."""
    global _scheduler_instance
    await shutdown_scheduler(_scheduler_instance)
    # Синхронизируем хендлеры перед выходом
    for handler in logging.getLogger().handlers:
        handler.flush()
        handler.close()
    logger.info("Бот завершает работу")


def main():
    """Точка входа бота."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден в .env! Проверь файл .env")
        sys.exit(1)
    
    logger.info("Запуск бота @telecal_remind_bot (PID: %d)", os.getpid())
    
    builder = Application.builder().token(BOT_TOKEN)
    application = builder.build()

    application.add_error_handler(error_handler)
    
    register_handlers(application)
    
    application.post_init = on_startup
    application.post_shutdown = on_shutdown
    
    try:
        logger.info("Запуск polling-режима...")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            timeout=30,
        )
    except KeyboardInterrupt:
        logger.info("Получен сигнал прерывания (Ctrl+C)")
    except Exception as e:
        logger.error("Критическая ошибка при запуске: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Завершение работы...")


if __name__ == '__main__':
    if '--verbose' in sys.argv or '-v' in sys.argv:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Включён отладочный режим")
    main()