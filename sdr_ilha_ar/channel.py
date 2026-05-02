# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""
Adaptador de canal: inbound (mensagem do cliente) -> ADK Runner -> texto de resposta.

Use com WhatsApp Business API ou outro webhook HTTP. Produção: substituir
`InMemoryRunner` por `Runner` + `DatabaseSessionService` (ou equivalente) para
histórico persistente entre reinícios do processo.
"""

from __future__ import annotations

import base64
import logging
import os
import uuid
import re
from urllib import request
from typing import Any

from google import genai
from google.adk.runners import InMemoryRunner
from google.genai import types

from sdr_ilha_ar.config import settings
from sdr_ilha_ar.llm_app import root_agent
from sdr_ilha_ar import repository as lead_repo

logger = logging.getLogger(__name__)

_runner: InMemoryRunner | None = None

FALLBACK_REPLY = (
    "Recebi sua mensagem certinho. Se puder, me manda novamente para eu confirmar "
    "seu atendimento agora."
)
TRANSCRIBE_PROMPT = (
    "Transcreva este áudio em português do Brasil. "
    "Retorne apenas o texto transcrito, sem comentários."
)


def _runner_singleton() -> InMemoryRunner:
    global _runner
    if _runner is None:
        _runner = InMemoryRunner(agent=root_agent, app_name=settings.app_name)
    return _runner


def _extract_evolution_instance(*, body: dict[str, Any], data: dict[str, Any]) -> str:
    def _from_obj(obj: Any) -> str:
        if isinstance(obj, str) and obj.strip():
            return obj.strip()
        if isinstance(obj, dict):
            inner = obj.get("instanceName") or obj.get("name") or obj.get("instance")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        return ""

    for obj in (body, data):
        if not isinstance(obj, dict):
            continue
        for key in ("instance", "instanceName", "instance_name"):
            hit = _from_obj(obj.get(key))
            if hit:
                return hit

    return str(os.getenv("EVOLUTION_INSTANCE") or "").strip()


def _resolve_external_channel(instance: str) -> str:
    # Operação single-instance: sempre usamos o mesmo canal lógico.
    # Isso evita segmentação por "whatsapp:<instancia>" no CRM.
    return "whatsapp"


async def handle_inbound_text(
    *,
    external_user_id: str,
    text: str,
    external_channel: str | None = None,
) -> str:
    """
    Uma rodada do SDR. `external_user_id` costuma ser o wa_id ou id estável do lead.

    `session_id` = `external_user_id` para manter o fio da conversa no InMemoryRunner.
    """
    runner = _runner_singleton()
    app_name = runner.app_name
    channel = external_channel or settings.default_external_channel
    session_id = external_user_id

    existing = await runner.session_service.get_session(
        app_name=app_name,
        user_id=external_user_id,
        session_id=session_id,
    )
    if existing is None:
        await runner.session_service.create_session(
            app_name=app_name,
            user_id=external_user_id,
            session_id=session_id,
        )
        created = await runner.session_service.get_session(
            app_name=app_name,
            user_id=external_user_id,
            session_id=session_id,
        )
        if created is not None:
            created.state["external_channel"] = channel
    else:
        # Sempre atualiza: sessões antigas podem ter ficado só em "whatsapp" e quebram roteamento A/B.
        existing.state["external_channel"] = channel

    content = types.Content(role="user", parts=[types.Part(text=text)])
    final_text = ""
    try:
        async for event in runner.run_async(
            user_id=external_user_id,
            session_id=session_id,
            new_message=content,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        final_text = part.text
    except Exception:
        logger.exception("Falha ao processar rodada do agente para user=%s", external_user_id)
        return (
            "Obrigada pelas informações! Nosso sistema teve uma instabilidade rápida, "
            "mas sua solicitação já foi recebida e vamos te responder em seguida."
        )
    if not final_text.strip():
        logger.warning("Resposta final vazia para user=%s", external_user_id)
    return final_text.strip() or FALLBACK_REPLY


def handle_inbound_text_sync(
    *,
    external_user_id: str,
    text: str,
    external_channel: str | None = None,
) -> str:
    """Versão síncrona para scripts."""
    import asyncio

    return asyncio.run(
        handle_inbound_text(
            external_user_id=external_user_id,
            text=text,
            external_channel=external_channel,
        )
    )


def transcribe_audio_bytes(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcreve áudio usando Gemini para alimentar o fluxo textual."""
    client = genai.Client()
    response = client.models.generate_content(
        model=settings.audio_transcribe_model,
        contents=[
            TRANSCRIBE_PROMPT,
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ],
    )
    return (response.text or "").strip()


