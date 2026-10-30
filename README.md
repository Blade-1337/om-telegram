# OnlyMonster → Telegram

Принимает вебхуки OnlyMonster (чаевые и PPV) и шлёт уведомления в Telegram без дублей.

## Render
- Тип: Web Service, Runtime: Python
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Environment: переменные из `.env.example`
- Health Check Path: `/health`
- Адрес вебхука: `https://<имя>.onrender.com/webhook` (вставить в OnlyMonster → API → Webhooks)

## Бот
1. @BotFather → /newbot → токен.
2. Напишите боту, откройте `https://api.telegram.org/bot<TOKEN>/getUpdates`, возьмите `chat.id`.

## Локальный запуск
    pip install -r requirements.txt
    export $(cat .env | xargs) && uvicorn main:app --reload
