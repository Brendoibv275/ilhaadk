from __future__ import annotations

import asyncio
import os
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from datetime import date as date_cls

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import psycopg
import requests

from sdr_ilha_ar.channel import handle_evolution_inbound, parse_evolution_inbound
from sdr_ilha_ar import repository as repo

app = FastAPI(title="SDR Ilha Ar Webhook API", version="1.0.0")

# CORS — permite front-ib (Netlify + localhost) bater na API.
# Origens configuráveis via env CORS_ALLOWED_ORIGINS (csv). Se não definido,
# aceita dev local + domínio oficial do front. Em prod real, restrinja.
_cors_env = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _cors_origins = [
        "https://ilhabreese.netlify.app",
        "http://localhost:5173",
        "http://localhost:4173",
        "http://127.0.0.1:5173",
    ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger = logging.getLogger(__name__)
# I/2 — Inbox único com debounce de 20s.
# Mensagens do mesmo lead (mesma conversation_key) que chegam dentro dessa
# janela são agregadas numa única chamada ao LLM, evitando múltiplas respostas
# quando o cliente manda "oi", "tudo bem?", "preciso de orçamento" em poucos
# segundos. Implementação em memória (asyncio.Task por conversa) — funciona
# com uma réplica; pra escalar horizontal troca pra Redis/Postgres.
DEBOUNCE_SECONDS = 20


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
    external_channel: str = "whatsapp"
    evolution_instance: str = ""
    message_ids: set[str] = field(default_factory=set)
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    task: asyncio.Task | None = None


_pending_by_phone: dict[str, PendingConversation] = {}
_handoff_cache: dict[str, bool] = {}

# I/4 — Healthcheck: trackeamos o último inbound/processamento em memória pra
# complementar o banco (que pode não estar acessível no momento do /health).
_last_inbound_received_at: datetime | None = None
_last_inbound_processed_at: datetime | None = None


def _resolve_external_channel(evolution_instance: str = "") -> str:
    # Operação single-instance: canal único para todos os leads.
    return "whatsapp"


def _resolve_evolution_api_key(evolution_instance: str) -> str:
    # Mantemos a assinatura para compatibilidade de chamadas existentes.
    return str(os.getenv("EVOLUTION_API_KEY") or "").strip()


def _is_allowed_instance(inbound_instance: str) -> bool:
    configured = str(os.getenv("EVOLUTION_INSTANCE") or "").strip().lower()
    incoming = str(inbound_instance or "").strip().lower()
    if not configured:
        return True
    if not incoming:
        return False
    return incoming == configured


def _extract_provider_message_id(payload: dict[str, Any]) -> str:
    """
    I/1 — Extrai o provider_message_id do payload Evolution/Meta.
    Retorna string vazia se não encontrar (fallback: processa normal).
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if isinstance(data, dict):
        key = data.get("key") if isinstance(data.get("key"), dict) else {}
        for candidate in (
            key.get("id") if isinstance(key, dict) else None,
            data.get("id"),
            data.get("messageId"),
            data.get("message_id"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    # Top-level (Meta Cloud etc.)
    for candidate in (payload.get("id"), payload.get("messageId")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "sim"}
    return bool(v)


def _normalize_phone_digits(value: str) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if digits.startswith("55") and len(digits) > 10:
        digits = digits[2:]
    return digits


def _ignored_inbound_numbers() -> set[str]:
    raw = str(os.getenv("IGNORED_WHATSAPP_NUMBERS") or "").strip()
    if not raw:
        return set()
    out: set[str] = set()
    for part in raw.split(","):
        norm = _normalize_phone_digits(part.strip())
        if norm:
            out.add(norm)
    return out


def _is_ignored_inbound_phone(phone: str) -> bool:
    norm = _normalize_phone_digits(phone)
    return bool(norm) and norm in _ignored_inbound_numbers()


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


def _resolve_lead_id(phone: str, evolution_instance: str = "") -> Any:
    external_channel = _resolve_external_channel(evolution_instance)
    if not phone:
        return None
    repo.reconcile_whatsapp_instance_channel(phone, external_channel)
    lead = repo.get_lead_by_external(external_channel, phone)
    if lead:
        return lead.get("id")
    return repo.ensure_lead(external_channel, phone)


def _sync_handoff_cache(conversation_key: str, lead_id: Any) -> bool:
    active = repo.is_bot_paused(lead_id)
    if active:
        _handoff_cache[conversation_key] = True
    else:
        _handoff_cache.pop(conversation_key, None)
    return active


def _clear_handoff_pause_for_lead(lead: dict[str, Any]) -> None:
    external_user_id = str(lead.get("external_user_id") or "").strip()
    if not external_user_id:
        return
    for key in list(_handoff_cache.keys()):
        if external_user_id in key:
            _handoff_cache.pop(key, None)
    for key in list(_pending_by_phone.keys()):
        if external_user_id in key:
            _clear_pending_conversation(key)


def _append_outbound_trace(phone: str, text: str, *, channel: str = "whatsapp", evolution_instance: str = "") -> None:
    lead_id = _resolve_lead_id(phone, evolution_instance=evolution_instance)
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


def _is_retriable_network_error(exc: Exception) -> bool:
    """
    I/3 — Classifica exceções que devem ser retentadas no envio WhatsApp.
    Consideramos retriável: ConnectionError, Timeout, e HTTPError com status 5xx.
    Erros 4xx (payload ruim, número inválido) não são retentados.
    """
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        return bool(status is not None and 500 <= int(status) < 600)
    return False


def _retry_network_call(
    func,
    *args,
    max_attempts: int = 3,
    backoff_seconds: tuple[float, ...] = (1.0, 4.0, 16.0),
    op_name: str = "network_call",
    **kwargs,
):
    """
    I/3 — Executa `func(*args, **kwargs)` com retry exponencial em erros de
    rede. Tenta até `max_attempts` vezes, dormindo `backoff_seconds[i]` antes
    da próxima tentativa. Só retenta em erros classificados como retriáveis.
    Loga estruturadamente cada retry.
    """
    import time

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            retriable = _is_retriable_network_error(exc)
            if not retriable or attempt >= max_attempts:
                logger.warning(
                    "I/3 retry: %s falhou definitivamente attempt=%s/%s retriable=%s erro=%s",
                    op_name,
                    attempt,
                    max_attempts,
                    retriable,
                    exc,
                )
                raise
            delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
            logger.warning(
                "I/3 retry: %s attempt=%s/%s falhou (retriable=%s) — retry em %.1fs. erro=%s",
                op_name,
                attempt,
                max_attempts,
                retriable,
                delay,
                exc,
            )
            time.sleep(delay)
    if last_exc:
        raise last_exc


def _post_whatsapp_text(*, endpoint: str, headers: dict, payload: dict) -> requests.Response:
    """I/3 — Wrapper que isola a chamada HTTP para ficar retentável."""
    response = requests.post(endpoint, headers=headers, json=payload, timeout=20)
    response.raise_for_status()
    return response


def _send_whatsapp_reply(*, remote_jid: str, phone: str, text: str, evolution_instance: str = "") -> None:
    base_url = (os.getenv("EVOLUTION_BASE_URL") or "").rstrip("/")
    instance = str(evolution_instance or os.getenv("EVOLUTION_INSTANCE") or "").strip()
    api_key = _resolve_evolution_api_key(instance)
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
                # I/3 — retry automático com backoff (1s, 4s, 16s) em erros de rede/5xx.
                _retry_network_call(
                    _post_whatsapp_text,
                    endpoint=endpoint,
                    headers=headers,
                    payload=payload,
                    op_name=f"whatsapp_send_text number={value}",
                )
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

def _send_whatsapp_audio(
    *,
    remote_jid: str,
    phone: str,
    audio_bytes: bytes,
    text_fallback: str,
    evolution_instance: str = "",
) -> None:
    base_url = (os.getenv("EVOLUTION_BASE_URL") or "").rstrip("/")
    instance = str(evolution_instance or os.getenv("EVOLUTION_INSTANCE") or "").strip()
    api_key = _resolve_evolution_api_key(instance)
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


async def _process_pending(conversation_key: str) -> None:
    await asyncio.sleep(DEBOUNCE_SECONDS)
    pending = _pending_by_phone.get(conversation_key)
    if not pending:
        return
    payloads = pending.payloads[:]
    lead_phone = pending.phone
    remote_jid = pending.remote_jid
    external_channel = pending.external_channel
    evolution_instance = pending.evolution_instance
    message_ids_processed = list(pending.message_ids)
    _pending_by_phone.pop(conversation_key, None)
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
                _send_whatsapp_reply(
                    remote_jid=remote_jid,
                    phone=lead_phone,
                    text=reply_to_send,
                    evolution_instance=evolution_instance,
                )
            else:
                tts_text = _apply_maranhao_speech_style(reply_to_send)
                audio_data = await asyncio.to_thread(_generate_elevenlabs_audio, tts_text)
                if audio_data:
                    _send_whatsapp_audio(
                        remote_jid=remote_jid,
                        phone=lead_phone,
                        audio_bytes=audio_data,
                        text_fallback=reply_to_send,
                        evolution_instance=evolution_instance,
                    )
                else:
                    _send_whatsapp_reply(
                        remote_jid=remote_jid,
                        phone=lead_phone,
                        text=reply_to_send,
                        evolution_instance=evolution_instance,
                    )
        else:
            _send_whatsapp_reply(
                remote_jid=remote_jid,
                phone=lead_phone,
                text=reply_to_send,
                evolution_instance=evolution_instance,
            )
        _append_outbound_trace(
            phone=lead_phone,
            text=reply_to_send,
            channel=external_channel,
            evolution_instance=evolution_instance,
        )

    # I/1 — marca processed_at no banco para cada provider_message_id do lote.
    for pmid in message_ids_processed:
        try:
            repo.mark_message_processed(pmid)
        except Exception:
            logger.exception("I/1: falha ao marcar mensagem como processada pmid=%s", pmid)

    # I/4 — Healthcheck: guarda última atividade processada.
    global _last_inbound_processed_at
    _last_inbound_processed_at = datetime.now(timezone.utc)


def _enqueue_payload(
    *,
    payload: dict[str, Any],
    remote_jid: str,
    phone: str,
    external_channel: str,
    evolution_instance: str,
) -> None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = data.get("key") if isinstance(data, dict) else {}
    message_id = str(key.get("id") or data.get("id") or "").strip()

    conversation_key = f"{evolution_instance}|{(remote_jid or phone).strip()}"
    if not conversation_key:
        return

    pending = _pending_by_phone.get(conversation_key)
    if pending is None:
        pending = PendingConversation(
            payloads=[payload],
            remote_jid=remote_jid,
            phone=phone,
            external_channel=external_channel,
            evolution_instance=evolution_instance,
        )
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
    pending.external_channel = external_channel or pending.external_channel
    pending.evolution_instance = evolution_instance or pending.evolution_instance
    pending.last_update = datetime.now(timezone.utc)
    if pending.task and not pending.task.done():
        pending.task.cancel()
    pending.task = asyncio.create_task(_process_pending(conversation_key))


def _is_business_hours_sl(ref: datetime | None = None) -> bool:
    """
    I/4 — Retorna True se o horário em São Luís/SP (UTC-3, sem DST) está entre
    8h e 18h. Extraído como função pra facilitar testes (pode ser monkeypatch).
    """
    moment = ref or datetime.now(timezone.utc)
    sl_hour = (moment.astimezone(timezone(timedelta(hours=-3)))).hour
    return 8 <= sl_hour < 18


@app.get("/health")
async def health() -> dict[str, Any]:
    """
    I/4 — Healthcheck enriquecido.

    Retorna:
    - status: "ok" | "degraded" | "down"
    - last_msg_received_at: ISO8601 | null  (último webhook aceito)
    - last_msg_processed_at: ISO8601 | null (último LLM processou)
    - workers_alive: int  (tasks de debounce pendentes, proxy de atividade)
    - time_since_last_msg_seconds: float | null

    Regra de "degraded": em horário comercial (8h-18h hora de São Luís/SP,
    UTC-3), se já recebemos alguma mensagem e o último recebimento foi há
    mais de 600s (10 min), consideramos degradado. Se nunca recebemos nada e
    o servidor acabou de subir, status="ok".
    """
    now = datetime.now(timezone.utc)

    # Preferência: dados em memória (mais frescos). Fallback: Postgres.
    last_received: datetime | None = _last_inbound_received_at
    last_processed: datetime | None = _last_inbound_processed_at

    if last_received is None or last_processed is None:
        try:
            db_times = repo.get_last_processed_message_times()
            if last_received is None:
                last_received = db_times.get("last_received_at")
            if last_processed is None:
                last_processed = db_times.get("last_processed_at")
        except Exception:
            logger.exception("/health: falha ao consultar processed_messages")

    def _aware(dt: Any) -> datetime | None:
        if dt is None:
            return None
        if isinstance(dt, datetime):
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return None

    last_received = _aware(last_received)
    last_processed = _aware(last_processed)

    time_since = None
    if last_received is not None:
        time_since = max(0.0, (now - last_received).total_seconds())

    # Horário comercial São Luís / São Paulo (UTC-3).
    business_hours = _is_business_hours_sl(now)

    status = "ok"
    if (
        last_received is not None
        and business_hours
        and time_since is not None
        and time_since > 600
    ):
        status = "degraded"

    workers_alive = sum(
        1 for p in _pending_by_phone.values() if p.task and not p.task.done()
    )

    def _iso(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt else None

    return {
        "status": status,
        "last_msg_received_at": _iso(last_received),
        "last_msg_processed_at": _iso(last_processed),
        "workers_alive": workers_alive,
        "time_since_last_msg_seconds": time_since,
        "business_hours_sl": business_hours,
    }


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
        "Inbound Evolution instance=%s remote_jid=%s phone=%s has_audio=%s text_len=%s",
        parsed.get("evolution_instance"),
        parsed.get("raw_remote_jid"),
        parsed.get("phone"),
        parsed.get("has_audio"),
        len(str(parsed.get("text") or "")),
    )
    phone = str(parsed.get("phone") or "").strip()
    remote_jid = str(parsed.get("raw_remote_jid") or "").strip()
    evolution_instance = str(parsed.get("evolution_instance") or "").strip()
    if not _is_allowed_instance(evolution_instance):
        logger.warning(
            "Inbound ignorado: instance=%s não corresponde à EVOLUTION_INSTANCE configurada.",
            evolution_instance,
        )
        return {"status": "ignored", "reason": "instance_not_allowed"}

    # I/1 — Idempotência: se esse provider_message_id já foi registrado, é
    # retry/duplicata do provedor. Skip e loga.
    provider_message_id = _extract_provider_message_id(payload)
    if provider_message_id:
        try:
            inserted = repo.register_processed_message(provider_message_id)
            if not inserted:
                logger.info(
                    "I/1: mensagem duplicada ignorada provider_id=%s phone=%s",
                    provider_message_id,
                    phone,
                )
                return {
                    "status": "ignored",
                    "reason": "duplicate_provider_message_id",
                    "provider_message_id": provider_message_id,
                }
        except Exception:
            logger.exception(
                "I/1: falha ao checar idempotência (processed_messages) — seguindo fluxo"
            )

    # I/4 — Healthcheck: atualiza timestamp em memória (para /health).
    global _last_inbound_received_at
    _last_inbound_received_at = datetime.now(timezone.utc)

    external_channel = str(parsed.get("external_channel") or "").strip() or _resolve_external_channel(evolution_instance)
    conversation_key = f"{evolution_instance}|{(remote_jid or phone).strip()}"
    lead_id = _resolve_lead_id(phone, evolution_instance=evolution_instance) if phone else None

    if _is_from_me(payload):
        text = str(parsed.get("text") or "").strip()
        if _is_outbound_echo(payload):
            return {"status": "ignored", "reason": "outbound_echo"}
        if conversation_key and lead_id:
            if not _can_activate_handoff(lead_id):
                return {"status": "ignored", "reason": "human_handoff_gate_blocked"}

            repo.set_handoff_state(lead_id, active=True, activated_by="human", reason="from_me_message")
            repo.set_bot_paused(
                lead_id,
                paused=True,
                by="human",
                reason="human_message",
            )
            confirmed = repo.confirm_latest_appointment_for_lead(lead_id)
            promoted = repo.promote_lead_stage_on_handoff(lead_id)
            # Persiste o texto COMPLETO do humano (não só stub) — novo role 'human_agent'
            # pra deixar claro que foi o atendente humano, não o bot nem o cliente.
            if text:
                repo.append_message(
                    lead_id=lead_id,
                    role="human_agent",
                    body=text[:4000],
                    metadata={
                        "event": "human_agent_message",
                        "channel": external_channel,
                    },
                )
                # Também injeta na sessão ADK (memória do runner) pra que quando
                # o bot for reativado, ele saiba o contexto do que o humano falou.
                # Falha aqui NÃO bloqueia o fluxo — é melhor o texto estar no banco
                # que nada.
                try:
                    from sdr_ilha_ar.channel import note_human_agent_message
                    await note_human_agent_message(
                        external_user_id=phone,
                        text=text,
                        external_channel=external_channel,
                    )
                except Exception:
                    logger.exception(
                        "Falha ao registrar nota humana na sessão ADK user=%s",
                        phone,
                    )
            # Mantém o stub event-log pra telemetria (não remove o existente).
            repo.append_message(
                lead_id=lead_id,
                role="tool",
                body="Bot pausado por mensagem humana (handoff ativo).",
                metadata={
                    "event": "bot_paused_by_human",
                    "appointment_confirmed": bool(confirmed),
                    "lead_promoted_to_scheduled": bool(promoted),
                    "text": text[:120],
                },
            )
            _activate_handoff_pause(conversation_key)
            return {"status": "ok", "reason": "bot_paused_by_human"}
        return {"status": "ignored", "reason": "outbound_without_recipient"}

    if _is_ignored_inbound_phone(phone):
        logger.info("Inbound ignorado para phone bloqueado=%s", phone)
        return {"status": "ignored", "reason": "ignored_inbound_phone"}

    if conversation_key and lead_id and (_is_handoff_paused(conversation_key) or _sync_handoff_cache(conversation_key, lead_id)):
        return {"status": "ignored", "reason": "human_handoff_active"}

    if remote_jid or phone:
        _enqueue_payload(
            payload=payload,
            remote_jid=remote_jid,
            phone=phone,
            external_channel=external_channel,
            evolution_instance=evolution_instance,
        )
        return {"status": "queued", "delivery": "queued", "debounce_seconds": DEBOUNCE_SECONDS}
    result = await handle_evolution_inbound(payload)
    # I/1: mesmo no caminho "skipped" (sem remote_jid/phone), marcamos
    # processed_at se conseguimos identificar provider_message_id.
    if provider_message_id:
        try:
            repo.mark_message_processed(provider_message_id)
        except Exception:
            logger.exception("I/1: falha ao marcar processed_at no caminho skipped")
    # I/4: guarda tempo de processamento também no caminho skipped.
    global _last_inbound_processed_at
    _last_inbound_processed_at = datetime.now(timezone.utc)
    return {**result, "delivery": "skipped"}


@app.get("/leads/{lead_id}/bot-status")
async def get_lead_bot_status(lead_id: str) -> dict[str, Any]:
    lid = uuid.UUID(lead_id)
    lead = repo.get_lead(lid)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    return {
        "lead_id": str(lead["id"]),
        "bot_paused": bool(lead.get("bot_paused")),
        "bot_paused_at": lead.get("bot_paused_at"),
        "bot_paused_by": lead.get("bot_paused_by"),
        "bot_paused_reason": lead.get("bot_paused_reason"),
        "bot_reactivated_at": lead.get("bot_reactivated_at"),
        "bot_reactivated_by": lead.get("bot_reactivated_by"),
        "equipe_responsavel": lead.get("equipe_responsavel"),
    }


# =============================================================================
# G — Informações de funil: estágio atual, duração, histórico.
# =============================================================================


@app.get("/leads/{lead_id}/stage-info")
async def get_lead_stage_info(lead_id: str) -> dict[str, Any]:
    """Retorna info de funil do lead: estágio atual, entered_at, duração (s) e histórico.

    Usado pelo frontend pra renderizar badge "Em '{stage}' há {duration_human}".
    """
    lid = uuid.UUID(lead_id)
    lead = repo.get_lead(lid)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    history = repo.get_stage_history(lid)
    duration = repo.get_current_stage_duration(lid)
    # entered_at do estágio corrente = última entrada em history que bate com stage atual e está aberta.
    entered_at = None
    current_stage = lead.get("stage")
    for row in reversed(history):
        if row.get("stage") == current_stage and row.get("exited_at") is None:
            entered_at = row.get("entered_at")
            break
    if entered_at is None and history:
        # fallback: último registro (caso linha esteja fechada por algum motivo)
        entered_at = history[-1].get("entered_at")
    return {
        "lead_id": str(lead["id"]),
        "current_stage": current_stage,
        "entered_at": entered_at,
        "duration_seconds": int(duration.total_seconds()) if duration is not None else None,
        "history": history,
    }


@app.post("/leads/{lead_id}/bot/reactivate")
async def reactivate_lead_bot(lead_id: str) -> dict[str, Any]:
    lid = uuid.UUID(lead_id)
    row = repo.set_bot_paused(lid, paused=False, by="frontend", reason="manual_reactivate")
    repo.append_message(
        lead_id=lid,
        role="tool",
        body="Bot reativado manualmente via frontend.",
        metadata={"event": "bot_reactivated_frontend"},
    )
    _clear_handoff_pause_for_lead(row)
    return {
        "status": "ok",
        "lead_id": str(row["id"]),
        "bot_paused": bool(row.get("bot_paused")),
        "bot_reactivated_at": row.get("bot_reactivated_at"),
        "bot_reactivated_by": row.get("bot_reactivated_by"),
    }


# =============================================================================
# F — Endpoints canônicos de pausa/retomada (usados pelo frontend Parte F).
# =============================================================================


class _PauseBotBody(BaseModel):
    reason: str | None = None
    by: str | None = None


@app.post("/leads/{lead_id}/pause-bot")
async def pause_lead_bot(
    lead_id: str,
    body: _PauseBotBody | None = None,
) -> dict[str, Any]:
    lid = uuid.UUID(lead_id)
    reason = (body.reason if body and body.reason else "manual_pause").strip() or "manual_pause"
    by = (body.by if body and body.by else "frontend").strip() or "frontend"
    row = repo.pause_bot_for_lead(lid, reason=reason, by=by)
    # Sincroniza cache de handoff local (conversation_key contém external_user_id).
    external_user_id = str(row.get("external_user_id") or "").strip()
    if external_user_id:
        for key in list(_handoff_cache.keys()):
            if external_user_id in key:
                _handoff_cache[key] = True
    repo.append_message(
        lead_id=lid,
        role="tool",
        body=f"Bot pausado manualmente via frontend (motivo={reason}).",
        metadata={"event": "bot_paused_frontend", "reason": reason, "by": by},
    )
    return {
        "status": "ok",
        "lead_id": str(row["id"]),
        "bot_paused": bool(row.get("bot_paused")),
        "bot_paused_at": row.get("bot_paused_at"),
        "bot_paused_by": row.get("bot_paused_by"),
        "bot_paused_reason": row.get("bot_paused_reason"),
    }


@app.post("/leads/{lead_id}/resume-bot")
async def resume_lead_bot(lead_id: str) -> dict[str, Any]:
    """Alias canônico do /bot/reactivate, pedido pela Parte F."""
    lid = uuid.UUID(lead_id)
    row = repo.resume_bot_for_lead(lid, by="frontend", reason="manual_resume")
    repo.append_message(
        lead_id=lid,
        role="tool",
        body="Bot retomado manualmente via frontend.",
        metadata={"event": "bot_resumed_frontend"},
    )
    _clear_handoff_pause_for_lead(row)
    return {
        "status": "ok",
        "lead_id": str(row["id"]),
        "bot_paused": bool(row.get("bot_paused")),
        "bot_reactivated_at": row.get("bot_reactivated_at"),
        "bot_reactivated_by": row.get("bot_reactivated_by"),
    }


# =============================================================================
# F5+A5 — endpoints de gestão de appointment (painel / frontend)
# =============================================================================


class _ConfirmBody(BaseModel):
    team_id: str | None = None


class _ReallocBody(BaseModel):
    # Data no formato DD/MM/AAAA para bater com o resto do sistema.
    new_date: str
    new_slot: str


class _CancelBody(BaseModel):
    reason: str | None = None


def _parse_br_date(value: str) -> date_cls:
    try:
        return datetime.strptime(str(value or "").strip(), "%d/%m/%Y").date()
    except Exception as exc:  # pragma: no cover - caminho de erro simples
        raise HTTPException(
            status_code=400,
            detail="Data inválida — use DD/MM/AAAA (ex: 05/05/2026).",
        ) from exc


def _format_scheduled_date(value: Any) -> str:
    """Devolve a data do appointment formatada DD/MM/AAAA para mensagens."""
    if value is None:
        return ""
    if isinstance(value, date_cls):
        return value.strftime("%d/%m/%Y")
    text = str(value).strip()
    # Postgres devolve 'YYYY-MM-DD' via _jsonify_value — convertemos aqui.
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().strftime("%d/%m/%Y")
    except Exception:
        return text


def _lead_first_name(appt: dict[str, Any]) -> str:
    raw = str(appt.get("display_name") or "").strip()
    if not raw:
        return ""
    return raw.split()[0]


def _notify_lead_whatsapp(appt: dict[str, Any], text: str) -> None:
    """Envia mensagem de texto ao lead reusando o transporte Evolution.

    Tolera falhas (ambiente sem credenciais, lead sem telefone) para não
    derrubar o endpoint REST — o status do appointment já foi persistido.
    """
    phone = str(appt.get("phone") or "").strip()
    remote_jid = str(appt.get("external_user_id") or "").strip()
    if not (phone or remote_jid):
        logger.warning(
            "Appointment %s sem telefone/jid; pulei notificação ao lead.",
            appt.get("id"),
        )
        return
    try:
        _send_whatsapp_reply(remote_jid=remote_jid, phone=phone, text=text)
    except Exception:
        logger.exception(
            "Falha ao notificar lead (appointment=%s). Status já foi atualizado.",
            appt.get("id"),
        )
    # Registra no histórico do lead independentemente do envio real.
    lead_id = appt.get("lead_id")
    if lead_id:
        try:
            repo.append_message(
                lead_id=uuid.UUID(str(lead_id)),
                role="assistant_outbound_stub",
                body=text[:4000],
                metadata={"channel": "whatsapp", "source": "appointment_endpoint"},
            )
        except Exception:
            logger.exception("Falha ao registrar outbound no histórico do lead.")


def _build_confirm_message(appt: dict[str, Any], team_id: str | None) -> str:
    slot_label = repo.SLOT_LABELS.get(str(appt.get("slot") or ""), str(appt.get("slot") or ""))
    data = _format_scheduled_date(appt.get("scheduled_date"))
    # Não expomos UUID interno do team_id pro cliente — mensagem uniforme,
    # humana e clara. Se um dia houver tabela teams com nome, fazer lookup aqui.
    _ = team_id
    return (
        f"✅ Seu agendamento foi confirmado pra {data} das {slot_label}! "
        "Nossa equipe técnica te avisa no dia quando sair pra aí."
    )


def _build_realloc_message(appt: dict[str, Any]) -> str:
    nome = _lead_first_name(appt)
    saudacao = f"Oi {nome}!" if nome else "Oi!"
    slot_label = repo.SLOT_LABELS.get(str(appt.get("slot") or ""), str(appt.get("slot") or ""))
    data = _format_scheduled_date(appt.get("scheduled_date"))
    return (
        f"{saudacao} Precisamos ajustar seu agendamento pra {data} das {slot_label}. "
        "Tudo bem? (responde SIM ou NÃO)"
    )


def _build_cancel_message(appt: dict[str, Any], reason: str | None) -> str:
    nome = _lead_first_name(appt)
    saudacao = f"Oi {nome}," if nome else "Oi,"
    motivo = ""
    if reason and str(reason).strip():
        motivo = f" ({str(reason).strip()})"
    return (
        f"{saudacao} precisei cancelar seu agendamento{motivo}. "
        "Se quiser remarcar, é só me chamar aqui."
    )


def _load_appointment_or_404(appointment_id: str) -> tuple[uuid.UUID, dict[str, Any]]:
    try:
        aid = uuid.UUID(appointment_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="appointment_id inválido") from exc
    existing = repo.get_appointment(aid)
    if not existing:
        raise HTTPException(status_code=404, detail="Appointment não encontrado")
    return aid, existing


@app.post("/appointments/{appointment_id}/confirm")
async def confirm_appointment(
    appointment_id: str,
    body: _ConfirmBody | None = None,
) -> dict[str, Any]:
    body = body or _ConfirmBody()
    aid, _existing = _load_appointment_or_404(appointment_id)
    team_id = (body.team_id or "").strip() or None
    try:
        updated = repo.update_appointment_status(
            aid,
            status="confirmed",
            team_id=team_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Carrega versão enriquecida (com dados do lead) para montar a mensagem.
    enriched = repo.get_appointment(aid) or {**_existing, **updated}
    message = _build_confirm_message(enriched, team_id)
    _notify_lead_whatsapp(enriched, message)
    return {
        "status": "ok",
        "appointment": updated,
        "message_sent": message,
    }


@app.post("/appointments/{appointment_id}/realloc")
async def realloc_appointment(
    appointment_id: str,
    body: _ReallocBody,
) -> dict[str, Any]:
    aid, _existing = _load_appointment_or_404(appointment_id)
    new_date = _parse_br_date(body.new_date)
    new_slot = str(body.new_slot or "").strip()
    if new_slot not in repo.SLOT_LABELS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Slot inválido: {new_slot}. "
                f"Use um de {list(repo.SLOT_LABELS)}."
            ),
        )
    try:
        updated = repo.update_appointment_status(
            aid,
            status="realloc",
            scheduled_date=new_date,
            slot=new_slot,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    enriched = repo.get_appointment(aid) or {**_existing, **updated}
    message = _build_realloc_message(enriched)
    _notify_lead_whatsapp(enriched, message)
    return {
        "status": "ok",
        "appointment": updated,
        "message_sent": message,
    }


@app.post("/appointments/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: str,
    body: _CancelBody | None = None,
) -> dict[str, Any]:
    body = body or _CancelBody()
    aid, _existing = _load_appointment_or_404(appointment_id)
    try:
        updated = repo.update_appointment_status(aid, status="cancelled")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    enriched = repo.get_appointment(aid) or {**_existing, **updated}
    message = _build_cancel_message(enriched, body.reason)
    _notify_lead_whatsapp(enriched, message)
    return {
        "status": "ok",
        "appointment": updated,
        "message_sent": message,
    }


# -----------------------------------------------------------------------------
# H — Recall 6m pós-conclusão
# -----------------------------------------------------------------------------

RECALL_6M_DAYS = 180
RECALL_6M_JOB_TYPE = "followup_recall_6m"


def schedule_recall_6m_for_lead(lead_id: uuid.UUID | str) -> str | None:
    """Enfileira um job `followup_recall_6m` para daqui 180 dias.

    Idempotente: a chave `recall6m_{lead_id}` evita duplicar se o appointment
    for marcado como completed mais de uma vez (ex: duplo clique no painel).
    Retorna o job_id (string) ou None se já existia.
    """
    lid = lead_id if isinstance(lead_id, uuid.UUID) else uuid.UUID(str(lead_id))
    run_at = datetime.now(timezone.utc) + timedelta(days=RECALL_6M_DAYS)
    idem = f"recall6m_{lid}"
    jid = repo.enqueue_job(
        lid,
        RECALL_6M_JOB_TYPE,
        run_at,
        {"reason": "appointment_completed", "offer_amount_brl": 280.0},
        idem,
    )
    if jid is not None:
        try:
            repo.append_message(
                lid,
                "tool",
                f"{RECALL_6M_JOB_TYPE} agendado para {run_at.isoformat()}",
            )
        except Exception:  # pragma: no cover — log-only side effect
            logger.exception("append_message falhou pro recall 6m lead=%s", lid)
    return str(jid) if jid else None


@app.post("/appointments/manual")
async def create_manual_appointment(request: Request) -> dict[str, Any]:
    """Cria lead legado + appointment manual num único passo.

    Usado pelo painel humano (front-end) pra cadastrar clientes que já tinham
    agendamento antes do sistema existir. Efeitos:
      - INSERT em `leads` com external_channel='manual', bot_paused=true
        (o agente IA NUNCA vai falar com esse lead — é só registro legado)
      - INSERT em `appointments` com status='pending_team_assignment' e campos
        scheduled_date/slot (ou custom_time pra horário fora dos 4 slots fixos)

    Request body:
        {
          "lead": {
            "display_name": "João Silva",       # obrigatório
            "phone": "+5598999999999",           # opcional
            "address": "Rua X, 123",             # opcional
            "service_type": "instalacao",        # opcional
            "btus": 12000,                       # opcional
            "floor_level": 3,                    # opcional
            "quoted_amount": 450.00,             # opcional
            "notes": "Cliente legado, ligou..."  # opcional (vai pra quote_notes)
          },
          "appointment": {
            "scheduled_date": "2026-05-10",      # obrigatório (YYYY-MM-DD)
            "slot": "morning_early",             # opcional (1 dos 4 slots fixos)
            "custom_time": "11:30",              # opcional (hora livre legada)
            "team_id": "abc-uuid",               # opcional (humano atribui depois)
            "notes": "Visita remarcada 2x"       # opcional
          }
        }

    Returns 201 com o appointment criado + lead_id.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON inválido.") from exc

    lead_data = body.get("lead") or {}
    appt_data = body.get("appointment") or {}

    # --- Validações mínimas ---
    display_name = (lead_data.get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(
            status_code=400,
            detail="lead.display_name é obrigatório.",
        )

    scheduled_date_raw = appt_data.get("scheduled_date")
    if not scheduled_date_raw:
        raise HTTPException(
            status_code=400,
            detail="appointment.scheduled_date é obrigatório (formato YYYY-MM-DD).",
        )
    try:
        scheduled_date = date_cls.fromisoformat(str(scheduled_date_raw))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="appointment.scheduled_date inválido. Use YYYY-MM-DD.",
        ) from exc

    slot = appt_data.get("slot") or None
    if slot and slot not in repo.SLOT_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"appointment.slot inválido. Valores aceitos: {list(repo.SLOT_LABELS.keys())}",
        )

    custom_time = (appt_data.get("custom_time") or "").strip() or None
    if not slot and not custom_time:
        raise HTTPException(
            status_code=400,
            detail="Informe appointment.slot OU appointment.custom_time.",
        )

    # --- Normaliza tipos opcionais ---
    def _to_int(v: Any) -> int | None:
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _to_float(v: Any) -> float | None:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # --- Cria lead ---
    try:
        lead_id = repo.create_lead_manual(
            display_name=display_name,
            phone=(lead_data.get("phone") or None),
            address=(lead_data.get("address") or None),
            service_type=(lead_data.get("service_type") or None),
            btus=_to_int(lead_data.get("btus")),
            floor_level=_to_int(lead_data.get("floor_level")),
            quoted_amount=_to_float(lead_data.get("quoted_amount")),
            notes=(lead_data.get("notes") or None),
        )
    except Exception as exc:
        logger.exception("create_lead_manual falhou body=%s", lead_data)
        raise HTTPException(status_code=500, detail=f"Falha ao criar lead: {exc}") from exc

    # --- Cria appointment ---
    try:
        appointment = repo.create_appointment_manual(
            lead_id=lead_id,
            scheduled_date=scheduled_date,
            slot=slot,
            custom_time=custom_time,
            team_id=(appt_data.get("team_id") or None),
            notes=(appt_data.get("notes") or None),
        )
    except Exception as exc:
        logger.exception("create_appointment_manual falhou lead=%s body=%s", lead_id, appt_data)
        raise HTTPException(status_code=500, detail=f"Falha ao criar appointment: {exc}") from exc

    # Normaliza tipos pra JSON (date/datetime → str)
    def _jsonable(v: Any) -> Any:
        if hasattr(v, "isoformat"):
            return v.isoformat()
        if isinstance(v, uuid.UUID):
            return str(v)
        return v

    appointment_clean = {k: _jsonable(v) for k, v in appointment.items()}

    logger.info(
        "create_manual_appointment ok lead=%s appointment=%s date=%s slot=%s custom=%s",
        lead_id,
        appointment_clean.get("id"),
        scheduled_date,
        slot,
        custom_time,
    )

    return JSONResponse(
        status_code=201,
        content={
            "status": "ok",
            "lead_id": str(lead_id),
            "appointment": appointment_clean,
        },
    )


