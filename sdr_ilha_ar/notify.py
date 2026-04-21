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

from sdr_ilha_ar.config import settings

logger = logging.getLogger(__name__)


def send_admin_whatsapp_message(text: str) -> dict[str, Any]:
    """Envia mensagem ao WhatsApp do admin usando a Evolution API configurada."""
    admin_number = settings.admin_whatsapp_number
    if not admin_number:
        logger.warning(
            "ADMIN_WHATSAPP_NUMBER ausente; notificação não enviada: %s",
            text[:500],
        )
        return {"status": "skipped", "reason": "admin_number_not_configured"}

    import os
    base_url = (os.getenv("EVOLUTION_BASE_URL") or "").rstrip("/")
    api_key = (os.getenv("EVOLUTION_API_KEY") or "").strip()
    instance = (os.getenv("EVOLUTION_INSTANCE") or "").strip()

    if not (base_url and api_key and instance):
        logger.warning("Credenciais da Evolution indisponíveis para notificação de admin.")
        return {"status": "skipped", "reason": "evolution_not_configured"}

    url = f"{base_url}/message/sendText/{instance}"
    body = json.dumps(
        {"number": admin_number, "text": text}
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
        logger.exception("Falha ao enviar notificação WhatsApp para admin")
        return {"status": "error", "message": str(e)}


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
