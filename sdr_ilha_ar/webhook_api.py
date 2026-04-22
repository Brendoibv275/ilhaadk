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
_handoff_cache: dict[str, bool] = {}


def _resolve_external_channel() -> str:
    instance = str(os.getenv("EVOLUTION_INSTANCE") or "").strip().lower()
    if instance:
        return f"whatsapp:{instance}"
    return "whatsapp"


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "sim"}
    return bool(v)


def _payload_data(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("data") if isinstance(payload.get("data"), dict) else payload


def _is_from_me(payload: dict[str, Any]) -> bool:
    data = _payload_data(payload)
    key = data.get("key") if isinstance(data, dict) else {}
    return (isinstance(key, dict) and _truthy(key.get("fromMe"))) or _truthy(data.get("fromMe"))


def _validate_webhook_secret(x_webhook_secret: str | None) -> None:
    expected = (os.getenv("EVOLUTION_WEBHOOK_SECRET") or "").strip()
    if not expected:
        return
    if (x_webhook_secret or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Webhook secret inválido")


def _is_outbound_echo(payload: dict[str, Any]) -> bool:
    data = _payload_data(payload)
    if not _is_from_me(payload):
        return False
    sender = str(data.get("sender") or "")
    source = str(data.get("source") or data.get("sourceType") or "")
    return sender.lower() in {"api", "system", "bot"} or source.lower() in {"api", "system", "bot"}


def _resume_commands() -> tuple[str, ...]:
    raw = (repo.settings.human_handoff_manual_resume_commands or "").strip()
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else ("#retomarbot", "#retomar", "/retomar")


def _is_resume_command(text: str) -> bool:
    if not repo.settings.human_handoff_allow_manual_resume:
        return False
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    return msg in _resume_commands()


def _clear_pending_conversation(conversation_key: str) -> None:
    pending = _pending_by_phone.pop(conversation_key, None)
    if pending and pending.task and not pending.task.done():
        pending.task.cancel()


def _activate_handoff_pause(conversation_key: str) -> None:
    _clear_pending_conversation(conversation_key)
    _handoff_cache[conversation_key] = True


def _is_handoff_paused(conversation_key: str) -> bool:
    return _handoff_cache.get(conversation_key, False)


def _clear_handoff_pause(conversation_key: str) -> None:
    _handoff_cache.pop(conversation_key, None)


def _finalization_patterns() -> tuple[str, ...]:
    raw = repo.settings.human_handoff_finalize_patterns or ""
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return tuple(parts)


def _is_human_finalization_text(text: str) -> bool:
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    return any(p in msg for p in _finalization_patterns())


def _stage_rank(stage: str) -> int:
    order = {
        "new": 0,
        "qualified": 1,
        "quoted": 2,
        "awaiting_slot": 3,
        "scheduled": 4,
        "completed": 5,
        "lost": -1,
        "emergency_handoff": 99,
    }
    return order.get(str(stage or "").strip().lower(), -1)


def _can_activate_handoff(lead_id: Any) -> bool:
    min_outbound = max(0, int(repo.settings.human_handoff_min_bot_outbound or 0))
    min_stage = str(repo.settings.human_handoff_min_stage or "qualified").strip().lower()
    lead = repo.get_lead(lead_id) or {}
    outbound_count = repo.count_messages_by_roles(
        lead_id, ("assistant_outbound_stub", "assistant_outbound")
    )
    if outbound_count >= min_outbound:
        return True
    return _stage_rank(str(lead.get("stage") or "")) >= _stage_rank(min_stage)


def _resolve_lead_id(phone: str) -> Any:
    external_channel = _resolve_external_channel()
    if not phone:
        return None
    lead = repo.get_lead_by_external(external_channel, phone)
    if lead:
        return lead.get("id")
    return repo.ensure_lead(external_channel=external_channel, external_user_id=phone)


def _sync_handoff_cache(conversation_key: str, lead_id: Any) -> bool:
    state = repo.get_handoff_state(lead_id)
    active = bool(state.get("active"))
    if active:
        _handoff_cache[conversation_key] = True
    else:
        _handoff_cache.pop(conversation_key, None)
    return active


def _append_outbound_trace(phone: str, text: str, *, channel: str = "whatsapp") -> None:
    lead_id = _resolve_lead_id(phone)
    if not lead_id:
        return
    try:
        repo.append_message(
            lead_id=lead_id,
            role="assistant_outbound_stub",
            body=str(text or "")[:4000],
            metadata={"channel": channel},
        )
    except Exception:
        logger.exception("Falha ao registrar outbound_stub no histórico")


def _split_text_blocks(text: str) -> list[str]:
    chunks = [c.strip() for c in re.split(r"\n{2,}", text) if c.strip()]
    if not chunks:
        return []
    if len(chunks) == 1 and len(chunks[0]) > 220:
        sentence_chunks = re.split(r"(?<=[.!?])\s+", chunks[0])
        chunks = [c.strip() for c in sentence_chunks if c.strip()]
    return chunks


def _build_evolution_number_candidates(*, remote_jid: str, phone: str) -> list[str]:
    """Gera candidatos para envio priorizando o remote_jid do webhook."""
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

    # Padrao: responder sempre com base no remote_jid vindo da Evolution.
    # Mantemos "phone" apenas como ultimo fallback quando o jid vier vazio.
    if jid_digits:
        _add(jid_digits)
        if not jid_digits.startswith("55") and len(jid_digits) in {10, 11}:
            _add(f"55{jid_digits}")
    if jid_base:
        _add(jid_base)
    if not out and phone_digits:
        _add(phone_digits)
        if not phone_digits.startswith("55") and len(phone_digits) in {10, 11}:
            _add(f"55{phone_digits}")
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


def _contains_critical_numbers(text: str) -> bool:
    t = str(text or "")
    if not t:
        return False
    return bool(re.search(r"\d", t))


def _apply_maranhao_speech_style(text: str) -> str:
    """
    Ajuste leve de pronúncia para TTS, apenas no áudio.
    Mantém legibilidade no texto normal.
    """
    if (os.getenv("TTS_MARANHAO_STYLE") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return text
    out = str(text or "")
    # Regras suaves e localizadas para não distorcer totalmente a mensagem.
    replacements = {
        "pista": "pixta",
        "esteira": "exteira",
        "suporte": "xuporte",
        "serviço": "xerviço",
        "servico": "xervico",
    }
    for src, dst in replacements.items():
        out = re.sub(rf"\b{re.escape(src)}\b", dst, out, flags=re.IGNORECASE)
    return out

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
    
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    candidates = _build_evolution_number_candidates(remote_jid=remote_jid, phone=phone)
    if not candidates:
        logger.warning("Sem destinatário para envio de áudio (phone=%s remote_jid=%s)", phone, remote_jid)
        return

    endpoints = (
        f"{base_url}/message/sendWhatsAppAudio/{instance}",
        f"{base_url}/message/sendPtt/{instance}",
    )
    sent = False
    last_error: Exception | None = None
    for value in candidates:
        raw_payload = {"number": value, "audio": b64_audio, "delay": 2000, "encoding": True}
        data_uri_payload = {"number": value, "audio": b64_uri, "delay": 2000, "encoding": True}
        for endpoint in endpoints:
            for payload in (raw_payload, data_uri_payload):
                try:
                    response = requests.post(endpoint, headers=headers, json=payload, timeout=25)
                    response.raise_for_status()
                    logger.info(
                        "Áudio enviado para number=%s endpoint=%s data_uri=%s",
                        value,
                        endpoint,
                        payload is data_uri_payload,
                    )
                    sent = True
                    break
                except Exception as exc:
                    last_error = exc
                    if isinstance(exc, requests.HTTPError) and exc.response is not None:
                        logger.warning(
                            "Falha envio áudio number=%s endpoint=%s status=%s body=%s",
                            value,
                            endpoint,
                            exc.response.status_code,
                            (exc.response.text or "").strip()[:500],
                        )
                    else:
                        logger.warning(
                            "Falha envio áudio number=%s endpoint=%s erro=%s",
                            value,
                            endpoint,
                            exc,
                        )
            if sent:
                break
        if sent:
            break
    if not sent:
        logger.error(
            "Não foi possível enviar áudio via Evolution; aplicando fallback em texto. phone=%s remote_jid=%s erro=%s",
            phone,
            remote_jid,
            last_error,
        )
        # Fallback: se a Evolution recusar áudio, ao menos garantimos resposta textual.
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
            # Se qualquer evento no lote veio por áudio, preserva resposta em áudio.
            must_reply_audio = must_reply_audio or bool(result.get("must_reply_audio", False))
            
    if reply_to_send:
        if must_reply_audio:
            # Exceção pedida: se a resposta tiver números relevantes, enviar texto para clareza.
            if _contains_critical_numbers(reply_to_send):
                _send_whatsapp_reply(remote_jid=remote_jid, phone=phone, text=reply_to_send)
            else:
                tts_text = _apply_maranhao_speech_style(reply_to_send)
                audio_data = await asyncio.to_thread(_generate_elevenlabs_audio, tts_text)
                if audio_data:
                    _send_whatsapp_audio(remote_jid=remote_jid, phone=phone, audio_bytes=audio_data, text_fallback=reply_to_send)
                else:
                    _send_whatsapp_reply(remote_jid=remote_jid, phone=phone, text=reply_to_send)
        else:
            _send_whatsapp_reply(remote_jid=remote_jid, phone=phone, text=reply_to_send)
        _append_outbound_trace(phone=phone, text=reply_to_send)


def _enqueue_payload(*, payload: dict[str, Any], remote_jid: str, phone: str) -> None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = data.get("key") if isinstance(data, dict) else {}
    message_id = str(key.get("id") or data.get("id") or "").strip()

    conversation_key = (remote_jid or phone).strip()
    if not conversation_key:
        return

    pending = _pending_by_phone.get(conversation_key)
    if pending is None:
        pending = PendingConversation(payloads=[payload], remote_jid=remote_jid, phone=phone)
        if message_id:
            pending.message_ids.add(message_id)
        _pending_by_phone[conversation_key] = pending
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
    pending.task = asyncio.create_task(_process_pending(conversation_key))


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
    conversation_key = (remote_jid or phone).strip()
    lead_id = _resolve_lead_id(phone) if phone else None

    if _is_from_me(payload):
        text = str(parsed.get("text") or "").strip()
        if _is_outbound_echo(payload):
            return {"status": "ignored", "reason": "outbound_echo"}
        if conversation_key and lead_id:
            if _is_resume_command(text) or _is_human_finalization_text(text):
                repo.set_handoff_state(lead_id, active=False, activated_by="human", reason="finalized")
                repo.append_message(
                    lead_id=lead_id,
                    role="tool",
                    body="Human handoff finalizado; bot reativado.",
                    metadata={"event": "human_handoff_resumed", "text": text[:120]},
                )
                _clear_handoff_pause(conversation_key)
                return {"status": "ok", "reason": "human_handoff_resumed"}
            if not _can_activate_handoff(lead_id):
                return {"status": "ignored", "reason": "human_handoff_gate_blocked"}

            repo.set_handoff_state(lead_id, active=True, activated_by="human", reason="from_me_message")
            confirmed = repo.confirm_latest_appointment_for_lead(lead_id)
            promoted = repo.promote_lead_stage_on_handoff(lead_id)
            repo.cancel_pending_jobs_for_lead(lead_id, job_type="abandonment_check")
            repo.cancel_pending_jobs_for_lead(lead_id, job_type="send_followup")
            repo.append_message(
                lead_id=lead_id,
                role="tool",
                body="Human handoff ativado com autopromoção de agendamento.",
                metadata={
                    "event": "human_handoff_activated",
                    "appointment_confirmed": bool(confirmed),
                    "lead_promoted_to_scheduled": bool(promoted),
                },
            )
            _activate_handoff_pause(conversation_key)
            return {"status": "ok", "reason": "human_handoff_activated"}
        return {"status": "ignored", "reason": "outbound_without_recipient"}

    if conversation_key and lead_id and (_is_handoff_paused(conversation_key) or _sync_handoff_cache(conversation_key, lead_id)):
        return {"status": "ignored", "reason": "human_handoff_active"}

    if remote_jid or phone:
        _enqueue_payload(payload=payload, remote_jid=remote_jid, phone=phone)
        return {"status": "queued", "delivery": "queued", "debounce_seconds": DEBOUNCE_SECONDS}
    result = await handle_evolution_inbound(payload)
    return {**result, "delivery": "skipped"}

