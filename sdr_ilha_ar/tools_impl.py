# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tools do SDR: persistência Postgres, precificação e fila de automações."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from google.adk.tools import ToolContext

from sdr_ilha_ar import repository as lead_repo
from sdr_ilha_ar.config import settings
from sdr_ilha_ar.repository import DatabaseNotConfiguredError, DatabaseUnavailableError
from sdr_ilha_ar.notify import apply_whatsapp_label

logger = logging.getLogger(__name__)
BR_TZ = ZoneInfo("America/Fortaleza")
WEEKDAY_PT = {
    "segunda": 0,
    "segunda-feira": 0,
    "terca": 1,
    "terça": 1,
    "terca-feira": 1,
    "terça-feira": 1,
    "quarta": 2,
    "quarta-feira": 2,
    "quinta": 3,
    "quinta-feira": 3,
    "sexta": 4,
    "sexta-feira": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
}

FIXED_SERVICE_QUOTES_BRL: dict[str, float] = {
    "higienizacao": 150.0,
    "manutencao_preventiva": 150.0,
    "carga_gas_revisao": 180.0,
    "visita_tecnica_gratis": 0.0,
}


def _resolve_lead_id(tool_context: ToolContext) -> uuid.UUID:
    raw = tool_context.state.get("lead_id")
    if raw:
        return uuid.UUID(str(raw))
    # ADK >= 1.30: ToolContext é alias de Context (sem .invocation_context público).
    external_user_id = tool_context.user_id
    channel = tool_context.state.get("external_channel") or settings.default_external_channel
    lead_repo.reconcile_whatsapp_instance_channel(external_user_id, channel)
    lid = lead_repo.ensure_lead(channel, external_user_id, touch_inbound=True)
    tool_context.state["lead_id"] = str(lid)
    return lid


def _external_channel_for_notify(tool_context: ToolContext, row: dict[str, Any]) -> str:
    return str(
        tool_context.state.get("external_channel")
        or row.get("external_channel")
        or ""
    ).strip()


def _db_error(e: Exception) -> dict[str, Any]:
    logger.exception("Erro de persistência nas tools do SDR")
    return {"status": "error", "message": str(e)}


def _label_lead_chat(lead_id: uuid.UUID, label: str) -> None:
    """Best effort para etiquetar o chat no WhatsApp via Evolution."""
    try:
        lead = lead_repo.get_lead(lead_id) or {}
        raw = str(lead.get("external_user_id") or "").strip()
        if not raw:
            return
        digits = re.sub(r"\D+", "", raw)
        if not digits:
            return
        remote_jid = f"{digits}@s.whatsapp.net"
        apply_whatsapp_label(remote_jid=remote_jid, label=label)
    except Exception:
        logger.exception("Falha ao etiquetar chat do lead=%s label=%s", lead_id, label)


def _pt_sim(val: str | None) -> bool | None:
    """Interpreta sim/não em PT-BR; None se não deu para saber."""
    if val is None or not str(val).strip():
        return None
    s = str(val).lower().strip()
    if s in ("sim", "s", "yes", "true", "1", "tem", "já tem", "ja tem"):
        return True
    if s in ("nao", "não", "n", "no", "false", "0", "nao tem", "não tem", "sem"):
        return False
    return None


