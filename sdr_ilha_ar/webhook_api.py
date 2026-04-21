from __future__ import annotations

import asyncio
import os
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import psycopg
import requests

from sdr_ilha_ar.channel import handle_evolution_inbound, parse_evolution_inbound
from sdr_ilha_ar import repository as repo

app = FastAPI(title="SDR Ilha Ar Webhook API", version="1.0.0")
logger = logging.getLogger(__name__)
DEBOUNCE_SECONDS = 12


@app.exception_handler(psycopg.ProgrammingError)
async def _pg_programming_error(_request: Request, exc: psycopg.ProgrammingError) -> JSONResponse:
    msg = str(exc).strip()
    if "does not exist" in msg:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Tabelas em falta no Postgres. Suba a API com a imagem atual (aplica db/schema.sql no arranque) ou rode no servidor: docker compose exec -T postgres psql -U sdr -d sdr < db/schema.sql",
                "postgres": msg,
            },
        )
    return JSONResponse(status_code=500, content={"detail": msg})


@dataclass
class PendingConversation:
    payloads: list[dict[str, Any]]
    remote_jid: str
    phone: str
    message_ids: set[str] = field(default_factory=set)
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    task: asyncio.Task | None = None


_pending_by_phone: dict[str, PendingConversation] = {}


def _validate_webhook_secret(x_webhook_secret: str | None) -> None:
    expected = (os.getenv("EVOLUTION_WEBHOOK_SECRET") or "").strip()
    if not expected:
        return
    if (x_webhook_secret or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Webhook secret inválido")


def _is_outbound_echo(payload: dict[str, Any]) -> bool:
    def _truthy(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "sim"}
        return bool(v)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = data.get("key") if isinstance(data, dict) else {}
    if isinstance(key, dict) and _truthy(key.get("fromMe")):
        return True
    if _truthy(data.get("fromMe")):
        return True
    sender = str(data.get("sender") or "")
    if sender.lower() in {"api", "system", "bot"}:
        return True
    return False


def _split_text_blocks(text: str) -> list[str]:
    chunks = [c.strip() for c in re.split(r"\n{2,}", text) if c.strip()]
    if not chunks:
        return []
    if len(chunks) == 1 and len(chunks[0]) > 220:
        sentence_chunks = re.split(r"(?<=[.!?])\s+", chunks[0])
        chunks = [c.strip() for c in sentence_chunks if c.strip()]
    return chunks


def _build_evolution_number_candidates(*, remote_jid: str, phone: str) -> list[str]:
    """Gera candidatos aceitos pelo sendText da Evolution (com/sem DDI)."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(value: str) -> None:
        v = str(value or "").strip()
        if not v or v in seen:
            return
        seen.add(v)
        out.append(v)

    jid_base = remote_jid.split("@", 1)[0].strip() if remote_jid else ""
    phone_digits = re.sub(r"\D+", "", phone or "")
    jid_digits = re.sub(r"\D+", "", jid_base)

    if phone_digits:
        _add(phone_digits)
        if not phone_digits.startswith("55") and len(phone_digits) in {10, 11}:
            _add(f"55{phone_digits}")
    if jid_digits:
        _add(jid_digits)
        if not jid_digits.startswith("55") and len(jid_digits) in {10, 11}:
            _add(f"55{jid_digits}")
    if jid_base:
        _add(jid_base)
    return out


def _send_whatsapp_reply(*, remote_jid: str, phone: str, text: str) -> None:
    base_url = (os.getenv("EVOLUTION_BASE_URL") or "").rstrip("/")
    api_key = (os.getenv("EVOLUTION_API_KEY") or "").strip()
    instance = (os.getenv("EVOLUTION_INSTANCE") or "").strip()
    if not (base_url and api_key and instance):
        logger.warning("Env da Evolution incompleta; resposta não enviada ao WhatsApp.")
        return
    endpoint = f"{base_url}/message/sendText/{instance}"
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    candidates = _build_evolution_number_candidates(remote_jid=remote_jid, phone=phone)
    if not candidates:
        logger.warning("Sem destinatário válido para Evolution (phone=%s remote_jid=%s)", phone, remote_jid)
        return
    last_error: Exception | None = None
    chunks = _split_text_blocks(text) or [text]
    for chunk in chunks:
        sent = False
        for value in candidates:
            payload = {"number": value, "text": chunk}
            try:
                response = requests.post(endpoint, headers=headers, json=payload, timeout=20)
                response.raise_for_status()
                sent = True
                break
            except Exception as exc:  # pragma: no cover - depende da API externa
                last_error = exc
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    logger.warning(
                        "Evolution rejeitou payload number=%s status=%s body=%s",
                        value,
                        exc.response.status_code,
                        (exc.response.text or "").strip()[:400],
                    )
        if not sent:
            break
        # Delay curto para envio natural em múltiplos blocos.
        if len(chunks) > 1:
            import time

            time.sleep(0.5)
    if last_error:
        raise RuntimeError(f"Falha ao enviar resposta para Evolution: {last_error}") from last_error


def _generate_elevenlabs_audio(text: str) -> bytes | None:
    api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    if not (api_key and voice_id):
        logger.warning("Credenciais ElevenLabs ausentes")
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    body = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.exception("Falha ao gerar audio ElevenLabs")
        return None

def _send_whatsapp_audio(*, remote_jid: str, phone: str, audio_bytes: bytes, text_fallback: str) -> None:
    base_url = (os.getenv("EVOLUTION_BASE_URL") or "").rstrip("/")
    api_key = (os.getenv("EVOLUTION_API_KEY") or "").strip()
    instance = (os.getenv("EVOLUTION_INSTANCE") or "").strip()
    if not (base_url and api_key and instance):
        return
    import base64
    b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
    mime = "audio/mpeg"
    b64_uri = f"data:{mime};base64,{b64_audio}"
    
    endpoint = f"{base_url}/message/sendWhatsAppAudio/{instance}"
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    candidates = _build_evolution_number_candidates(remote_jid=remote_jid, phone=phone)
    if not candidates:
        return
    
    sent = False
    for value in candidates:
        # A API Evolution aceita base64 longo diretamente assim. encoding=True força ser PTT (audio gravado na hora)
        payload = {"number": value, "audio": b64_uri, "delay": 2000, "encoding": True}
        try:
            requests.post(endpoint, headers=headers, json=payload, timeout=25).raise_for_status()
            sent = True
            break
        except Exception:
            pass
    if not sent:
        # Falback se der erro
        _send_whatsapp_reply(remote_jid=remote_jid, phone=phone, text=text_fallback)


async def _process_pending(phone: str) -> None:
    await asyncio.sleep(DEBOUNCE_SECONDS)
    pending = _pending_by_phone.get(phone)
    if not pending:
        return
    payloads = pending.payloads[:]
    remote_jid = pending.remote_jid
    _pending_by_phone.pop(phone, None)
    reply_to_send = ""
    must_reply_audio = False
    for payload in payloads:
        result = await handle_evolution_inbound(payload)
        candidate = str(result.get("reply") or "").strip()
        if candidate:
            reply_to_send = candidate
            must_reply_audio = result.get("must_reply_audio", False)
            
    if reply_to_send:
        if must_reply_audio:
            audio_data = await asyncio.to_thread(_generate_elevenlabs_audio, reply_to_send)
            if audio_data:
                _send_whatsapp_audio(remote_jid=remote_jid, phone=phone, audio_bytes=audio_data, text_fallback=reply_to_send)
            else:
                _send_whatsapp_reply(remote_jid=remote_jid, phone=phone, text=reply_to_send)
        else:
            _send_whatsapp_reply(remote_jid=remote_jid, phone=phone, text=reply_to_send)


def _enqueue_payload(*, payload: dict[str, Any], remote_jid: str, phone: str) -> None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = data.get("key") if isinstance(data, dict) else {}
    message_id = str(key.get("id") or data.get("id") or "").strip()

    pending = _pending_by_phone.get(phone)
    if pending is None:
        pending = PendingConversation(payloads=[payload], remote_jid=remote_jid, phone=phone)
        if message_id:
            pending.message_ids.add(message_id)
        _pending_by_phone[phone] = pending
    else:
        if message_id and message_id in pending.message_ids:
            return
        if message_id:
            pending.message_ids.add(message_id)
        pending.payloads.append(payload)
    pending.remote_jid = remote_jid or pending.remote_jid
    pending.last_update = datetime.now(timezone.utc)
    if pending.task and not pending.task.done():
        pending.task.cancel()
    pending.task = asyncio.create_task(_process_pending(phone))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.on_event("startup")
async def startup_event() -> None:
    try:
        repo.bootstrap_db_schema()
    except Exception:
        logger.exception("Falha ao aplicar db/schema.sql no startup")
    try:
        repo.ensure_finance_schema()
    except Exception:
        logger.exception("Falha ao garantir schema financeiro no startup")


@app.post("/webhook/whatsapp")
async def webhook_whatsapp(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    _validate_webhook_secret(x_webhook_secret)
    payload = await request.json()
    if _is_outbound_echo(payload):
        return {"status": "ignored", "reason": "outbound_echo"}
    parsed = parse_evolution_inbound(payload)
    logger.info(
        "Inbound Evolution remote_jid=%s phone=%s has_audio=%s text_len=%s",
        parsed.get("raw_remote_jid"),
        parsed.get("phone"),
        parsed.get("has_audio"),
        len(str(parsed.get("text") or "")),
    )
    phone = str(parsed.get("phone") or "").strip()
    remote_jid = str(parsed.get("raw_remote_jid") or "").strip()

    if phone:
        _enqueue_payload(payload=payload, remote_jid=remote_jid, phone=phone)
        return {"status": "queued", "delivery": "queued", "debounce_seconds": DEBOUNCE_SECONDS}
    result = await handle_evolution_inbound(payload)
    return {**result, "delivery": "skipped"}

