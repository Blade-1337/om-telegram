"""OnlyMonster -> Telegram.

Источник истины: GET .../transactions (у каждой транзакции свой id).
Вебхук OnlyMonster работает как "будильник": запускает быстрый опрос.
Регулярный опрос (по умолчанию раз в 60 сек) страхует от потерянных вебхуков.
Дубли исключены: id транзакции хранится в SQLite.
"""
import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("om-tg")

OM_KEY = os.environ["OM_API_KEY"]
OM_BASE = os.getenv("OM_BASE_URL", "https://omapi.onlymonster.ai")
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT = os.environ["TG_CHAT_ID"]
WEBHOOK_SECRET = os.getenv("OM_WEBHOOK_SECRET", "")
STRICT_SIGNATURE = os.getenv("STRICT_SIGNATURE", "1") == "1"
SIG_HEADER = os.getenv("SIGNATURE_HEADER", "x-om-webhook-signature")
TS_HEADER = os.getenv("TIMESTAMP_HEADER", "x-om-webhook-timestamp")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
DB_PATH = os.getenv("DB_PATH", "sent.db")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SEC", "60"))
OVERLAP_MIN = int(os.getenv("POLL_OVERLAP_MIN", "10"))
FIRST_RUN_SILENT = os.getenv("FIRST_RUN_SILENT", "1") == "1"
EXCLUDE_TYPES = [
    s.strip().lower() for s in os.getenv("EXCLUDE_TYPES", "subscription").split(",") if s.strip()
]
ONLY_ACCOUNTS = {s.strip() for s in os.getenv("ACCOUNT_IDS", "").split(",") if s.strip()}

# ---------- хранилище ----------
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS sent (dedup_key TEXT PRIMARY KEY, sent_at TEXT)")
_db.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)")
_db.commit()


def claim(key: str) -> bool:
    cur = _db.execute(
        "INSERT OR IGNORE INTO sent VALUES (?, ?)", (key, datetime.utcnow().isoformat())
    )
    _db.commit()
    return cur.rowcount == 1


def release(key: str) -> None:
    _db.execute("DELETE FROM sent WHERE dedup_key = ?", (key,))
    _db.commit()


def get_state(k: str):
    row = _db.execute("SELECT v FROM state WHERE k = ?", (k,)).fetchone()
    return row[0] if row else None


def set_state(k: str, v: str) -> None:
    _db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (k, v))
    _db.commit()


# ---------- Telegram ----------
async def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        for attempt in range(5):
            try:
                r = await client.post(url, json={"chat_id": TG_CHAT, "text": text})
                if r.status_code == 200:
                    return
                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 2)
                    await asyncio.sleep(wait)
                    continue
                log.error("Telegram %s: %s", r.status_code, r.text)
            except httpx.HTTPError as e:
                log.error("Telegram network error: %s", e)
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("Telegram: не удалось отправить")


# ---------- OnlyMonster API ----------
_accounts_cache = {"at": 0.0, "data": []}


async def get_accounts(client: httpx.AsyncClient) -> list:
    if time.time() - _accounts_cache["at"] < 3600 and _accounts_cache["data"]:
        return _accounts_cache["data"]
    out, cursor = [], None
    while True:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = await client.get(
            f"{OM_BASE}/api/v0/accounts", headers={"x-om-auth-token": OM_KEY}, params=params
        )
        r.raise_for_status()
        body = r.json()
        out += body.get("accounts", [])
        cursor = body.get("nextCursor")
        if not cursor:
            break
    out = [
        a for a in out
        if a.get("platform") == "onlyfans"
        and (not ONLY_ACCOUNTS or str(a["platform_account_id"]) in ONLY_ACCOUNTS)
    ]
    _accounts_cache.update(at=time.time(), data=out)
    return out


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def fetch_transactions(client, pid: str, start: datetime, end: datetime) -> list:
    items, cursor, use_limit = [], None, True
    while True:
        params = {"start": iso_z(start), "end": iso_z(end)}
        if use_limit:
            params["limit"] = 100
        if cursor:
            params["cursor"] = cursor
        r = await client.get(
            f"{OM_BASE}/api/v0/platforms/onlyfans/accounts/{pid}/transactions",
            headers={"x-om-auth-token": OM_KEY},
            params=params,
        )
        if r.status_code == 400 and use_limit:
            use_limit = False  # на случай, если эндпоинт не принимает limit
            continue
        r.raise_for_status()
        body = r.json()
        items += body.get("items", [])
        cursor = body.get("cursor") or body.get("nextCursor")
        if not cursor:
            return items


# ---------- форматирование ----------
def fmt_date(value) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(value)


