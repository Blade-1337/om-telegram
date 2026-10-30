import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("om-tg")

SECRET = os.environ["OM_WEBHOOK_SECRET"]
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT = os.environ["TG_CHAT_ID"]
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
DB_PATH = os.getenv("DB_PATH", "sent.db")
SIG_HEADER = os.getenv("SIGNATURE_HEADER", "x-om-webhook-signature")
TS_HEADER = os.getenv("TIMESTAMP_HEADER", "x-om-webhook-timestamp")

app = FastAPI()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS sent (dedup_key TEXT PRIMARY KEY, sent_at TEXT)")
_db.commit()
_lock = asyncio.Lock()


def claim(key: str) -> bool:
    """True, если ключ новый (можно отправлять)."""
    cur = _db.execute(
        "INSERT OR IGNORE INTO sent VALUES (?, ?)", (key, datetime.utcnow().isoformat())
    )
    _db.commit()
    return cur.rowcount == 1


def release(key: str) -> None:
    _db.execute("DELETE FROM sent WHERE dedup_key = ?", (key,))
    _db.commit()


def verify(raw: bytes, timestamp: str, signature: str) -> bool:
    msg = timestamp.encode() + b"." + raw
    expected = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
    signature = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, signature)


def fmt_date(value) -> str:
    if not value:
        return "—"
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)


def build_message(payload: dict):
    """Возвращает (dedup_key, текст) или None, если событие не нужно."""
    event = str(payload.get("event") or payload.get("type") or "")
    data = payload.get("data") or payload.get("payload") or payload
    when = fmt_date(data.get("timestamp") or payload.get("timestamp") or data.get("created_at"))
    account = data.get("account_id") or data.get("platform_account_id") or "—"
    fan = data.get("fan_id", "—")

    if event == "fans.tip.received":
        key = f"tip:{data.get('tip_id')}"
        text = (
            f"💸 Чаевые ${data.get('amount_gross')}\n"
            f"Аккаунт: {account}\nФан: {fan}\nДата: {when}"
        )
        return key, text

    if event == "fans.ppv.purchased":
        key = (
            f"ppv:{account}:{data.get('content_type')}:{data.get('content_id')}:{fan}"
        )
        text = (
            f"🔓 PPV ${data.get('price_gross')}\n"
            f"Аккаунт: {account}\nФан: {fan}\n"
            f"Контент: {data.get('content_type')} #{data.get('content_id')}\nДата: {when}"
        )
        return key, text

    return None


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


async def process(payload: dict) -> None:
    built = build_message(payload)
    if not built:
        return
    key, text = built
    async with _lock:
        if not claim(key):
            log.info("дубль, пропуск: %s", key)
            return
        try:
            await send_telegram(text)
            log.info("отправлено: %s", key)
        except Exception:
            release(key)  # чтобы повтор/опрос мог отправить позже
            log.exception("не отправлено: %s", key)


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    ts = request.headers.get(TS_HEADER, "")
    sig = request.headers.get(SIG_HEADER, "")
    if not verify(raw, ts, sig):
        log.warning("неверная подпись. Заголовки: %s", dict(request.headers))
        raise HTTPException(status_code=401, detail="bad signature")
    payload = json.loads(raw)
    log.info("вебхук: %s", raw.decode()[:1000])
    background.add_task(process, payload)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}
