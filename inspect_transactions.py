"""Разовый скрипт: показывает, как выглядит ответ эндпоинта транзакций.

Запуск:  OM_API_KEY=... python inspect_transactions.py <platform_account_id>
Путь и параметры сверьте с документацией OnlyMonster и поправьте при необходимости.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

BASE = os.getenv("OM_BASE_URL", "https://omapi.onlymonster.ai")
KEY = os.environ["OM_API_KEY"]
account = sys.argv[1]

end = datetime.now(timezone.utc)
start = end - timedelta(days=1)
url = f"{BASE}/api/v0/platforms/onlyfans/accounts/{account}/transactions"
r = httpx.get(
    url,
    headers={"x-om-auth-token": KEY},
    params={"start": start.isoformat(), "end": end.isoformat()},
    timeout=30,
)
print(r.status_code)
try:
    print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:6000])
except Exception:
    print(r.text[:3000])
