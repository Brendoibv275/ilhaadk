# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Processamento de `automation_jobs`: notificação interna, follow-up, NPS, agenda (stub)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sdr_ilha_ar import repository as lead_repo
from sdr_ilha_ar.config import settings
from sdr_ilha_ar.notify import format_lead_notification, send_internal_notification_message

logger = logging.getLogger(__name__)


def _process_notify_internal(job: dict[str, Any]) -> None:
    import json

    lead_id = uuid.UUID(str(job["lead_id"]))
    lead = lead_repo.get_lead(lead_id)
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload) if payload.strip() else {}
    tag = payload.get("tag", "")
    title = payload.get("title", "NOTIFICAÇÃO INTERNA Ilha Ar")
    if tag:
        title = f"{tag} {title}"
    extra_lines = []
    if payload.get("reason"):
        extra_lines.append(f"Motivo: {payload['reason']}")
    if payload.get("service_type"):
        extra_lines.append(f"Solicitação: {payload['service_type']}")
    if payload.get("display_name"):
        extra_lines.append(f"Nome: {payload['display_name']}")
    if payload.get("address"):
        extra_lines.append(f"Endereço: {payload['address']}")
    if payload.get("window_label"):
        extra_lines.append(f"Janela pedida: {payload['window_label']}")
    if payload.get("notes"):
        extra_lines.append(f"Notas: {payload['notes']}")
    extra = "\n".join(extra_lines)
    text = format_lead_notification(
        title,
        lead or {},
        extra=extra,
    )
    result = send_internal_notification_message(text)
    logger.info("notify_internal job=%s result=%s", job["id"], result)


def _process_send_followup(job: dict[str, Any]) -> None:
    """MVP: registra texto sugerido; integração WhatsApp no adaptador de canal."""
    import json

    lead_id = uuid.UUID(str(job["lead_id"]))
    lead = lead_repo.get_lead(lead_id)
    name = (lead or {}).get("display_name") or "Cliente"
    raw_pl = job.get("payload") or {}
    if isinstance(raw_pl, str):
        pl: dict[str, Any] = json.loads(raw_pl) if raw_pl.strip() else {}
    else:
        pl = raw_pl if isinstance(raw_pl, dict) else {}
    template = pl.get("template", "followup")
    msg = (
        f"Oi, {name}! Tudo bem? Vi que conversamos sobre o serviço. "
        f"Ficou alguma dúvida sobre valores ou prefere uma condição de pagamento?"
    )
    if template == "orcamento_instalacao":
        msg = (
            f"Oi, {name}! Passando para ver se ficou alguma dúvida sobre o orçamento "
            f"de instalação ou se quer que eu veja condição melhor de pagamento."
        )
    logger.info("[send_followup] lead=%s template=%s -> %s", lead_id, template, msg)
    lead_repo.append_message(lead_id, "assistant_outbound_stub", msg)


def _process_nps(job: dict[str, Any]) -> None:
    lead_id = uuid.UUID(str(job["lead_id"]))
    lead = lead_repo.get_lead(lead_id)
    name = (lead or {}).get("display_name") or "Cliente"
    msg = (
        f"Olá, {name}! Aqui é da Ilha Ar. O serviço ficou 100% e o ar está gelando "
        f"direitinho? Responda com um OK ou nos conte se algo precisar de ajuste."
    )
    review = settings.google_review_url or "(configure GOOGLE_REVIEW_URL)"
    msg_after = f"Se estiver tudo certo, avalie aqui: {review}"
    logger.info("[nps] %s | pós-msg: %s", msg, msg_after)
    lead_repo.append_message(lead_id, "assistant_outbound_stub", msg + "\n" + msg_after)


def _process_check_calendar(job: dict[str, Any]) -> None:
    """Fase 2: Google Calendar. MVP: marca como livre e sugere confirmação."""
    lead_id = uuid.UUID(str(job["lead_id"]))
    lead = lead_repo.get_lead(lead_id)
    window = (lead or {}).get("preferred_window") or "janela combinada"
    logger.info(
        "[check_calendar stub] lead=%s window=%s — tratar como horário livre (MVP)",
        lead_id,
        window,
    )
    lead_repo.append_message(
        lead_id,
        "assistant_outbound_stub",
        f"(MVP) Horário {window!r} disponível na rota. Confirme com o cliente.",
    )


def _process_abandonment_check(job: dict[str, Any]) -> None:
    lead_id = uuid.UUID(str(job["lead_id"]))
    lead = lead_repo.get_lead(lead_id)
    if not lead:
        return
    stage = lead.get("stage")
    if stage in {"new", "qualified"}:
        name = lead.get("display_name") or "Cliente"
        msg = (
            f"Olá {name}, tudo bem? Sou eu, Kauan da Ilha Ar de novo! "
            f"Vi que não concluímos nosso atendimento. Ficou alguma dúvida sobre nossos serviços "
            f"ou gostaria de retomar o orçamento onde paramos?"
        )
        logger.info("[abandonment_check] lead=%s retentando contato", lead_id)
        lead_repo.append_message(lead_id, "assistant_outbound_stub", msg)
        
        # Opcionalmente re-enfilera para tentar novamente
        # lead_repo.enqueue_job(lead_id, "abandonment_check2", ...)
    else:
        logger.info("[abandonment_check] lead=%s evoluiu de estagio, ignorado.", lead_id)


def _process_six_month_cleaning_followup(job: dict[str, Any]) -> None:
    lead_id = uuid.UUID(str(job["lead_id"]))
    lead = lead_repo.get_lead(lead_id) or {}
    name = lead.get("display_name") or "Cliente"
    addr = lead.get("address") or "endereço não informado"
    msg = (
        "LEMBRETE PÓS-SERVIÇO (6 MESES)\n"
        f"Cliente: {name}\n"
        f"Endereço: {addr}\n"
        "Ação: oferecer limpeza preventiva e revisão do ar-condicionado."
    )
    result = send_internal_notification_message(msg)
    logger.info("six_month_cleaning_followup job=%s result=%s", job["id"], result)
    lead_repo.append_message(
        lead_id,
        "tool",
        "six_month_cleaning_followup enviado para equipe interna",
        {"job_id": str(job["id"])},
    )

def process_job(job: dict[str, Any]) -> None:
    jid = uuid.UUID(str(job["id"]))
    jtype = job["job_type"]
    try:
        if jtype == "notify_internal":
            _process_notify_internal(job)
        elif jtype == "send_followup":
            _process_send_followup(job)
        elif jtype == "nps":
            _process_nps(job)
        elif jtype == "check_calendar":
            _process_check_calendar(job)
        elif jtype == "abandonment_check":
            _process_abandonment_check(job)
        elif jtype == "six_month_cleaning_followup":
            _process_six_month_cleaning_followup(job)
        else:
            raise ValueError(f"job_type desconhecido: {jtype}")
        lead_repo.complete_job(jid)
    except Exception as e:
        logger.exception("Job %s falhou", jid)
        lead_repo.fail_job(jid, str(e))


def run_tick(limit: int = 20) -> int:
    """Processa até `limit` jobs pendentes. Retorna quantos foram concluídos."""
    jobs = lead_repo.list_pending_jobs_due(limit=limit)
    for job in jobs:
        process_job(job)
    return len(jobs)