def _decode_b64_audio(raw_b64: str) -> bytes:
    payload = raw_b64.strip()
    if "," in payload and payload.lower().startswith("data:"):
        payload = payload.split(",", 1)[1]
    return base64.b64decode(payload)


def fetch_audio_bytes_from_url(url: str) -> bytes:
    with request.urlopen(url, timeout=settings.audio_fetch_timeout_seconds) as resp:
        return resp.read()


def _normalize_phone_from_remote_jid(remote_jid: str) -> str:
    base = remote_jid.split("@", 1)[0].strip()
    digits = re.sub(r"\D+", "", base)
    if digits.startswith("55") and len(digits) > 10:
        digits = digits[2:]
    return digits


def _extract_prename(data: dict[str, Any], body: dict[str, Any]) -> str:
    candidate = (
        data.get("pushName")
        or data.get("senderName")
        or body.get("pushName")
        or body.get("senderName")
        or ""
    )
    return str(candidate).strip()


def _looks_like_address(text: str) -> bool:
    t = (text or "").lower()
    if len(t) < 12:
        return False
    tokens = ("rua", "avenida", "av ", "quadra", "bairro", "conjunto", "casa", "numero", "nº")
    has_token = any(tok in t for tok in tokens)
    has_digit = any(ch.isdigit() for ch in t)
    return has_token and has_digit


def _maybe_autosave_address(*, phone: str, text: str, external_channel: str) -> None:
    """
    FIX-MAPS: NÃO autosalvamos mais endereço por texto livre como source of truth.
    O endereço-texto só é gravado se ainda não houver NENHUM dado de endereço
    (nem lat/lng nem address), e fica como fallback. A source of truth agora é
    o pin de localização (lat/lng) — ver `_persist_inbound_location`.
    """
    if not _looks_like_address(text):
        return
    try:
        lead_id = lead_repo.ensure_lead(external_channel, phone, touch_inbound=True)
        lead = lead_repo.get_lead(lead_id) or {}
        has_coords = lead.get("latitude") is not None and lead.get("longitude") is not None
        has_address = bool(str(lead.get("address") or "").strip())
        if has_coords or has_address:
            return
        # DESIGN DECISION: ainda salvamos texto como fallback, mas marcamos no log
        # para deixar claro que é fallback. O prompt pede pin depois.
        lead_repo.save_lead_field(lead_id, "address", text.strip())
        lead_repo.append_message(
            lead_id,
            "tool",
            "autosave address fallback (texto livre) — pin de localização ainda não enviado",
        )
    except Exception:
        logger.exception("Falha ao autosalvar endereço para phone=%s", phone)


def _persist_inbound_location(
    *,
    phone: str,
    external_channel: str,
    location: dict[str, Any],
) -> None:
    """
    FIX-MAPS: quando o cliente manda um pin (message.type=location), gravamos
    lat/lng como source of truth da localização. Se vier `address` no payload
    da própria mensagem (Google Places às vezes preenche), só grava se não
    houver address ainda salvo.
    """
    if not location:
        return
    try:
        lat = location.get("latitude")
        lng = location.get("longitude")
        if lat is None or lng is None:
            return
        lead_id = lead_repo.ensure_lead(external_channel, phone, touch_inbound=True)
        lead_repo.save_lead_location(lead_id, lat, lng)
        lead = lead_repo.get_lead(lead_id) or {}
        # Se vier nome/endereço no pin e não houver address salvo, usa como fallback humano.
        loc_address = (location.get("name") or "") + (
            (", " + location.get("address")) if location.get("address") else ""
        )
        loc_address = loc_address.strip(", ")
        if loc_address and not str(lead.get("address") or "").strip():
            lead_repo.save_lead_field(lead_id, "address", loc_address)
        lead_repo.append_message(
            lead_id,
            "tool",
            f"FIX-MAPS: location pin salvo lat={lat} lng={lng} name={location.get('name')!r}",
        )
    except Exception:
        logger.exception("Falha ao persistir location inbound para phone=%s", phone)