def tx_text(acc: dict, t: dict) -> str:
    type_ = str(t.get("type") or "—")
    low = type_.lower()
    icon = "💸" if "tip" in low else "🔓" if ("message" in low or "post" in low) else "💰"
    amount = t.get("amount")
    amount_s = f"{amount:g}" if isinstance(amount, (int, float)) else str(amount)
    name = acc.get("custom_label") or acc.get("name") or acc["platform_account_id"]
    user = f" (@{acc['username']})" if acc.get("username") else ""
    fan = (t.get("fan") or {}).get("id", "—")
    return (
        f"{icon} ${amount_s} — {type_}\n"
        f"Аккаунт: {name}{user}\n"
        f"Фан: {fan}\n"
        f"Дата: {fmt_date(t.get('timestamp'))}\n"
        f"Статус: {t.get('status', '—')}"
    )


# ---------- опрос ----------
poll_lock = asyncio.Lock()
health = {"last_ok": time.time(), "alerted": False}


async def handle_transaction(acc: dict, t: dict, silent: bool) -> bool:
    type_ = str(t.get("type") or "").lower()
    if any(x in type_ for x in EXCLUDE_TYPES):
        return True
    key = f"tx:{t['id']}"
    if not claim(key):
        return True
    if silent:
        return True
    try:
        await send_telegram(tx_text(acc, t))
        log.info("отправлено: %s (%s)", key, t.get("type"))
        return True
    except Exception:
        release(key)  # следующий опрос попробует снова
        log.exception("не отправлено: %s", key)
        return False


async def poll_once() -> bool:
    async with poll_lock:
        all_ok = True
        async with httpx.AsyncClient(timeout=30) as client:
            accounts = await get_accounts(client)
            for acc in accounts:
                pid = str(acc["platform_account_id"])
                state_key = f"last_poll:{pid}"
                last = get_state(state_key)
                first = last is None
                now = datetime.now(timezone.utc)
                if first:
                    start = now - timedelta(hours=24)
                else:
                    start = datetime.fromisoformat(last) - timedelta(minutes=OVERLAP_MIN)
                try:
                    items = await fetch_transactions(client, pid, start, now)
                except Exception:
                    log.exception("ошибка запроса транзакций, аккаунт %s", pid)
                    all_ok = False
                    continue
                items.sort(key=lambda x: str(x.get("timestamp", "")))
                silent = first and FIRST_RUN_SILENT
                if silent:
                    log.info("первый запуск аккаунта %s: %d транзакций помечены без уведомлений", pid, len(items))
                sent_ok = True
                for t in items:
                    if not await handle_transaction(acc, t, silent):
                        sent_ok = False
                if sent_ok:
                    set_state(state_key, now.isoformat())
                else:
                    all_ok = False
                await asyncio.sleep(1.1)  # бережём лимиты API
        return all_ok


async def poll_loop():
    await asyncio.sleep(2)
    while True:
        try:
            if await poll_once():
                health["last_ok"] = time.time()
                health["alerted"] = False
        except Exception:
            log.exception("сбой опроса")
        if time.time() - health["last_ok"] > 900 and not health["alerted"]:
            health["alerted"] = True
            try:
                await send_telegram("⚠️ Опрос OnlyMonster не проходит больше 15 минут. Проверьте сервис.")
            except Exception:
                log.exception("не удалось отправить алерт")
        await asyncio.sleep(POLL_INTERVAL)


_bg: set = set()
_last_trigger = {"t": 0.0}


async def delayed_polls():
    for delay in (3, 17, 40):  # через 3, 20 и 60 сек после вебхука
        await asyncio.sleep(delay)
        try:
            await poll_once()
        except Exception:
            log.exception("сбой опроса после вебхука")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


def verify(raw: bytes, timestamp: str, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        return True
    msg = timestamp.encode() + b"." + raw
    expected = hmac.new(WEBHOOK_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    ok = verify(raw, request.headers.get(TS_HEADER, ""), request.headers.get(SIG_HEADER, ""))
    if not ok:
        log.warning("подпись не совпала. Заголовки: %s", dict(request.headers))
        if STRICT_SIGNATURE:
            raise HTTPException(status_code=401, detail="bad signature")
    log.info("вебхук: %s", raw.decode(errors="replace")[:500])
    if time.time() - _last_trigger["t"] > 5:  # не плодим опросы при пачке событий
        _last_trigger["t"] = time.time()
        task = asyncio.create_task(delayed_polls())
        _bg.add(task)
        task.add_done_callback(_bg.discard)
    return {"ok": True}


@app.get("/health")
async def health_check():
    return {"status": "ok", "seconds_since_last_ok_poll": int(time.time() - health["last_ok"])}
