"""Показывает ваши аккаунты и их platform_account_id.

Windows (cmd):
    set OM_API_KEY=ваш_ключ
    python list_accounts.py
"""
import os

import httpx

BASE = os.getenv("OM_BASE_URL", "https://omapi.onlymonster.ai")
KEY = os.environ["OM_API_KEY"]

r = httpx.get(
    f"{BASE}/api/v0/accounts",
    headers={"x-om-auth-token": KEY},
    params={"limit": 1000},
    timeout=30,
)
print("HTTP", r.status_code)
if r.status_code != 200:
    print(r.text[:1000])
    raise SystemExit(1)

for a in r.json().get("accounts", []):
    print(
        f"id={a['id']:<8} platform_account_id={a['platform_account_id']:<14} "
        f"{a['platform']:<9} @{a['username']}  ({a.get('custom_label') or a['name']})"
    )
