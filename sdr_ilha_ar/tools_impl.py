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
from datetime import date as _date, datetime, timedelta, timezone
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
    "higienizacao": 180.0,
    "manutencao_preventiva": 180.0,
    "carga_gas_revisao": 180.0,
    "desinstalacao": 150.0,
    "visita_tecnica_gratis": 300.0,
    # H — limpeza de manutenção promocional 6m pós-conclusão.
    "limpeza_recall_6m": 280.0,
}

# Desconto máximo de negociação: cliente reclamou do preço pode receber -R$ 50.
# Só aplicar quando agente explicitamente decidir negociar (não automático).
NEGOTIATION_DISCOUNT_BRL = 50.0

# UZI Andaimes — parceiro oficial da Ilha Breeze em São Luís.
# Jeito B: valor é embutido no orçamento total da Ilha Breeze (cliente paga único).
# Acima do 4º andar a Ilha Breeze NÃO atende com andaime.
SCAFFOLD_PRICES_BY_FLOOR_BRL: dict[int, float] = {
    1: 120.0,
    2: 140.0,
    3: 170.0,
    4: 250.0,
}
MAX_SCAFFOLD_FLOOR = 4
# Escada 2 lances (1º andar, acesso externo simples).
LADDER_PRICE_BRL = 100.0
# Mão de obra quando precisa de equipamento de acesso (andaime/escada): R$ 300 + R$ 100 adicional.
LABOR_COMPLEX_BRL = 400.0
# Mão de obra padrão (fácil acesso).
LABOR_EASY_BRL = 300.0


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
    access_equipment: str | None = None,
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
        needs_scaffold_exterior: sim/nao — precisa equipamento de acesso externo.
        scaffold_floor: 1 a 4 — andar para aluguel do andaime (embutido no total).
        easy_access: sim = térreo, sacada, varanda (acesso fácil); nao = mais difícil.
        floor_level: legado; se scaffold_floor vazio e for 1–4, pode alinhar com andaime.
        tubing_complex: legado; se disser "sem"/"média", ajuda a inferir tubulação.
        access_equipment: "andaime" | "escada" | None. Quando sem varanda e sem janela viável,
            o agente deve inferir equipamento (escada só para 1º andar simples; andaime nos demais).

    Returns:
        amount_brl total Ilha Breeze (mão de obra + material + equipamento, pagamento único);
        scaffold_rental_client_brl mantido para referência interna do andaime embutido.

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
    if st in ("limpeza_recall_6m", "recall_6m", "manutencao_recall_6m"):
        # H — oferta promocional pós-recall 6 meses.
        return _finalize_ok_quote(
            tool_context,
            "limpeza_recall_6m",
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 280.0,
                "labor_brl": 280.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Limpeza de manutenção promocional (cliente retorno 6 meses): "
                    "R$ 280,00 — valor especial. Inclui higienização completa + "
                    "revisão geral. Técnicos credenciados com ART, 3 meses de "
                    "garantia. São Luís."
                ),
            },
        )
    if st in ("limpeza", "higienizacao"):
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
                "summary": (
                    "Limpeza/higienização: R$ 180. Técnico confirma na hora. São Luís."
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
                "amount_brl": 180.0,
                "labor_brl": 180.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Manutenção preventiva: R$ 180. Técnico confirma na hora. São Luís."
                ),
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
                "summary": (
                    "Carga de gás + revisão: R$ 180. Técnico confirma na hora. São Luís."
                ),
            },
        )
    if st in ("desinstalacao", "desinstalação", "retirar", "tirar_ar"):
        return _finalize_ok_quote(
            tool_context,
            "desinstalacao",
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 150.0,
                "labor_brl": 150.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Desinstalação: R$ 150. Se precisar de andaime/escada pelo andar, "
                    "soma o equipamento (UZI). Técnico confirma na hora. São Luís."
                ),
            },
        )

    if st in ("visita_tecnica_gratis", "visita_gratis", "defeito", "manutencao_corretiva"):
        # Regra dono: NUNCA mais cotação zerada. Toda visita tem estimativa mínima.
        # Faixa típica de diagnóstico + serviço corretivo: R$ 200 a R$ 400.
        return _finalize_ok_quote(
            tool_context,
            st,
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 300.0,
                "labor_brl": 300.0,
                "materials_tubing_brl": 0.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Diagnóstico + serviço corretivo: estimado entre R$ 200 e R$ 400 "
                    "(média R$ 300). Técnico confirma na hora depois de avaliar. "
                    "Casos comuns: não gela, vazamento, cassete/piso-teto, ruído. "
                    "São Luís."
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
        # Regra dono: sempre dar estimativa. Assumir fácil acesso como baseline.
        return _finalize_ok_quote(
            tool_context,
            "instalacao",
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 500.0,
                "labor_brl": 300.0,
                "materials_tubing_brl": 200.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Instalação: estimado R$ 300 mão de obra + R$ 200 material "
                    "(total ~R$ 500). Se precisar quebrar parede/teto ou mexer "
                    "em fiação, pode subir — técnico confirma na hora. São Luís."
                ),
            },
        )

    # Regra crítica: se envolver quebra de parede/teto ou fiação, estimativa majorada.
    if wall_or_wiring is True:
        return _finalize_ok_quote(
            tool_context,
            "instalacao",
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 700.0,
                "labor_brl": 500.0,
                "materials_tubing_brl": 200.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Instalação com quebra de parede/teto ou fiação: estimado "
                    "entre R$ 600 e R$ 900 (média R$ 700). Mão de obra majorada "
                    "pela complexidade. Técnico confirma o valor final na hora."
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
        # Regra dono: sempre dar estimativa. Assumir fácil acesso como baseline.
        return _finalize_ok_quote(
            tool_context,
            "instalacao",
            {
                "status": "ok",
                "currency": "BRL",
                "amount_brl": 500.0,
                "labor_brl": 300.0,
                "materials_tubing_brl": 200.0,
                "scaffold_rental_client_brl": None,
                "summary": (
                    "Instalação: estimado R$ 300 mão de obra + R$ 200 material "
                    "(total ~R$ 500). Se for acesso difícil (sem varanda/janela), "
                    "vai adicionar andaime/escada — técnico confirma na hora. São Luís."
                ),
            },
        )

    if needs_scaf is True or easy is False:
        # Jeito B: cota na hora com equipamento embutido no total Ilha Breeze.
        equip_raw = (access_equipment or "").strip().lower()
        # Inferência do andar a partir de scaffold_floor > floor_level.
        inferred_floor: int | None = None
        if isinstance(scaffold_floor, int) and scaffold_floor > 0:
            inferred_floor = scaffold_floor
        elif isinstance(floor_level, int) and floor_level > 0:
            inferred_floor = floor_level

        # Inferência de equipamento se o agente não passou:
        # 1º andar simples → escada; 2º-4º → andaime; acima disso, visita.
        if not equip_raw and inferred_floor is not None:
            if inferred_floor == 1:
                equip_raw = "escada"
            elif 2 <= inferred_floor <= MAX_SCAFFOLD_FLOOR:
                equip_raw = "andaime"

        # Acima do 4º → Ilha Breeze não atende com andaime da UZI, mas dá estimativa.
        if inferred_floor is not None and inferred_floor > MAX_SCAFFOLD_FLOOR:
            return _finalize_ok_quote(
                tool_context,
                "instalacao",
                {
                    "status": "ok",
                    "currency": "BRL",
                    "amount_brl": 600.0,
                    "labor_brl": 400.0,
                    "materials_tubing_brl": 200.0,
                    "scaffold_rental_client_brl": None,
                    "summary": (
                        f"Acima do {MAX_SCAFFOLD_FLOOR}º andar a UZI não tem andaime "
                        "padrão. Estimativa inicial: R$ 600 (mão de obra + material), "
                        "sem contar equipamento especial (andaime alto/cesto). "
                        "Um humano da equipe vai confirmar cotação do equipamento."
                    ),
                },
            )

        # Escada → só 1º andar.
        if equip_raw == "escada":
            if inferred_floor is not None and inferred_floor != 1:
                # escada só serve pro 1º; se informaram andar diferente, corrige pra andaime.
                equip_raw = "andaime"
            else:
                equipment_cost = LADDER_PRICE_BRL
                equipment_label = "escada (2 lances)"

        if equip_raw == "andaime":
            if inferred_floor is None:
                # Sem andar informado → dá estimativa média da tabela e pede o andar.
                return _finalize_ok_quote(
                    tool_context,
                    "instalacao",
                    {
                        "status": "ok",
                        "currency": "BRL",
                        "amount_brl": 570.0,
                        "labor_brl": 400.0,
                        "materials_tubing_brl": 0.0,
                        "scaffold_rental_client_brl": 170.0,
                        "summary": (
                            "Instalação com andaime: estimado entre R$ 520 (1º andar) e "
                            "R$ 650 (4º andar). Média R$ 570 = mão de obra R$ 400 + "
                            "andaime UZI ~R$ 170 (3º andar). Pergunte o andar pro cliente "
                            "pra cotar exato. ⚠️ Agendamento com andaime: mínimo 48h."
                        ),
                    },
                )
            equipment_cost = SCAFFOLD_PRICES_BY_FLOOR_BRL[inferred_floor]
            equipment_label = f"andaime {inferred_floor}º andar"
        elif equip_raw == "escada":
            # Reafirmação do caso escada (para quando caiu no elif inicial).
            equipment_cost = LADDER_PRICE_BRL
            equipment_label = "escada (2 lances)"
        else:
            # Sem equipamento determinado → estimativa média assumindo andaime 2º andar.
            return _finalize_ok_quote(
                tool_context,
                "instalacao",
                {
                    "status": "ok",
                    "currency": "BRL",
                    "amount_brl": 540.0,
                    "labor_brl": 400.0,
                    "materials_tubing_brl": 0.0,
                    "scaffold_rental_client_brl": 140.0,
                    "summary": (
                        "Instalação em acesso difícil: estimado R$ 540 "
                        "(mão de obra R$ 400 + andaime UZI ~R$ 140 para 2º andar). "
                        "Pergunte o andar pro cliente pra cotar exato. "
                        "⚠️ Agendamento com andaime: mínimo 48h."
                    ),
                },
            )

        labor = LABOR_COMPLEX_BRL
        tubing_extra = 0.0
        if own is False:
            tubing_extra = 200.0
        total = labor + tubing_extra + equipment_cost

        tubing_part = (
            " + tubulação R$ 200" if own is False else ""
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
                "scaffold_rental_client_brl": round(equipment_cost, 2),
                "summary": (
                    f"Instalação com {equipment_label}: R$ {total:.0f} total "
                    f"(mão de obra R$ {labor:.0f}{tubing_part} + "
                    f"{equipment_label} R$ {equipment_cost:.0f}). "
                    "⚠️ Agendamento com andaime/escada: mínimo 48h de antecedência "
                    "pra garantir disponibilidade do equipamento."
                ),
            },
        )

    labor = LABOR_EASY_BRL

    tubing_extra = 0.0
    if own is False:
        tubing_extra = 200.0

    total = labor + tubing_extra
    parts = [
        f"Instalação padrão (acesso fácil): R$ {labor:.0f} mão de obra."
    ]
    if own is False:
        parts.append(
            f"Sem tubulação: + R$ 200 material. Total R$ {total:.0f}."
        )
    elif own is True:
        parts.append("Cliente já tem tubulação: só a mão de obra.")
    else:
        parts.append(
            "Se tiver tubulação, só mão de obra. Sem tubulação, + R$ 200."
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

        # Cadência de follow-ups automáticos após orçamento enviado.
        # Pedido do Kauan (v2): 45min, 1h, 5h, 1d, 3d (+cupom R$50 pra 3d).
        # Os templates são distinguidos via `template` no payload; o prompt do agente
        # reconhece cada um via prefixo [FOLLOWUP:<tag>] e ajusta tom/oferta.
        now_utc = datetime.now(timezone.utc)
        followup_schedule = [
            (timedelta(minutes=45), "followup_45min", "45min"),
            (timedelta(hours=1),    "followup_1h",    "1h"),
            (timedelta(hours=5),    "followup_5h",    "5h"),
            (timedelta(days=1),     "followup_1d",    "1d"),
            (timedelta(days=3),     "followup_3d",    "3d_coupon_50"),
        ]
        scheduled: list[str] = []
        for delta, template, tag in followup_schedule:
            run_at = now_utc + delta
            try:
                lead_repo.enqueue_job(
                    lead_id,
                    "send_followup",
                    run_at,
                    {"template": template, "followup_tag": tag},
                    f"{template}_{lead_id}",
                )
                scheduled.append(f"{tag}@{run_at.isoformat()}")
            except Exception:
                # Não falha o mark_quote_sent se 1 followup específico não enfileirar
                # (ex: colisão de idempotência em retry). Log e segue.
                logger.exception("Falha ao enfileirar followup %s lead=%s", template, lead_id)

        run_at = now_utc + timedelta(minutes=45)  # compat com retorno legado
        lead_repo.append_message(
            lead_id,
            "tool",
            f"mark_quote_sent + cadencia followup ({len(scheduled)} jobs)",
        )
        _label_lead_chat(lead_id, "orcado")
        return {
            "status": "ok",
            "followup_scheduled_at": run_at.isoformat(),
            "followup_cadence": scheduled,
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


# =============================================================================
# F2+A4: Tools de agendamento com slots fixos (check_availability / book_slot)
# =============================================================================
# DESIGN DECISION: essas tools são a interface do LLM com a engine de slots
# já implementada em repository.py. Elas NÃO tomam decisão de negócio própria —
# só traduzem data humana (DD/MM/AAAA) -> datetime.date, chamam o repository,
# e montam `tell_client` em PT-BR pronto pro modelo ler ao cliente.

def _parse_ddmmyyyy(value: str) -> _date | None:
    """Converte 'DD/MM/AAAA' -> date. Retorna None se inválido."""
    raw = (value or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", raw)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return _date(year, month, day)
    except ValueError:
        return None


def _format_free_slots_pt(slots: dict[str, bool]) -> str:
    """Monta lista humana de slots livres, ex.: '8h-10h, 14h-16h e 16h-18h'."""
    labels = [
        lead_repo.SLOT_LABELS[s]
        for s in lead_repo.SLOTS_ORDER
        if slots.get(s)
    ]
    # Troca '08h-10h' por '8h-10h' para ficar natural em PT-BR.
    labels = [lbl.lstrip("0") if lbl.startswith("0") else lbl for lbl in labels]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} e {labels[1]}"
    return ", ".join(labels[:-1]) + f" e {labels[-1]}"


def check_availability(date: str, tool_context: ToolContext) -> dict[str, Any]:
    """
    Consulta disponibilidade de slots (manhã/tarde) numa data específica.

    Args:
        date: data no formato DD/MM/AAAA (ex.: "05/05/2026").

    Returns:
        status: ok/error;
        date: data solicitada (DD/MM/AAAA);
        slots: dict com cada slot -> "livre" ou "ocupado";
        slot_labels: dict com rótulo humano de cada slot (ex.: "08h-10h");
        tell_client: string pronta em PT-BR para o LLM ler ao cliente.
    """
    # DESIGN DECISION: `tool_context` é aceito (padrão das outras tools) mas não
    # é estritamente necessário aqui — checagem de slot é global por data, não
    # por lead. Mantido para uniformidade do registry ADK.
    del tool_context  # não usado; apenas parte da assinatura ADK.
    parsed = _parse_ddmmyyyy(date)
    if parsed is None:
        return {
            "status": "error",
            "message": f"Data inválida: {date!r}. Use formato DD/MM/AAAA.",
            "tell_client": (
                "Me confirma a data desejada no formato dia/mês/ano (ex.: 05/05/2026), "
                "por favor?"
            ),
        }
    try:
        slots_bool = lead_repo.check_slot_availability(parsed)
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as e:
        return _db_error(e)

    slots_str = {
        slot: ("livre" if free else "ocupado")
        for slot, free in slots_bool.items()
    }
    slot_labels = dict(lead_repo.SLOT_LABELS)
    date_human = parsed.strftime("%d/%m/%Y")
    free_any = any(slots_bool.values())
    if not free_any:
        tell_client = (
            f"No dia {date_human} já estou com 4 atendimentos marcados. "
            "Tem outro dia bom pra ti?"
        )
    else:
        free_list = _format_free_slots_pt(slots_bool)
        tell_client = (
            f"No dia {date_human} tenho livres: {free_list}. Qual prefere?"
        )
    return {
        "status": "ok",
        "date": date_human,
        "slots": slots_str,
        "slot_labels": slot_labels,
        "tell_client": tell_client,
    }


def book_slot(
    date: str,
    slot: str,
    tool_context: ToolContext,
    notes: str = "",
) -> dict[str, Any]:
    """
    Reserva um slot específico (DD/MM/AAAA + slot) para o lead atual.

    Args:
        date: data no formato DD/MM/AAAA.
        slot: um de morning_early | morning_late | afternoon_early | afternoon_late.
        notes: observações livres (opcional).

    Returns:
        status: ok/error;
        appointment_id: uuid do agendamento criado (quando ok);
        date, slot, slot_label: eco dos dados confirmados;
        tell_client: mensagem pronta em PT-BR;
        requires_team_assignment: sempre True (equipe é atribuída depois por humano).
    """
    parsed = _parse_ddmmyyyy(date)
    if parsed is None:
        return {
            "status": "error",
            "message": f"Data inválida: {date!r}. Use formato DD/MM/AAAA.",
            "tell_client": (
                "Me confirma a data no formato dia/mês/ano (ex.: 05/05/2026), por favor?"
            ),
        }
    slot_key = (slot or "").strip()
    if slot_key not in lead_repo.SLOT_LABELS:
        return {
            "status": "error",
            "message": (
                f"Slot inválido: {slot!r}. Use um de "
                f"{list(lead_repo.SLOT_LABELS)}."
            ),
            "tell_client": (
                "Qual horário prefere: 8h-10h, 10h-12h, 14h-16h ou 16h-18h?"
            ),
        }
    try:
        lead_id = _resolve_lead_id(tool_context)
        appt = lead_repo.create_slot_appointment(
            lead_id,
            appointment_date=parsed,
            slot=slot_key,
            notes=notes or "",
        )
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as e:
        return _db_error(e)
    except ValueError as e:
        # Slot ocupado ou dia cheio — mensagem amigável ao cliente.
        msg = str(e)
        low = msg.lower()
        date_human = parsed.strftime("%d/%m/%Y")
        if "já está com 4" in msg or "4 atendimentos" in low:
            tell = (
                f"Poxa, o dia {date_human} já fechou com 4 atendimentos. "
                "Tem outro dia bom pra ti?"
            )
        else:
            label = lead_repo.SLOT_LABELS.get(slot_key, slot_key)
            label_human = label.lstrip("0") if label.startswith("0") else label
            tell = (
                f"O horário das {label_human} do dia {date_human} acabou de ser "
                "reservado. Posso ver outro horário nesse mesmo dia?"
            )
        return {"status": "error", "message": msg, "tell_client": tell}
    except LookupError as e:
        return {"status": "error", "message": str(e)}

    # Best effort: avança o funil e etiqueta o chat (não bloqueia retorno).
    try:
        _advance_lead_to_scheduled(lead_id)
    except Exception:
        logger.exception("Falha ao avançar funil após book_slot lead=%s", lead_id)
    try:
        lead_repo.append_message(
            lead_id,
            "tool",
            f"book_slot {parsed.isoformat()} slot={slot_key}",
        )
    except Exception:
        logger.exception("Falha ao registrar mensagem de book_slot lead=%s", lead_id)
    _label_lead_chat(lead_id, "agendado")

    slot_label = lead_repo.SLOT_LABELS[slot_key]
    slot_label_human = slot_label.lstrip("0") if slot_label.startswith("0") else slot_label
    date_human = parsed.strftime("%d/%m/%Y")
    date_short = parsed.strftime("%d/%m")
    tell_client = (
        f"Prontinho! Agendamento marcado pra {date_short} das {slot_label_human}. "
        "Nossa equipe confirma qual técnico vai atender até amanhã. "
        "Qualquer coisa te aviso por aqui!"
    )
    return {
        "status": "ok",
        "appointment_id": str(appt.get("id")),
        "date": date_human,
        "slot": slot_key,
        "slot_label": slot_label,
        "tell_client": tell_client,
        "requires_team_assignment": True,
    }
