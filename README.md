# 📅 Google Calendar Reminder Bot

Асинхронный Telegram-бот для персонализированных уведомлений из Google Calendar. 
Поддерживает индивидуальную OAuth2-авторизацию, шифрование токенов, гибкие напоминания, 
индивидуальные часовые пояса и устойчивую работу с API.

## ✨ Возможности
- 🔐 **Персональная авторизация** — каждый пользователь подключает свой аккаунт Google
- 🔒 **Безопасное хранение** — токены шифруются (Fernet/AES) перед записью в SQLite
- ⏱️ **Гибкие напоминания** — от 5 до 1440 минут (шаг 5 мин), inline-кнопки быстрого выбора
- 🌍 **Индивидуальные таймзоны** — алиасы городов, IANA-идентификаторы, валидация
- 🔕 **Snooze (откладывание)** — кнопка «Отложить 15 мин» прямо в уведомлении
- 🔄 **Авто-синхронизация** — фоновое обновление событий и очистка удалённых в Google
- 🛡️ **Устойчивость** — rate-limiting, retry с экспоненциальной задержкой, graceful shutdown
- 📜 **История с пагинацией** — удобная навигация по завершённым встречам
- 📁 **Ротация логов** — автосохранение в `logs/bot.log` (10 МБ/файл, 5 архивов)

## 📋 Требования
- Python `3.10`
- Telegram Bot Token (от [@BotFather](https://t.me/BotFather))
- Google Cloud Project с включённым **Google Calendar API**
- Файл `credentials.json` (OAuth 2.0 Client ID)
- VPN! (Логика под прокси в коде не реализована)

## 🚀 Установка и запуск

### 1. Клонирование и окружение
```bash
git clone <your-repo-url>
cd calendar-bot
python -m venv venv

# Linux/macOS
source venv/bin/activate
# Windows
venv\Scripts\activate

Зависимости:

pip install -r requirements.txt

Генерация ключа шифрования:

Бот требует 44-символьный Fernet-ключ. Сгенерируйте его:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Скопируйте вывод — он понадобится для .env.
 
Настройка .env:

Создайте файл .env в корне проекта:

BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TOKEN_ENCRYPTION_KEY=ваш_44_символьный_ключ_из_шага_3

Размещение credentials.json:

Поместите скачанный JSON от Google Cloud в корень проекта рядом с main.py.

Запуск:

python main.py
# или с отладочным уровнем логов
python main.py --verbose


🌐 Настройка Google Cloud Console:

Перейдите в Google Cloud Console
Создайте проект → включите Google Calendar API
Перейдите в APIs & Services → OAuth consent screen → выберите External или Internal
Добавьте scope: https://www.googleapis.com/auth/calendar.readonly
В Credentials создайте OAuth 2.0 Client ID → тип Desktop app
Скачайте JSON → переименуйте в credentials.json
В Authorized redirect URIs добавьте http://localhost (требуется для ручного ввода кода)
В OAuth consent screen выберите пункт Audience и добавьте тестовых пользователей

💬 Команды бота:

/start - Регистрация, главное меню
/auth - Получение ссылки для подключения Google Calendar
/auth_code <код> - Завершение авторизации кодом из браузера
/set_timezone <город> - Установка часового пояса (напр. Москва, MSK, Europe/Moscow)
/set_reminder [мин] - Выбор интервала напоминания (inline-кнопки или ручной ввод)
/upcoming - Предстоящие события на 24 часа
/history - История завершённых встреч (пагинация)
/delete_account - Полное удаление токена, событий и настроек
/help - Справка по командам

⚠️ Важно: Указанный через /set_timezone пояс должен совпадать с поясом вашего аккаунта Google Calendar. В противном случае время событий может смещаться.

📁 Структура проекта:

calendar-bot/
├── main.py                  # Точка входа, инициализация PTB, планировщика, логов
├── database.py              # SQLite + aiosqlite, миграции, Fernet-шифрование
├── calendar_api.py          # Обёртка Google Calendar API, retry, auth flow
├── scheduler.py             # APScheduler: авто-синхронизация, уведомления, snooze
├── handlers/
│   ├── __init__.py          # Экспорт обработчиков
│   ├── commands.py          # /start, /auth, /set_reminder, /upcoming и др.
│   └── callbacks.py         # Inline-кнопки, пагинация, snooze
├── requirements.txt         # Зависимости
├── .env                     # Токен бота + ключ шифрования (игнорируется в git)
├── credentials.json         # OAuth конфигурация Google (игнорируется в git)
├── calendar_bot.db          # Локальная БД (игнорируется в git)
└── logs/                    # Ротируемые логи (bot.log, bot.log.1...)

🛠️ Troubleshooting:

Проблема/Решение:
FATAL: TOKEN_ENCRYPTION_KEY не задан... - Убедитесь, что в .env ровно 44 символа. Пересгенерируйте ключ командой из шага 3.
credentials.json не найден - Скачайте JSON из Google Cloud Console и поместите в корень проекта.
OAuth error / Redirect URI mismatch - В Google Cloud добавьте http://localhost в Authorized redirect URIs.
События приходят с неверным временем - Проверьте совпадение пояса в Google Calendar и вывода /set_timezone.
Бот не отправляет уведомления - Проверьте, что пользователь прошёл /auth и /set_reminder. Логи: logs/bot.log.
Database is locked / OperationalError - Бот использует aiosqlite с асинхронными подключениями. Перезапустите процесс.

Примечание:

Единственная пограничная ситуация, которая может не сработать:
Сейчас: 31 декабря 23:59
Событие в БД: 01.01 00:30 (формат "дд.мм ЧЧ:ММ")
Если событие в январе, а сейчас декабрь — может не сработать.