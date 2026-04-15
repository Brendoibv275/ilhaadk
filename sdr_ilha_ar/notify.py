# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Envio de notificações internas (Telegram)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from sdr_ilha_ar.config import settings

logger = logging.getLogger(__name__)


def send_telegram_message(text: str) -> dict[str, Any]:
    """POST sendMessage na API do Telegram. Sem token, apenas registra em log."""
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        logger.warning(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes; notificação não enviada: %s",
            text[:500],
        )
        return {"status": "skipped", "reason": "telegram_not_configured"}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return {"status": "ok", "response": raw[:500]}
    except urllib.error.URLError as e:
        logger.exception("Falha ao enviar Telegram")
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