@app.post("/appointments/{appointment_id}/complete")
async def complete_appointment(appointment_id: str) -> dict[str, Any]:
    """Marca appointment como concluído e agenda recall de 6 meses.

    Chamado pelo painel interno quando o técnico confirma que o serviço foi
    executado. Efeitos:
      - status=completed
      - job `followup_recall_6m` com `run_at = now + 180 dias` (oferta limpeza R$ 280)
      - job `notify_internal` IMEDIATO com tag [CONCLUÍDO] avisando o grupo/admin
        (BUG FIX: antes só atualizava o DB sem notificar — ninguém sabia que
        a visita foi finalizada).
    """
    aid, _existing = _load_appointment_or_404(appointment_id)
    try:
        updated = repo.update_appointment_status(aid, status="completed")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lead_id_raw = updated.get("lead_id") or _existing.get("lead_id")
    recall_job_id: str | None = None
    notify_job_id: str | None = None
    if lead_id_raw:
        lead_uuid = uuid.UUID(str(lead_id_raw))
        # Recall 6 meses (já existia)
        try:
            recall_job_id = schedule_recall_6m_for_lead(str(lead_id_raw))
        except Exception:
            logger.exception(
                "Falha ao agendar recall 6m para lead=%s (appointment=%s)",
                lead_id_raw,
                aid,
            )
        # BUG FIX: notificar o grupo/admin que o serviço foi concluído
        try:
            window_label = updated.get("window_label") or _existing.get("window_label") or "—"
            notes_val = updated.get("notes") or _existing.get("notes") or ""
            scheduled_date = updated.get("scheduled_date") or _existing.get("scheduled_date")
            slot = updated.get("slot") or _existing.get("slot")
            date_slot_line = ""
            if scheduled_date:
                date_slot_line = f"Data/slot: {scheduled_date}"
                if slot:
                    date_slot_line += f" · {slot}"
            notify_payload: dict[str, Any] = {
                "tag": "[CONCLUÍDO]",
                "title": "VISITA TÉCNICA CONCLUÍDA",
                "window_label": window_label,
            }
            if notes_val:
                notify_payload["notes"] = notes_val
            if date_slot_line:
                notify_payload["reason"] = date_slot_line
            ret = repo.enqueue_job(
                lead_id=lead_uuid,
                job_type="notify_internal",
                run_at=datetime.now(timezone.utc),
                payload=notify_payload,
                idempotency_key=f"notify_completed:{aid}",
            )
            notify_job_id = str(ret) if ret else None
        except Exception:
            logger.exception(
                "Falha ao enfileirar notify_internal de conclusão (appointment=%s)",
                aid,
            )
    return {
        "status": "ok",
        "appointment": updated,
        "recall_6m_job_id": recall_job_id,
        "notify_job_id": notify_job_id,
    }


