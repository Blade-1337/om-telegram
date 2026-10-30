# OnlyMonster → Telegram

Уведомления в Telegram о новых транзакциях (чаевые, PPV и др.) без дублей и потерь.

Как работает: вебхук OnlyMonster запускает быстрый опрос эндпоинта транзакций (через 3, 20 и 60 сек),
плюс регулярный опрос раз в минуту. Каждая транзакция отправляется один раз (по её id).

## Render
- Web Service, Runtime: Python
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/health`
- Environment: переменные из `.env.example`
- Вебхук: `https://<имя>.onrender.com/webhook` → OnlyMonster → API → Webhooks

## Типы транзакций
Тип приходит строкой (например `Payment for message`). Не нужные типы исключаются через `EXCLUDE_TYPES`.
Смотрите логи: незнакомые типы видны в строках "отправлено: tx:... (тип)".
