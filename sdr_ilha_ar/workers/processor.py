# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Processamento de `automation_jobs`: notificação interna, follow-up, NPS, agenda (stub)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
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
    ext_ch = payload.get("external_channel") or (lead or {}).get("external_channel")
    result = send_internal_notification_message(text, external_channel=ext_ch)
    logger.info("notify_internal job=%s result=%s", job["id"], result)


def _process_send_followup(job: dict[str, Any]) -> None:
    """MVP: registra texto sugerido; integração WhatsApp no adaptador de canal.

    Suporta os templates da cadência de reengajamento (v2 pedido Kauan):
        followup_45min, followup_1h, followup_5h, followup_1d, followup_3d
    Cada template gera uma mensagem com prefixo [FOLLOWUP:<tag>] — o prompt do
    agente reconhece esse prefixo e adapta tom (e no caso 3d libera cupom R$50).

    Templates legados (`followup`, `orcamento_instalacao`) continuam funcionando
    com as mensagens originais.
    """
    import json

    lead_id = uuid.UUID(str(job["lead_id"]))
    lead = lead_repo.get_lead(lead_id) or {}
    name = lead.get("display_name") or "Cliente"
    raw_pl = job.get("payload") or {}
    if isinstance(raw_pl, str):
        pl: dict[str, Any] = json.loads(raw_pl) if raw_pl.strip() else {}
    else:
        pl = raw_pl if isinstance(raw_pl, dict) else {}
    template = pl.get("template", "followup")
    tag = pl.get("followup_tag")

    # G: gate do follow-up amarrado ao estágio.
    # A cadência (45min, 1h, 5h, 1d, 3d) é contada a partir do momento em que o
    # lead entrou em 'quoted' — não da última mensagem. Se o lead:
    #   (a) não está mais em 'quoted' (avançou ou voltou a estágio anterior), pula;
    #   (b) está em 'quoted' há menos tempo que o threshold (ex: reentrou), pula;
    # Isso evita disparar cupom R$50 em lead que já agendou ou que acabou de
    # ser requotado. Templates legados ficam isentos da checagem.
    cadence_thresholds = {
        "followup_45min": timedelta(minutes=45),
        "followup_1h": timedelta(hours=1),
        "followup_5h": timedelta(hours=5),
        "followup_1d": timedelta(days=1),
        "followup_3d": timedelta(days=3),
    }
    threshold = cadence_thresholds.get(template)
    if threshold is not None:
        current_stage = lead.get("stage")
        if current_stage != "quoted":
            logger.info(
                "[send_followup] lead=%s template=%s pulado — estagio atual=%s (nao e quoted)",
                lead_id, template, current_stage,
            )
            return
        quoted_dur = lead_repo.get_stage_duration_for(lead_id, "quoted")
        if quoted_dur is None:
            # Sem histórico (lead antigo pré-migration). Segue o fluxo.
            logger.info(
                "[send_followup] lead=%s template=%s sem historico de quoted, prosseguindo",
                lead_id, template,
            )
        elif quoted_dur < threshold:
            logger.info(
                "[send_followup] lead=%s template=%s pulado — quoted ha %s < threshold %s",
                lead_id, template, quoted_dur, threshold,
            )
            return

    # Mensagens por template da cadência nova.
    # As mensagens começam com [FOLLOWUP:<tag>] pra que o agente (quando re-engajar
    # via LLM) reconheça o contexto e adapte. Por ora o stub apenas registra.
    cadence_templates = {
        "followup_45min": (
            f"[FOLLOWUP:45min] Oi {name}! Só passando pra ver se ficou alguma dúvida "
            f"sobre o orçamento. Qualquer coisa tô por aqui, tranquilo?"
        ),
        "followup_1h": (
            f"[FOLLOWUP:1h] Oi {name}, tudo bem? Posso te ajudar com alguma dúvida "
            f"sobre o serviço?"
        ),
        "followup_5h": (
            f"[FOLLOWUP:5h] {name}, ainda quer fechar o serviço? Se precisar de "
            f"condição diferente, me avisa."
        ),
        "followup_1d": (
            f"[FOLLOWUP:1d] Oi {name}! Passou 1 dia e não retornou — tá tudo bem? "
            f"Precisa de algum ajuste no orçamento ou tem alguma dúvida?"
        ),
        "followup_3d": (
            f"[FOLLOWUP:3d_coupon_50] Oi {name}! 🎯 Tô liberando um cupom relâmpago "
            f"de R$ 50 de desconto pra ti no orçamento que passei, válido pras "
            f"próximas 48h. Bora aproveitar?"
        ),
    }

    # Templates legados (compatibilidade).
    legacy_templates = {
        "followup": (
            f"Oi, {name}! Tudo bem? Vi que conversamos sobre o serviço. "
            f"Ficou alguma dúvida sobre valores ou prefere uma condição de pagamento?"
        ),
        "orcamento_instalacao": (
            f"Oi, {name}! Passando para ver se ficou alguma dúvida sobre o orçamento "
            f"de instalação ou se quer que eu veja condição melhor de pagamento."
        ),
    }

    msg = cadence_templates.get(template) or legacy_templates.get(template) or legacy_templates["followup"]

    # Regra de negócio do cupom de 3 dias: só vale se o lead realmente recebeu
    # orçamento numérico. Caso contrário, cai pra reengajamento neutro.
    if template == "followup_3d":
        quoted_amount = lead.get("quoted_amount")
        try:
            quoted_num = float(quoted_amount) if quoted_amount not in (None, "") else 0.0
        except (TypeError, ValueError):
            quoted_num = 0.0
        if quoted_num <= 0:
            msg = (
                f"[FOLLOWUP:3d_no_quote] Oi {name}! Já faz uns dias — caso ainda "
                f"precise do serviço, a visita técnica é gratuita. Me avisa quando "
                f"for bom pra ti."
            )

    logger.info("[send_followup] lead=%s template=%s tag=%s -> %s", lead_id, template, tag, msg)
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
    result = send_internal_notification_message(
        msg,
        external_channel=(lead or {}).get("external_channel"),
    )
    logger.info("six_month_cleaning_followup job=%s result=%s", job["id"], result)
    lead_repo.append_message(
        lead_id,
        "tool",
        "six_month_cleaning_followup enviado para equipe interna",
        {"job_id": str(job["id"])},
    )