def _extract_location_payload(message: dict[str, Any], data: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """
    FIX-MAPS: extrai latitude/longitude de mensagens WhatsApp do tipo location.
    Suporta Evolution API (locationMessage) e variantes.
    Retorna dict com {"latitude": float, "longitude": float, "name": str, "address": str}
    ou dict vazio se não for location.
    """
    def _from_obj(obj: Any) -> dict[str, Any]:
        if not isinstance(obj, dict):
            return {}
        lat = obj.get("degreesLatitude") or obj.get("latitude") or obj.get("lat")
        lng = obj.get("degreesLongitude") or obj.get("longitude") or obj.get("lng") or obj.get("lon")
        try:
            if lat is not None and lng is not None:
                return {
                    "latitude": float(lat),
                    "longitude": float(lng),
                    "name": str(obj.get("name") or obj.get("locationName") or "").strip(),
                    "address": str(obj.get("address") or "").strip(),
                }
        except (TypeError, ValueError):
            return {}
        return {}

    # Estruturas comuns Evolution / Baileys
    for key in ("locationMessage", "liveLocationMessage", "location"):
        hit = _from_obj(message.get(key))
        if hit:
            return hit
        # Dentro de ephemeralMessage ou viewOnceMessage
        for wrapper_key in ("ephemeralMessage", "viewOnceMessage"):
            wrapper = message.get(wrapper_key)
            if isinstance(wrapper, dict):
                inner = wrapper.get("message") if isinstance(wrapper.get("message"), dict) else wrapper
                hit_inner = _from_obj(inner.get(key) if isinstance(inner, dict) else None)
                if hit_inner:
                    return hit_inner

    # Top-level em data/body (alguns provedores)
    for obj in (data, body):
        hit = _from_obj(obj.get("location") if isinstance(obj, dict) else None)
        if hit:
            return hit
        # Campos soltos data.latitude + data.longitude
        if isinstance(obj, dict):
            hit_flat = _from_obj(obj)
            if hit_flat:
                return hit_flat
    return {}


def _has_location_by_shape(*, data: dict[str, Any], body: dict[str, Any]) -> bool:
    msg_type = str(data.get("messageType") or body.get("messageType") or "").strip().lower()
    if "location" in msg_type:
        return True
    return False


def _extract_audio_payload(message: dict[str, Any]) -> dict[str, Any]:
    if isinstance(message.get("audioMessage"), dict):
        return message["audioMessage"]
    if isinstance(message.get("pttMessage"), dict):
        return message["pttMessage"]
    ephemeral = message.get("ephemeralMessage")
    if isinstance(ephemeral, dict):
        inner = ephemeral.get("message")
        if isinstance(inner, dict):
            if isinstance(inner.get("audioMessage"), dict):
                return inner["audioMessage"]
            if isinstance(inner.get("pttMessage"), dict):
                return inner["pttMessage"]
    view_once = message.get("viewOnceMessage")
    if isinstance(view_once, dict):
        inner = view_once.get("message")
        if isinstance(inner, dict) and isinstance(inner.get("audioMessage"), dict):
            return inner["audioMessage"]
    return {}


def _coalesce_audio_media_url(*, data: dict[str, Any], body: dict[str, Any], audio_payload: dict[str, Any]) -> str:
    candidates = (
        audio_payload.get("url"),
        audio_payload.get("mediaUrl"),
        data.get("mediaUrl"),
        data.get("mediaURL"),
        data.get("url"),
        body.get("mediaUrl"),
        body.get("mediaURL"),
        body.get("url"),
    )
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _has_audio_by_shape(*, body: dict[str, Any], data: dict[str, Any], message: dict[str, Any], audio_payload: dict[str, Any]) -> bool:
    if audio_payload:
        return True
    msg_type = str(data.get("messageType") or body.get("messageType") or "").strip().lower()
    if any(token in msg_type for token in ("audio", "ptt", "voice")):
        return True
    media_type = str(data.get("mediaType") or body.get("mediaType") or "").strip().lower()
    if media_type in {"audio", "ptt", "voice"}:
        return True
    mime_candidates = (
        audio_payload.get("mimetype"),
        data.get("mimetype"),
        data.get("mimeType"),
        body.get("mimetype"),
        body.get("mimeType"),
    )
    for raw in mime_candidates:
        if isinstance(raw, str) and raw.strip().lower().startswith("audio/"):
            return True
    if isinstance(message.get("documentMessage"), dict):
        doc = message["documentMessage"]
        mime = str(doc.get("mimetype") or doc.get("mimeType") or "").strip().lower()
        if mime.startswith("audio/"):
            return True
    return False


def _seed_lead_identity(*, phone: str, pre_name: str, external_channel: str) -> None:
    try:
        lead_repo.reconcile_whatsapp_instance_channel(phone, external_channel)
        lead_id = lead_repo.ensure_lead(external_channel, phone, touch_inbound=True)
        lead = lead_repo.get_lead(lead_id) or {}
        if phone and (not lead.get("phone") or str(lead.get("phone")).strip() != phone):
            lead_repo.save_lead_field(lead_id, "phone", phone)
        if pre_name and not str(lead.get("display_name") or "").strip():
            lead_repo.save_lead_field(lead_id, "display_name", pre_name)
            
        # Agenda um Oi Sumido (Abandono) caso o lead empaque em "new"
        from datetime import datetime, timedelta, timezone
        run_at = datetime.now(timezone.utc) + timedelta(hours=24)
        lead_repo.enqueue_job(
            lead_id,
            "abandonment_check",
            run_at,
            {},
            f"abandon_check_{lead_id}",
        )
    except Exception:
        logger.exception("Falha ao semear identidade do lead para phone=%s", phone)


def parse_evolution_inbound(body: dict[str, Any]) -> dict[str, Any]:
    """
    Extrai metadados comuns de webhook inbound da Evolution API.

    Compatível com estruturas comuns: `data.key.remoteJid`, `data.message.*`.
    """
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    key = data.get("key") if isinstance(data, dict) else {}
    message = data.get("message") if isinstance(data, dict) else {}
    if not isinstance(message, dict):
        message = {}

    remote_jid = str(key.get("remoteJid") or data.get("remoteJid") or "").strip()
    phone = _normalize_phone_from_remote_jid(remote_jid) if remote_jid else ""
    pre_name = _extract_prename(data, body)
    evolution_instance = _extract_evolution_instance(body=body, data=data)
    channel = _resolve_external_channel(evolution_instance)

    text = ""
    msg_type = str(data.get("messageType") or "")
    if isinstance(message.get("conversation"), str):
        text = message["conversation"].strip()
    elif isinstance(message.get("extendedTextMessage"), dict):
        text = str(message["extendedTextMessage"].get("text") or "").strip()
    elif msg_type == "conversation":
        text = str(data.get("text") or "").strip()

    audio_payload = _extract_audio_payload(message)
    audio_url = _coalesce_audio_media_url(data=data, body=body, audio_payload=audio_payload)
    audio_b64 = str(
        audio_payload.get("base64")
        or audio_payload.get("audioBase64")
        or audio_payload.get("pttBase64")
        or message.get("base64")
        or message.get("audioBase64")
        or data.get("base64")
        or data.get("audioBase64")
        or data.get("base64Audio")
        or body.get("base64")
        or body.get("audioBase64")
        or body.get("base64Audio")
        or ""
    ).strip()
    mime_type = str(audio_payload.get("mimetype") or data.get("mimetype") or "audio/ogg").strip()
    has_audio = bool(audio_url or audio_b64) or _has_audio_by_shape(
        body=body,
        data=data,
        message=message,
        audio_payload=audio_payload,
    )
    if has_audio:
        # Para mensagens com áudio, forçamos transcrição para evitar tratar
        # placeholders do provedor como texto real do cliente.
        text = ""

    # FIX-MAPS: detectar pin de localização do WhatsApp.
    location = _extract_location_payload(message, data, body)
    has_location = bool(location) or _has_location_by_shape(data=data, body=body)

    return {
        "external_user_id": phone,
        "raw_remote_jid": remote_jid,
        "phone": phone,
        "pre_name": pre_name,
        "external_channel": channel,
        "evolution_instance": evolution_instance,
        "text": text,
        "audio_url": audio_url,
        "audio_b64": audio_b64,
        "mime_type": mime_type,
        "has_audio": has_audio,
        "location": location,
        "has_location": has_location,
    }


async def handle_evolution_inbound(body: dict[str, Any]) -> dict[str, Any]:
    """
    Processa webhook inbound da Evolution API.
    Retorna envelope pronto para camada HTTP responder.
    """
    parsed = parse_evolution_inbound(body)
    phone = str(parsed.get("phone") or "").strip()
    if not phone:
        logger.warning("Webhook Evolution sem remoteJid válido: %s", parsed.get("raw_remote_jid"))
        return build_http_response_envelope(
            "Não consegui identificar seu número para continuar o atendimento. "
            "Pode enviar uma nova mensagem?"
        )
    _seed_lead_identity(
        phone=phone,
        pre_name=str(parsed.get("pre_name") or "").strip(),
        external_channel=str(parsed["external_channel"]),
    )
    # FIX-MAPS: se veio pin de localização, grava lat/lng ANTES do agente rodar
    # e substitui texto por um marcador estruturado para o LLM reconhecer.
    location = parsed.get("location") or {}
    if location and location.get("latitude") is not None and location.get("longitude") is not None:
        _persist_inbound_location(
            phone=phone,
            external_channel=str(parsed["external_channel"]),
            location=location,
        )
    _maybe_autosave_address(
        phone=phone,
        text=str(parsed.get("text") or ""),
        external_channel=str(parsed["external_channel"]),
    )

    # F — Guarda de segurança: ANTES de invocar o LLM, checa se o bot foi
    # pausado para esse lead (manual ou auto por humano respondendo).
    # O webhook_api já filtra com cache, mas entradas vindas por rota
    # alternativa (ex: testes, outros provedores) precisam desse cinto.
    try:
        lead_id_guard = lead_repo.ensure_lead(
            str(parsed["external_channel"]), phone, touch_inbound=False
        )
        if lead_id_guard and lead_repo.is_bot_paused(lead_id_guard):
            logger.info(
                "F/guard: bot pausado para lead=%s — pulando LLM (phone=%s)",
                lead_id_guard,
                phone,
            )
            envelope = build_http_response_envelope("")
            envelope["bot_paused"] = True
            envelope["skipped_reason"] = "bot_paused"
            return envelope
    except Exception:
        logger.exception("F/guard: falha ao verificar bot_paused — seguindo fluxo")

    text = parsed["text"]
    # Se veio location e não veio texto, injeta texto sintético para o agente
    # continuar o fluxo naturalmente.
    if location and not text:
        text = (
            f"[LOCATION_RECEIVED lat={location['latitude']} lng={location['longitude']}"
            + (f" name={location.get('name')!r}" if location.get("name") else "")
            + "] Cliente enviou o pin de localização pelo WhatsApp."
        )
    if not text:
        try:
            if parsed["audio_b64"]:
                audio_bytes = _decode_b64_audio(parsed["audio_b64"])
            elif parsed["audio_url"]:
                audio_bytes = fetch_audio_bytes_from_url(parsed["audio_url"])
            else:
                audio_bytes = b""
            if audio_bytes:
                text = transcribe_audio_bytes(audio_bytes, parsed["mime_type"])
        except Exception:
            logger.exception("Falha ao transcrever áudio inbound da Evolution")
            return build_http_response_envelope(
                "Recebi seu áudio, mas não consegui transcrever agora. "
                "Pode reenviar o áudio ou escrever em texto?"
            )

    if not text:
        return build_http_response_envelope(
            "Recebi sua mensagem. Pode me enviar em texto para eu te atender melhor?"
        )

    reply = await handle_inbound_text(
        external_user_id=phone,
        text=text,
        external_channel=parsed["external_channel"],
    )
    envelope = build_http_response_envelope(reply)
    envelope["must_reply_audio"] = parsed.get("has_audio", False)
    return envelope


def parse_meta_whatsapp_example(body: dict[str, Any]) -> tuple[str, str] | None:
    """
    Exemplo de extração para webhooks Meta Cloud API (ajuste ao payload real).

    Retorna (wa_id, texto) ou None.
    """
    try:
        entry = body["entry"][0]["changes"][0]["value"]
        msg = entry["messages"][0]
        if msg.get("type") != "text":
            return None
        wa_id = msg["from"]
        text = msg["text"]["body"]
        return wa_id, text
    except (KeyError, IndexError, TypeError):
        return None


def build_http_response_envelope(response_text: str) -> dict[str, Any]:
    """Envelope genérico para o controlador HTTP devolver ao provedor."""
    return {"reply": response_text, "correlation_id": str(uuid.uuid4())}
