# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Envio de notificações internas (WhatsApp Admin via Evolution)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any
import requests

from sdr_ilha_ar.config import settings

logger = logging.getLogger(__name__)


def _instance_from_external_channel(external_channel: str | None) -> str:
    # Single-instance: não roteia por external_channel, usa instância global do ambiente.
    _ = external_channel
    return ""


def _resolve_group_for_instance(instance: str) -> str:
    _ = instance
    return str(settings.tech_group_jid or "").strip()


def _evolution_credentials(instance_hint: str = "") -> tuple[str, str, str] | None:
    _ = instance_hint
    import os
    base_url = (os.getenv("EVOLUTION_BASE_URL") or "").rstrip("/")
    api_key = (os.getenv("EVOLUTION_API_KEY") or "").strip()
    instance = (os.getenv("EVOLUTION_INSTANCE") or "").strip()
    if not (base_url and api_key and instance):
        return None
    return base_url, api_key, instance


def _send_text_to_destination(number_or_jid: str, text: str, *, instance_hint: str = "") -> dict[str, Any]:
    creds = _evolution_credentials(instance_hint=instance_hint)
    if not creds:
        logger.warning("Credenciais da Evolution indisponíveis para notificação interna.")
        return {"status": "skipped", "reason": "evolution_not_configured"}
    base_url, api_key, instance = creds

    url = f"{base_url}/message/sendText/{instance}"
    body = json.dumps(
        {"number": number_or_jid, "text": text}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"apikey": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return {"status": "ok", "response": raw[:500]}
    except urllib.error.URLError as e:
        logger.exception("Falha ao enviar mensagem interna WhatsApp")
        return {"status": "error", "message": str(e)}


def send_admin_whatsapp_message(text: str, *, instance_hint: str = "") -> dict[str, Any]:
    """Envia mensagem ao WhatsApp do admin usando a Evolution API configurada."""
    admin_number = settings.admin_whatsapp_number
    if not admin_number:
        logger.warning(
            "ADMIN_WHATSAPP_NUMBER ausente; notificação não enviada: %s",
            text[:500],
        )
        return {"status": "skipped", "reason": "admin_number_not_configured"}
    return _send_text_to_destination(admin_number, text, instance_hint=instance_hint)


def send_internal_notification_message(text: str, *, external_channel: str | None = None) -> dict[str, Any]:
    """
    Envia notificação para grupo técnico (quando configurado) e usa admin como fallback.
    """
    instance = _instance_from_external_channel(external_channel)
    group_jid = _resolve_group_for_instance(instance)
    gj = group_jid or ""
    logger.info(
        "notify_internal route instance=%r group=%r external_channel=%r",
        instance,
        gj[:20] + "…" if len(gj) > 20 else gj,
        external_channel,
    )
    if group_jid:
        result = _send_text_to_destination(group_jid, text, instance_hint=instance)
        if result.get("status") == "ok":
            return {"status": "ok", "destination": "tech_group", "result": result}
        logger.warning("Falha ao enviar para grupo interno; resultado=%s", result)
        if not settings.internal_notify_admin_fallback:
            return {"status": "error", "destination": "tech_group_failed", "result": result}
    else:
        logger.warning(
            "Nenhum TECH_GROUP mapeado para instance=%r (external_channel=%r)",
            instance,
            external_channel,
        )
        if not settings.internal_notify_admin_fallback:
            return {"status": "skipped", "reason": "no_group_for_instance"}
    if not instance and (settings.tech_group_jid or "").strip():
        logger.warning(
            "external_channel sem instância (ex.: só 'whatsapp'); usando TECH_GROUP_JID fallback para admin"
        )
    admin_result = send_admin_whatsapp_message(text, instance_hint=instance)
    return {"status": admin_result.get("status"), "destination": "admin_fallback", "result": admin_result}


def apply_whatsapp_label(*, remote_jid: str, label: str) -> dict[str, Any]:
    """
    Aplica etiqueta no chat via Evolution. Best effort: tenta endpoints/payloads comuns.
    """
    jid = str(remote_jid or "").strip()
    if not jid:
        return {"status": "skipped", "reason": "empty_remote_jid"}
    creds = _evolution_credentials()
    if not creds:
        return {"status": "skipped", "reason": "evolution_not_configured"}
    base_url, api_key, instance = creds
    endpoints = (
        f"{base_url}/chat/addLabel/{instance}",
        f"{base_url}/chat/updateLabel/{instance}",
        f"{base_url}/chat/markLabel/{instance}",
    )
    number = jid.split("@", 1)[0]
    payloads = (
        {"jid": jid, "label": label},
        {"chatId": jid, "label": label},
        {"number": number, "label": label},
        {"remoteJid": jid, "label": label},
    )
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    last_error: Exception | None = None
    for endpoint in endpoints:
        for payload in payloads:
            try:
                response = requests.post(endpoint, headers=headers, json=payload, timeout=20)
                response.raise_for_status()
                return {"status": "ok", "endpoint": endpoint}
            except Exception as exc:
                last_error = exc
    logger.warning("Falha ao aplicar etiqueta=%s no jid=%s erro=%s", label, jid, last_error)
    return {"status": "error", "message": str(last_error) if last_error else "unknown"}


def format_lead_notification(title: str, lead: dict[str, Any], extra: str = "") -> str:
    lines = [
        title,
        f"Cliente: {lead.get('display_name') or '—'}",
        f"Telefone/canal: {lead.get('phone') or lead.get('external_user_id')}",
        f"Serviço: {lead.get('service_type') or '—'}",
        f"Endereço: {lead.get('address') or '—'}",
        f"Janela: {lead.get('preferred_window') or '—'}",
        f"Estágio: {lead.get('stage')}",
    ]
    if extra:
        lines.append(extra)
    return "\n".join(lines)