def _process_followup_recall_6m(job: dict[str, Any]) -> None:
    """H — Recall 6 meses pós-conclusão.

    Disparado 180 dias após marcar appointment como completed. Regras:
    - Se lead não existe mais: pula.
    - Se lead já tem appointment ativo (pending/proposed/confirmed/realloc): pula
      (não queremos oferta de recall colidindo com atendimento em curso).
    - Caso contrário, grava mensagem com prefixo [FOLLOWUP:6m_recall] pra que o
      agente LLM adapte o tom e ofereça limpeza de manutenção por R$ 280.
    """
    lead_id = uuid.UUID(str(job["lead_id"]))
    lead = lead_repo.get_lead(lead_id)
    if not lead:
        logger.info("[followup_recall_6m] lead=%s nao existe, pulado", lead_id)
        return

    # Evita colidir com atendimento em andamento.
    try:
        active = lead_repo.list_active_appointments_for_lead(lead_id)
    except AttributeError:
        # Compat: instalação antiga sem a helper ainda — segue o fluxo.
        active = []
    if active:
        logger.info(
            "[followup_recall_6m] lead=%s tem %d appointment(s) ativo(s), pulado",
            lead_id,
            len(active),
        )
        return

    name = lead.get("display_name") or "Cliente"
    msg = (
        f"[FOLLOWUP:6m_recall] E aí {name}! Passou 6 meses desde o último "
        f"serviço. Tô liberando uma limpeza de manutenção promocional por "
        f"R$ 280 (valor especial cliente retorno). Quer que eu agende?"
    )
    logger.info("[followup_recall_6m] lead=%s disparando oferta R$280", lead_id)
    lead_repo.append_message(lead_id, "assistant_outbound_stub", msg)

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
        elif jtype == "followup_recall_6m":
            _process_followup_recall_6m(job)
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