def _normalize_preferred_window(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    low = raw.lower()
    now_br = datetime.now(timezone.utc).astimezone(BR_TZ)
    today = now_br.date()
    tomorrow = today + timedelta(days=1)
    out = raw
    if "depois de amanh" in low:
        d2 = today + timedelta(days=2)
        out = re.sub(r"depois de amanh[ãa]", d2.strftime("%d/%m/%Y"), out, flags=re.IGNORECASE)
    if "amanh" in low:
        out = re.sub(r"amanh[ãa]", tomorrow.strftime("%d/%m/%Y"), out, flags=re.IGNORECASE)
    if "hoje" in low:
        out = re.sub(r"\bhoje\b", today.strftime("%d/%m/%Y"), out, flags=re.IGNORECASE)
    # Resolve dia da semana para próxima ocorrência (>= hoje).
    low_norm = low.replace("ç", "c")
    for token, weekday in WEEKDAY_PT.items():
        if token in low_norm:
            delta = (weekday - today.weekday()) % 7
            target = today + timedelta(days=delta)
            out = re.sub(token, target.strftime("%d/%m/%Y"), out, flags=re.IGNORECASE)
            break
    return out


def _extract_first_date_ddmmyyyy(value: str) -> datetime | None:
    m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", value or "")
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(year, month, day, tzinfo=BR_TZ)
    except ValueError:
        return None


def get_current_datetime() -> dict[str, Any]:
    """Retorna data/hora atual de São Luís para orientar agendamentos relativos."""
    now_br = datetime.now(timezone.utc).astimezone(BR_TZ)
    return {
        "status": "ok",
        "timezone": "America/Fortaleza",
        "date": now_br.strftime("%d/%m/%Y"),
        "time": now_br.strftime("%H:%M"),
        "iso": now_br.isoformat(),
        "weekday": now_br.strftime("%A"),
        "hint": "Use estes valores como referência para hoje/amanhã e horários.",
    }


def _advance_lead_to_scheduled(lead_id: uuid.UUID) -> None:
    row = lead_repo.get_lead(lead_id)
    if not row:
        return
    current = str(row.get("stage") or "new")
    target_path = ["qualified", "quoted", "awaiting_slot", "scheduled"]
    if current in target_path:
        idx = target_path.index(current) + 1
    else:
        idx = 0
    for stage in target_path[idx:]:
        try:
            lead_repo.set_lead_stage(lead_id, stage)
        except ValueError:
            break


def _crm_service_slug(st: str) -> str:
    """Slug canônico de service_type para coluna leads.service_type."""
    if st in ("limpeza", "higienizacao"):
        return "higienizacao"
    if st in ("visita_gratis", "defeito", "manutencao_corretiva"):
        return "visita_tecnica_gratis"
    return st


def _finalize_ok_quote(
    tool_context: ToolContext | None,
    st: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Persiste service_type e quoted_amount após cotação ok (não bloqueia resposta ao cliente)."""
    if result.get("status") != "ok":
        return result
    if tool_context is None:
        return result
    try:
        lead_id = _resolve_lead_id(tool_context)
        slug = _crm_service_slug(st)
        amt = result.get("amount_brl")
        lead_repo.save_lead_field(lead_id, "service_type", slug)
        if amt is not None:
            lead_repo.save_lead_field(lead_id, "quoted_amount", str(amt))
        lead_repo.append_message(
            lead_id,
            "tool",
            f"get_pricing_quote CRM service_type={slug!r} quoted_amount={amt!r}",
        )
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as e:
        logger.warning("CRM persist após get_pricing_quote falhou (DB): %s", e)
    except (ValueError, LookupError) as e:
        logger.warning("CRM persist após get_pricing_quote falhou: %s", e)
    return result


def get_pricing_quote(
    service_type: str,
    btus: int | None = None,
    has_own_tubing: str | None = None,
    requires_wall_or_wiring: str | None = None,
    needs_scaffold_exterior: str | None = None,
    scaffold_floor: int | None = None,
    easy_access: str | None = None,
    floor_level: int | None = None,
    tubing_complex: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """
    Tabela Ilha Ar — **Somente São Luís** (valores oficiais do negócio).

    Args:
        service_type: higienizacao | manutencao_preventiva | carga_gas_revisao |
            instalacao | visita_tecnica_gratis (ou limpeza/instalacao/defeito como alias).
        btus: potência (instalação). 9k–12k no pacote base; acima de 18k regra especial.
        has_own_tubing: sim/nao — cliente já tem tubulação? Se não, material ~R$ 200 (2 m).
        requires_wall_or_wiring: sim/nao — precisa quebrar parede/teto ou fazer fiação.
        needs_scaffold_exterior: sim/nao — andaime ou escada alta por fora do prédio.
        scaffold_floor: 1, 2 ou 3 — andar para aluguel do andaime (pago à parte pelo cliente).
        easy_access: sim = térreo, sacada, varanda (acesso fácil); nao = mais difícil.
        floor_level: legado; se scaffold_floor vazio e for 1–3, pode alinhar com andaime.
        tubing_complex: legado; se disser "sem"/"média", ajuda a inferir tubulação.

    Returns:
        amount_brl estimativa principal (mão de obra + material quando couber);
        scaffold_rental_client_brl separado (cliente paga direto ao fornecedor).

    Quando `tool_context` é fornecido pelo ADK, grava `service_type` e `quoted_amount` no lead.
    """
    raw = service_type.lower().strip()
    if "preventiva" in raw.replace(" ", ""):
        st = "manutencao_preventiva"
    elif "corretiva" in raw.replace(" ", "") or raw in ("defeito", "corretiva"):
        st = "visita_tecnica_gratis"
    else:
        st = raw.replace(" ", "_").replace("ção", "cao")
    # aliases
    if st in ("limpeza", "higienizacao"):
        return _finalize_ok_quote(
            tool_context,
            st,
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 150.0,
                "labor_brl": 150.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Higienização completa: R$ 150,00. Limpeza profunda interna "
                    "(sujeira, mofo, bactérias). Válido em São Luís."
                ),
            },
        )
    if st in ("manutencao_preventiva", "manutenção preventiva", "preventiva"):
        return _finalize_ok_quote(
            tool_context,
            st,
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 150.0,
                "labor_brl": 150.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": "Manutenção preventiva: a partir de R$ 150,00 (São Luís).",
            },
        )
    if st in ("carga_gas_revisao", "carga_gas", "gas", "recarga"):
        return _finalize_ok_quote(
            tool_context,
            st,
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 180.0,
                "labor_brl": 180.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": "Carga de gás + revisão: a partir de R$ 180,00 (São Luís).",
            },
        )

    if st in ("visita_tecnica_gratis", "visita_gratis", "defeito", "manutencao_corretiva"):
        return _finalize_ok_quote(
            tool_context,
            st,
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 0.0,
                "labor_brl": 0.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Visita técnica presencial gratuita para avaliação (sem orçamento remoto "
                    "nesse caso). Indicado: cassete/piso-teto, quebra de teto/fiação, não gela/"
                    "vazamento, cliente não sabe explicar o problema. São Luís."
                ),
            },
        )

    if st != "instalacao":
        return {
            "status": "error",
            "message": (
                "service_type inválido. Use: higienizacao, manutencao_preventiva, "
                "carga_gas_revisao, instalacao ou visita_tecnica_gratis (ou defeito)."
            ),
        }

    # --- Instalação (regras Ilha Ar) ---
    wall_or_wiring = _pt_sim(requires_wall_or_wiring)
    if wall_or_wiring is None:
        tc = (tubing_complex or "").lower()
        high_risk_tokens = (
            "quebrar parede",
            "quebra de parede",
            "quebrar teto",
            "quebra de teto",
            "fiação",
            "fiacao",
            "eletrica",
            "elétrica",
        )
        if any(token in tc for token in high_risk_tokens):
            wall_or_wiring = True

    if wall_or_wiring is None:
        return _finalize_ok_quote(
            tool_context,
            "visita_tecnica_gratis",
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 0.0,
                "labor_brl": 0.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Para evitar erro no diagnóstico, vamos de visita técnica gratuita. "
                    "Assim avaliamos o local com segurança e te passamos o melhor cenário."
                ),
            },
        )

    # Regra crítica: se envolver quebra de parede/teto ou fiação, não orçar remoto.
    if wall_or_wiring is True:
        return _finalize_ok_quote(
            tool_context,
            "instalacao",
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 0.0,
                "labor_brl": 0.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Nesse caso, precisamos de visita técnica presencial gratuita antes de "
                    "passar orçamento, pois há necessidade de quebra estrutural/fiação."
                ),
            },
        )

    own = _pt_sim(has_own_tubing)
    if own is None and tubing_complex:
        tc = tubing_complex.lower()
        if any(
            x in tc
            for x in ("sem tubo", "nao tenho", "não tenho", "preciso comprar", "precisa tubo")
        ):
            own = False
        elif any(x in tc for x in ("ja tenho", "já tenho", "tenho a tubo", "tenho tubo")):
            own = True
    needs_scaf = _pt_sim(needs_scaffold_exterior)
    easy = _pt_sim(easy_access)
    if easy is None:
        return _finalize_ok_quote(
            tool_context,
            "visita_tecnica_gratis",
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 0.0,
                "labor_brl": 0.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Se o acesso ainda não está claro, seguimos com visita técnica gratuita. "
                    "É o caminho mais seguro para fechar o orçamento sem erro."
                ),
            },
        )

    if needs_scaf is True or easy is False:
        return _finalize_ok_quote(
            tool_context,
            "visita_tecnica_gratis",
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 0.0,
                "labor_brl": 0.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Para instalação com acesso complexo (altura/andaime/área externa), "
                    "vamos de visita técnica gratuita para avaliar com segurança."
                ),
            },
        )

    labor = 300.0

    tubing_extra = 0.0
    if own is False:
        tubing_extra = 200.0

    total = labor + tubing_extra
    labor_note = "instalação padrão com acesso fácil (térreo/sacada/varanda)"
    parts = [f"Mão de obra instalação Ilha Breeze: R$ {labor:.0f} ({labor_note})."]
    if own is False:
        parts.append(
            "Cliente sem tubulação: material ~2 m ≈ R$ 200. "
            f"Total indicativo ≈ R$ {total:.0f} (mão de obra + material), sem margem em peça."
        )
    elif own is True:
        parts.append("Cliente já tem tubulação: cobrar só mão de obra conforme regra acima.")
    else:
        parts.append(
            "Se o cliente já tiver tubulação, segue só mão de obra (R$ 300). "
            "Sem tubulação, some ~R$ 200 de material (transparente, sem margem)."
        )

    parts.append(
        "Diferencial Ilha Breeze: você paga mão de obra e material separado, "
        "com repasse transparente de peças (sem margem escondida)."
    )
    parts.append(
        "No mercado, muitos pacotes fechados ficam entre R$ 650 e R$ 700 no total."
    )

    return _finalize_ok_quote(
        tool_context,
        "instalacao",
        {
            "status": "ok",
            "currency": "BRL",
            "amount_brl": round(total, 2),
            "labor_brl": round(labor, 2),
            "materials_tubing_brl": round(tubing_extra, 2),
            "scaffold_rental_client_brl": None,
            "summary": " ".join(parts) + " Referência: São Luís.",
        },
    )


def save_lead_field(
    field_name: str,
    value: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Persiste um campo do lead. field_name deve ser um dos permitidos no backend."""
    try:
        lead_id = _resolve_lead_id(tool_context)
        normalized = _normalize_preferred_window(value) if field_name == "preferred_window" else value
        row = lead_repo.save_lead_field(lead_id, field_name, normalized)
        if field_name == "service_type":
            # Ajuste de robustez: quando o atendimento avança sem get_pricing_quote,
            # mantém quoted_amount coerente para serviços de preço fixo.
            slug = _crm_service_slug(str(normalized).strip().lower())
            fixed_amount = FIXED_SERVICE_QUOTES_BRL.get(slug)
            if fixed_amount is not None and row.get("quoted_amount") is None:
                lead_repo.save_lead_field(lead_id, "quoted_amount", str(fixed_amount))
                lead_repo.append_message(
                    lead_id,
                    "tool",
                    f"autosync quoted_amount={fixed_amount:.2f} from service_type={slug}",
                )
        lead_repo.append_message(lead_id, "tool", f"save_lead_field {field_name}={value!r}")
        return {"status": "ok", "lead_id": str(lead_id), "stage": row.get("stage")}
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as e:
        return _db_error(e)
    except (ValueError, LookupError) as e:
        return {"status": "error", "message": str(e)}


def set_lead_stage(target_stage: str, tool_context: ToolContext) -> dict[str, Any]:
    """Altera o estágio do lead respeitando a máquina de estados."""
    try:
        lead_id = _resolve_lead_id(tool_context)
        row = lead_repo.set_lead_stage(lead_id, target_stage.strip())
        lead_repo.append_message(lead_id, "tool", f"set_lead_stage -> {target_stage}")
        if row.get("stage") == "qualified":
            _label_lead_chat(lead_id, "novo_lead")
        if row.get("stage") == "completed" and settings.six_month_followup_enabled:
            run_at = datetime.now(timezone.utc) + timedelta(days=max(1, settings.six_month_followup_days))
            lead_repo.enqueue_job(
                lead_id=lead_id,
                job_type="six_month_cleaning_followup",
                run_at=run_at,
                payload={"trigger": "lead_completed", "days": settings.six_month_followup_days},
                idempotency_key=f"six_month_followup_{lead_id}_{run_at.date().isoformat()}",
            )
            lead_repo.append_message(
                lead_id,
                "tool",
                f"six_month_cleaning_followup agendado para {run_at.isoformat()}",
            )
        return {"status": "ok", "stage": row["stage"], "lead_id": str(lead_id)}
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as e:
        return _db_error(e)
    except (ValueError, LookupError) as e:
        return {"status": "error", "message": str(e)}


def enqueue_automation_job(
    job_type: str,
    run_at_iso: str,
    idempotency_key: str,
    payload_json: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """
    Enfileira um job de automação.

    Args:
        job_type: check_calendar | send_followup | notify_internal | nps
        run_at_iso: data/hora UTC ISO-8601 (ex.: 2026-04-15T14:00:00+00:00)
        idempotency_key: chave única para evitar duplicidade
        payload_json: JSON string com detalhes (texto livre ou estruturado)
    """
    try:
        payload: dict[str, Any] = json.loads(payload_json) if payload_json.strip() else {}
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"payload_json inválido: {e}"}

    try:
        run_at = datetime.fromisoformat(run_at_iso.replace("Z", "+00:00"))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
    except ValueError as e:
        return {"status": "error", "message": f"run_at_iso inválido: {e}"}

    try:
        lead_id = _resolve_lead_id(tool_context)
        jid = lead_repo.enqueue_job(lead_id, job_type.strip(), run_at, payload, idempotency_key)
        if jid is None:
            return {
                "status": "skipped",
                "message": "Job já existia (idempotency_key duplicada).",
            }
        lead_repo.append_message(lead_id, "tool", f"enqueue_job {job_type} id={jid}")
        return {"status": "ok", "job_id": str(jid)}
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as e:
        return _db_error(e)


def request_human_handoff(reason: str, tool_context: ToolContext) -> dict[str, Any]:
    """Marca emergência e agenda notificação imediata para a equipe interna."""
    try:
        lead_id = _resolve_lead_id(tool_context)
        row = lead_repo.get_lead(lead_id)
        current = row["stage"] if row else "new"
        # permite ir para emergency_handoff a partir de qualquer estágio conhecido
        from sdr_ilha_ar import state_machine

        if current in state_machine.STAGES and current != "emergency_handoff":
            try:
                lead_repo.set_lead_stage(lead_id, "emergency_handoff")
            except ValueError:
                # se transição direta falhar, ainda assim notifica
                pass
        now = datetime.now(timezone.utc)
        row_notify = lead_repo.get_lead(lead_id) or {}
        lead_repo.enqueue_job(
            lead_id,
            "notify_internal",
            now,
            {
                "tag": "[EMERGÊNCIA]",
                "reason": reason,
                "external_channel": _external_channel_for_notify(tool_context, row_notify),
            },
            f"handoff_{lead_id}_{int(now.timestamp())}",
        )
        lead_repo.insert_outbox_event(lead_id, "human_handoff", {"reason": reason})
        lead_repo.append_message(lead_id, "tool", f"human_handoff: {reason}")
        return {"status": "ok", "message": "Equipe interna será notificada."}
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as e:
        return _db_error(e)


def mark_quote_sent(
    tool_context: ToolContext,
    client_facing_total_brl: str | None = None,
) -> dict[str, Any]:
    """
    Registra envio de orçamento e agenda follow-up em 4 horas (idempotente por lead).

    Use `client_facing_total_brl` com o valor **exato** (total Ilha Ar: mão de obra + material)
    que você acabou de comunicar ao cliente, para o CRM ficar igual ao discurso.
    Ex.: \"450\", \"450.00\".
    """
    try:
        lead_id = _resolve_lead_id(tool_context)
        total = (client_facing_total_brl or "").strip()
        if total:
            lead_repo.save_lead_field(lead_id, "quoted_amount", total)
            lead_repo.append_message(
                lead_id,
                "tool",
                f"mark_quote_sent sync quoted_amount={total!r} (valor comunicado ao cliente)",
            )

        # Tenta avançar o lead para quoted
        row = lead_repo.get_lead(lead_id)
        current = str(row.get("stage") or "new") if row else "new"
        if current in {"new", "qualified"}:
            try:
                # Se for new, a máquina de estados (em state_machine.py) exige ir pra qualified e depois quoted,
                # ou ela permite new -> quoted nativamente (VALID_TRANSITIONS["new"] tem "quoted").
                lead_repo.set_lead_stage(lead_id, "quoted")
            except ValueError:
                pass

        lead_repo.mark_quote_sent(lead_id)
        run_at = datetime.now(timezone.utc) + timedelta(hours=4)
        lead_repo.enqueue_job(
            lead_id,
            "send_followup",
            run_at,
            {"template": "orcamento_instalacao"},
            f"followup_quote_{lead_id}",
        )
        lead_repo.append_message(lead_id, "tool", "mark_quote_sent + followup 4h")
        _label_lead_chat(lead_id, "orcado")
        return {
            "status": "ok",
            "followup_scheduled_at": run_at.isoformat(),
            "quoted_amount_synced": total or None,
        }
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as e:
        return _db_error(e)
    except LookupError as e:
        return {"status": "error", "message": str(e)}


def register_appointment_request(
    window_label: str,
    notes: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """
    Registra pedido de agendamento e notifica a equipe.

    Se `preferred_window` ainda não estiver no banco, mas `window_label` vier
    preenchido (ex.: resumo da confirmação com data/hora), grava automaticamente
    em `preferred_window` para não falhar só porque o modelo esqueceu um save.
    """
    try:
        lead_id = _resolve_lead_id(tool_context)
        row = lead_repo.get_lead(lead_id)
        if not row:
            return {"status": "error", "message": "Lead não encontrado"}
        wl = _normalize_preferred_window((window_label or "").strip())
        if wl and not row.get("preferred_window"):
            lead_repo.save_lead_field(lead_id, "preferred_window", wl)
            row = lead_repo.get_lead(lead_id)
        missing = []
        if not row.get("display_name"):
            missing.append("display_name")
        if not row.get("address"):
            missing.append("address")
        if not row.get("preferred_window") and not wl:
            missing.append("preferred_window (ou passe window_label com data/hora)")
        if missing:
            return {
                "status": "error",
                "message": f"Preencha antes de agendar: {', '.join(missing)}",
            }
        service_slug = _crm_service_slug(str(row.get("service_type") or "").strip().lower())
        if not service_slug:
            return {
                "status": "error",
                "message": "Preencha antes de agendar: service_type",
                "tell_client": (
                    "Perfeito! Só vou confirmar um detalhe do serviço aqui e já te retorno."
                ),
            }
        quoted_amount = row.get("quoted_amount")
        if quoted_amount is None:
            fixed_amount = FIXED_SERVICE_QUOTES_BRL.get(service_slug)
            if fixed_amount is not None:
                lead_repo.save_lead_field(lead_id, "quoted_amount", str(fixed_amount))
                lead_repo.append_message(
                    lead_id,
                    "tool",
                    f"autosync quoted_amount={fixed_amount:.2f} at appointment from service_type={service_slug}",
                )
                row = lead_repo.get_lead(lead_id) or row
            else:
                return {
                    "status": "error",
                    "message": "Preencha antes de agendar: quoted_amount",
                    "tell_client": (
                        "Perfeito! Vou só confirmar o valor certinho do serviço e já te retorno."
                    ),
                }
        final_window = wl or (row.get("preferred_window") or "")
        # Evita confirmar agendamento com data explícita no passado.
        dt = _extract_first_date_ddmmyyyy(final_window)
        if dt and dt.date() < datetime.now(timezone.utc).astimezone(BR_TZ).date():
            return {
                "status": "error",
                "message": "preferred_window contém data passada",
                "tell_client": (
                    "Para evitar erro no agendamento, me confirma novamente a data "
                    "desejada (dd/mm/aaaa)?"
                ),
            }
        lead_repo.create_appointment(lead_id, final_window, status="proposed")
        # Mantém o funil coerente após pedido (vai pro awaiting_slot aguardando a equipe).
        current_st = row.get("stage", "new")
        if current_st in {"new", "qualified", "quoted"}:
            try:
                lead_repo.set_lead_stage(lead_id, "awaiting_slot")
            except ValueError:
                pass
        
        # Se já agendou, follow-up de orçamento deixa de fazer sentido.
        lead_repo.cancel_pending_jobs_for_lead(lead_id, job_type="send_followup")
        now = datetime.now(timezone.utc)
        lead_repo.enqueue_job(
            lead_id,
            "notify_internal",
            now,
            {
                "title": "NOVO PEDIDO DE AGENDAMENTO",
                "window_label": final_window,
                "notes": notes,
                "service_type": row.get("service_type") or "nao_informado",
                "display_name": row.get("display_name") or "",
                "address": row.get("address") or "",
                "external_channel": _external_channel_for_notify(tool_context, row),
            },
            f"notify_appt_{lead_id}",
        )
        lead_repo.insert_outbox_event(lead_id, "appointment_requested", {"window": final_window})
        lead_repo.append_message(lead_id, "tool", f"register_appointment_request {final_window}")
        _label_lead_chat(lead_id, "agendado")
        name = row.get("display_name") or "Cliente"
        tell_client = (
            f"Prontinho, {name}! Seu pedido foi registrado para {final_window}. "
            "A equipe confere a rota e te confirma o horário final em breve. "
            "Se precisar mudar algo, é só avisar."
        )
        return {
            "status": "ok",
            "message": "Agendamento registrado; equipe interna notificada.",
            "tell_client": tell_client,
        }
    except DatabaseUnavailableError:
        return {
            "status": "error",
            "message": "Banco indisponível no momento.",
            "tell_client": (
                "Perfeito, recebi suas informações. Nosso sistema está instável agora, "
                "mas a equipe vai confirmar seu agendamento em breve. Obrigado pela paciência!"
            ),
        }
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as e:
        return _db_error(e)


def get_lead_status(tool_context: ToolContext) -> dict[str, Any]:
    """
    Consulta o que já está salvo no banco (estágio, nome, endereço, janela).
    Use quando o cliente perguntar se já agendou, o que falta, ou após um erro.
    """
    try:
        lead_id = _resolve_lead_id(tool_context)
        row = lead_repo.get_lead(lead_id)
        if not row:
            return {"status": "error", "message": "Lead não encontrado"}
        return {
            "status": "ok",
            "stage": row.get("stage"),
            "display_name": row.get("display_name"),
            "address": row.get("address"),
            "preferred_window": row.get("preferred_window"),
            "phone": row.get("phone"),
            "service_type": row.get("service_type"),
            "quoted_amount": str(row["quoted_amount"])
            if row.get("quoted_amount") is not None
            else None,
            "hint": "Responda ao cliente com base nesses campos; se stage ainda for quoted/awaiting_slot, explique o próximo passo.",
        }
    except DatabaseNotConfiguredError as e:
        return _db_error(e)
