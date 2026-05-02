# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Acesso síncrono ao PostgreSQL para leads, mensagens, jobs e eventos."""

from __future__ import annotations

import logging
import json
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import time
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from sdr_ilha_ar.config import settings
from sdr_ilha_ar import state_machine

logger = logging.getLogger(__name__)
HANDOFF_KEY = "_handoff"

ALLOWED_LEAD_FIELDS = frozenset(
    {
        "display_name",
        "address",
        "phone",
        "preferred_window",
        "service_type",
        "btus",
        "floor_level",
        "tubing_complex",
        "quoted_amount",
        "quote_notes",
        "equipe_responsavel",
        "latitude",
        "longitude",
    }
)


class DatabaseNotConfiguredError(RuntimeError):
    """DATABASE_URL ausente."""


class DatabaseUnavailableError(RuntimeError):
    """Postgres indisponível após tentativas de conexão."""


def _require_url() -> str:
    if not settings.database_url:
        raise DatabaseNotConfiguredError(
            "Defina DATABASE_URL (Postgres) para usar as tools de persistência."
        )
    return settings.database_url


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    url = _require_url()
    retries = max(0, settings.db_connect_retries)
    backoff = max(0.0, settings.db_retry_backoff_seconds)
    timeout = max(1, settings.db_connect_timeout_seconds)
    conn: psycopg.Connection | None = None
    last_error: psycopg.OperationalError | None = None

    for attempt in range(retries + 1):
        try:
            conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=timeout)
            break
        except psycopg.OperationalError as err:
            last_error = err
            if attempt >= retries:
                break
            time.sleep(backoff * (attempt + 1))

    if conn is None:
        assert last_error is not None
        raise DatabaseUnavailableError(
            "Banco indisponível no momento. Tente novamente em instantes."
        ) from last_error

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _jsonify_value(v: Any) -> Any:
    """Garante tipos compatíveis com JSON (UUID, datas, Numeric, JSONB aninhado)."""
    if v is None:
        return None
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonify_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonify_value(x) for x in v]
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).decode("utf-8", errors="replace")
    return v


def _jsonify_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _jsonify_value(val) for k, val in r.items()} for r in rows]


def bootstrap_db_schema() -> None:
    """Aplica ``db/schema.sql`` com ``psql`` (idempotente). Requer cliente no PATH."""
    if not shutil.which("psql"):
        logger.warning("psql não encontrado; aplique db/schema.sql manualmente no Postgres")
        return
    try:
        url = _require_url()
    except DatabaseNotConfiguredError:
        logger.warning("DATABASE_URL ausente; bootstrap do schema ignorado")
        return

    path = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
    if not path.is_file():
        logger.warning("db/schema.sql não encontrado em %s", path)
        return

    proc = subprocess.run(
        ["psql", url, "-f", str(path)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        logger.error(
            "Falha ao aplicar schema (exit %s). stderr=%s stdout=%s",
            proc.returncode,
            proc.stderr,
            proc.stdout,
        )
        return
    logger.info("db/schema.sql aplicado (bootstrap)")


def reconcile_whatsapp_instance_channel(
    external_user_id: str,
    namespaced_channel: str,
) -> None:
    """
    Evita dois leads para o mesmo telefone (legado `whatsapp` vs `whatsapp:<instancia>`).
    Quando existir o par, move filhas para o lead canônico e remove o duplicado.
    """
    ch = str(namespaced_channel or "").strip()
    if not ch.startswith("whatsapp:"):
        return
    uid = str(external_user_id or "").strip()
    if not uid:
        return
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM leads
                WHERE external_user_id = %s AND external_channel = %s
                """,
                (uid, ch),
            )
            canon = cur.fetchone()
            cur.execute(
                """
                SELECT id FROM leads
                WHERE external_user_id = %s AND external_channel = 'whatsapp'
                """,
                (uid,),
            )
            legacy = cur.fetchone()
            if not legacy:
                return
            legacy_id = str(legacy["id"])
            if canon:
                canon_id = str(canon["id"])
                if canon_id == legacy_id:
                    return
                cur.execute(
                    "UPDATE messages SET lead_id = %s WHERE lead_id = %s",
                    (canon_id, legacy_id),
                )
                cur.execute(
                    "UPDATE appointments SET lead_id = %s WHERE lead_id = %s",
                    (canon_id, legacy_id),
                )
                cur.execute(
                    "UPDATE automation_jobs SET lead_id = %s WHERE lead_id = %s",
                    (canon_id, legacy_id),
                )
                cur.execute(
                    "UPDATE outbox_events SET lead_id = %s WHERE lead_id = %s",
                    (canon_id, legacy_id),
                )
                cur.execute(
                    "UPDATE finance_entries SET lead_id = %s WHERE lead_id = %s",
                    (canon_id, legacy_id),
                )
                cur.execute("DELETE FROM leads WHERE id = %s", (legacy_id,))
            else:
                cur.execute(
                    """
                    UPDATE leads
                    SET external_channel = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (ch, legacy_id),
                )


def ensure_lead(
    external_channel: str,
    external_user_id: str,
    *,
    touch_inbound: bool = True,
) -> uuid.UUID:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO leads (
                    external_channel,
                    external_user_id,
                    last_inbound_at,
                    equipe_responsavel
                )
                VALUES (%s, %s, CASE WHEN %s THEN now() ELSE NULL END, %s)
                ON CONFLICT (external_channel, external_user_id)
                DO UPDATE SET
                    last_inbound_at = CASE
                        WHEN %s THEN now()
                        ELSE leads.last_inbound_at
                    END,
                    equipe_responsavel = COALESCE(leads.equipe_responsavel, EXCLUDED.equipe_responsavel)
                RETURNING id
                """,
                (
                    external_channel,
                    external_user_id,
                    touch_inbound,
                    settings.equipe_responsavel,
                    touch_inbound,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            return row["id"]


def get_lead(lead_id: uuid.UUID) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE id = %s", (str(lead_id),))
            return cur.fetchone()


def get_lead_by_external(external_channel: str, external_user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM leads
                WHERE external_channel = %s AND external_user_id = %s
                """,
                (external_channel, external_user_id),
            )
            return cur.fetchone()


def _parse_quote_notes_meta(raw: Any) -> dict[str, Any]:
    txt = str(raw or "").strip()
    if not txt:
        return {}
    try:
        parsed = json.loads(txt)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {"_legacy_quote_notes": txt}


def get_handoff_state(lead_id: uuid.UUID) -> dict[str, Any]:
    lead = get_lead(lead_id) or {}
    meta = _parse_quote_notes_meta(lead.get("quote_notes"))
    handoff = meta.get(HANDOFF_KEY)
    return handoff if isinstance(handoff, dict) else {}


def set_handoff_state(
    lead_id: uuid.UUID,
    *,
    active: bool,
    activated_by: str = "human",
    reason: str = "",
) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT quote_notes FROM leads WHERE id = %s FOR UPDATE",
                (str(lead_id),),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError("Lead não encontrado")
            meta = _parse_quote_notes_meta(row.get("quote_notes"))
            if active:
                meta[HANDOFF_KEY] = {
                    "active": True,
                    "activated_at": datetime.now(timezone.utc).isoformat(),
                    "activated_by": activated_by,
                    "reason": reason,
                }
            else:
                meta[HANDOFF_KEY] = {
                    "active": False,
                    "reactivated_at": datetime.now(timezone.utc).isoformat(),
                    "reactivated_by": activated_by,
                    "reason": reason,
                }
            cur.execute(
                """
                UPDATE leads
                SET quote_notes = %s, updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (json.dumps(meta, ensure_ascii=False), str(lead_id)),
            )
            out = cur.fetchone()
            if not out:
                raise LookupError("Lead não encontrado")
            return dict(out)


def is_bot_paused(lead_id: uuid.UUID) -> bool:
    lead = get_lead(lead_id) or {}
    return bool(lead.get("bot_paused"))


def pause_bot_for_lead(
    lead_id: uuid.UUID,
    *,
    reason: str = "",
    by: str = "system",
) -> dict[str, Any]:
    """Wrapper de alto nível: pausa o bot pra este lead.

    Contrato amigável para os endpoints REST e detecção automática.
    Internamente delega para `set_bot_paused` (que lida com os timestamps).
    """
    return set_bot_paused(lead_id, paused=True, by=by, reason=reason)


def resume_bot_for_lead(
    lead_id: uuid.UUID,
    *,
    by: str = "system",
    reason: str = "manual_resume",
) -> dict[str, Any]:
    """Wrapper de alto nível: retoma o bot (espelho de `pause_bot_for_lead`)."""
    return set_bot_paused(lead_id, paused=False, by=by, reason=reason)


def set_bot_paused(
    lead_id: uuid.UUID,
    *,
    paused: bool,
    by: str,
    reason: str = "",
) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE leads
                SET
                    bot_paused = %s,
                    bot_paused_at = CASE WHEN %s THEN now() ELSE bot_paused_at END,
                    bot_paused_by = CASE WHEN %s THEN %s ELSE bot_paused_by END,
                    bot_paused_reason = CASE WHEN %s THEN %s ELSE bot_paused_reason END,
                    bot_reactivated_at = CASE WHEN %s THEN bot_reactivated_at ELSE now() END,
                    bot_reactivated_by = CASE WHEN %s THEN bot_reactivated_by ELSE %s END,
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (
                    paused,
                    paused,
                    paused,
                    by,
                    paused,
                    reason,
                    paused,
                    paused,
                    by,
                    str(lead_id),
                ),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError("Lead não encontrado")
            return dict(row)


def count_messages_by_roles(lead_id: uuid.UUID, roles: tuple[str, ...]) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)::int AS total
                FROM messages
                WHERE lead_id = %s AND role = ANY(%s)
                """,
                (str(lead_id), list(roles)),
            )
            row = cur.fetchone() or {}
            return int(row.get("total") or 0)


def confirm_latest_appointment_for_lead(lead_id: uuid.UUID) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE appointments
                SET status = 'confirmed', updated_at = now()
                WHERE id = (
                    SELECT id FROM appointments
                    WHERE lead_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                AND status = 'proposed'
                RETURNING *
                """,
                (str(lead_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def promote_lead_stage_on_handoff(lead_id: uuid.UUID) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE leads
                SET stage = 'scheduled', updated_at = now()
                WHERE id = %s
                  AND stage IN ('awaiting_slot', 'quoted')
                RETURNING *
                """,
                (str(lead_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def save_lead_field(lead_id: uuid.UUID, field_name: str, value: Any) -> dict[str, Any]:
    if field_name not in ALLOWED_LEAD_FIELDS:
        raise ValueError(f"Campo não permitido: {field_name}")

    col = field_name
    if field_name == "quoted_amount":
        if value is None:
            py_val: Any = None
        else:
            py_val = Decimal(str(value))
    elif field_name in ("latitude", "longitude"):
        py_val = Decimal(str(value)) if value is not None and value != "" else None
    elif field_name == "btus" or field_name == "floor_level":
        py_val = int(value) if value is not None else None
    else:
        py_val = str(value) if value is not None else None

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE leads SET {col} = %s, updated_at = now() WHERE id = %s RETURNING *",
                (py_val, str(lead_id)),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError("Lead não encontrado")
            return dict(row)


def save_lead_location(
    lead_id: uuid.UUID,
    latitude: float | str | Decimal,
    longitude: float | str | Decimal,
) -> dict[str, Any]:
    """
    FIX-MAPS: persiste latitude + longitude de uma vez como source of truth
    da localização enviada pelo cliente via pin do WhatsApp (message.type=location).
    """
    lat = Decimal(str(latitude))
    lng = Decimal(str(longitude))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE leads
                SET latitude = %s, longitude = %s, updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (lat, lng, str(lead_id)),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError("Lead não encontrado")
            return dict(row)


def set_lead_stage(lead_id: uuid.UUID, new_stage: str) -> dict[str, Any]:
    if new_stage not in state_machine.STAGES:
        raise ValueError(f"Estágio desconhecido: {new_stage}")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stage FROM leads WHERE id = %s FOR UPDATE", (str(lead_id),))
            row = cur.fetchone()
            if not row:
                raise LookupError("Lead não encontrado")
            current = row["stage"]
            state_machine.assert_transition(current, new_stage)
            cur.execute(
                """
                UPDATE leads SET stage = %s, updated_at = now() WHERE id = %s
                RETURNING *
                """,
                (new_stage, str(lead_id)),
            )
            out = cur.fetchone()
            assert out is not None
            # G: mantém histórico — fecha linha aberta anterior e insere nova.
            # Só registra se realmente mudou de estágio.
            if current != new_stage:
                cur.execute(
                    """
                    UPDATE lead_stage_history
                    SET exited_at = now()
                    WHERE lead_id = %s AND exited_at IS NULL
                    """,
                    (str(lead_id),),
                )
                cur.execute(
                    """
                    INSERT INTO lead_stage_history (lead_id, stage, entered_at)
                    VALUES (%s, %s, now())
                    """,
                    (str(lead_id), new_stage),
                )
            return dict(out)


def get_current_stage_duration(lead_id: uuid.UUID) -> timedelta | None:
    """Retorna timedelta desde o último entered_at do estágio atual (linha aberta).

    Se não houver linha aberta em lead_stage_history, faz fallback para
    max(entered_at) independente de exited_at. Se nada existir, retorna None.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entered_at FROM lead_stage_history
                WHERE lead_id = %s AND exited_at IS NULL
                ORDER BY entered_at DESC LIMIT 1
                """,
                (str(lead_id),),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    """
                    SELECT entered_at FROM lead_stage_history
                    WHERE lead_id = %s
                    ORDER BY entered_at DESC LIMIT 1
                    """,
                    (str(lead_id),),
                )
                row = cur.fetchone()
            if not row:
                return None
            entered_at = row["entered_at"]
            now = datetime.now(timezone.utc)
            if entered_at.tzinfo is None:
                entered_at = entered_at.replace(tzinfo=timezone.utc)
            return now - entered_at


def get_stage_history(lead_id: uuid.UUID) -> list[dict[str, Any]]:
    """Retorna histórico de estágios do lead (ordem cronológica asc)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, lead_id, stage, entered_at, exited_at
                FROM lead_stage_history
                WHERE lead_id = %s
                ORDER BY entered_at ASC
                """,
                (str(lead_id),),
            )
            return [dict(r) for r in cur.fetchall()]


def get_stage_duration_for(lead_id: uuid.UUID, stage: str) -> timedelta | None:
    """Tempo desde o último entered_at do `stage` informado (aberto ou fechado).

    Usado pelo worker de follow-up: calcula há quanto tempo o lead está (ou esteve)
    em determinado estágio, ex: 'quoted'. Se houver linha aberta daquele stage,
    usa-a; senão, usa a mais recente fechada.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entered_at FROM lead_stage_history
                WHERE lead_id = %s AND stage = %s
                ORDER BY entered_at DESC LIMIT 1
                """,
                (str(lead_id), stage),
            )
            row = cur.fetchone()
            if not row:
                return None
            entered_at = row["entered_at"]
            now = datetime.now(timezone.utc)
            if entered_at.tzinfo is None:
                entered_at = entered_at.replace(tzinfo=timezone.utc)
            return now - entered_at


def mark_quote_sent(lead_id: uuid.UUID) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE leads
                SET quote_sent_at = now(), updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (str(lead_id),),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError("Lead não encontrado")
            return dict(row)


def append_message(
    lead_id: uuid.UUID, role: str, body: str, metadata: dict[str, Any] | None = None
) -> uuid.UUID:
    meta = metadata or {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages (lead_id, role, body, metadata)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (str(lead_id), role, body, Json(meta)),
            )
            row = cur.fetchone()
            assert row is not None
            return row["id"]


def enqueue_job(
    lead_id: uuid.UUID,
    job_type: str,
    run_at: datetime,
    payload: dict[str, Any],
    idempotency_key: str | None,
) -> uuid.UUID | None:
    """Retorna None se idempotency_key duplicada (insert ignorado)."""
    with connect() as conn:
        with conn.cursor() as cur:
            if idempotency_key:
                cur.execute(
                    """
                    INSERT INTO automation_jobs (lead_id, job_type, run_at, payload, idempotency_key)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        str(lead_id),
                        job_type,
                        run_at,
                        Json(payload),
                        idempotency_key,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO automation_jobs (lead_id, job_type, run_at, payload)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (str(lead_id), job_type, run_at, Json(payload)),
                )
            row = cur.fetchone()
            return row["id"] if row else None


def insert_outbox_event(
    lead_id: uuid.UUID | None, event_type: str, payload: dict[str, Any]
) -> uuid.UUID:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO outbox_events (lead_id, event_type, payload)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (str(lead_id) if lead_id else None, event_type, Json(payload)),
            )
            row = cur.fetchone()
            assert row is not None
            return row["id"]


def list_pending_jobs_due(limit: int = 20) -> list[dict[str, Any]]:
    """Lista jobs pendentes com run_at <= agora. Rode um único worker por fila (sem claim distribuído)."""
    now = datetime.now(timezone.utc)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM automation_jobs
                WHERE status = 'pending' AND run_at <= %s
                ORDER BY run_at
                LIMIT %s
                """,
                (now, limit),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def complete_job(job_id: uuid.UUID) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE automation_jobs
                SET status = 'done', updated_at = now(), last_error = NULL
                WHERE id = %s
                """,
                (str(job_id),),
            )


def fail_job(job_id: uuid.UUID, err: str) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE automation_jobs
                SET status = 'failed', last_error = %s, updated_at = now()
                WHERE id = %s
                """,
                (err[:4000], str(job_id)),
            )


def requeue_job(job_id: uuid.UUID, run_at: datetime) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE automation_jobs
                SET status = 'pending', run_at = %s, updated_at = now()
                WHERE id = %s
                """,
                (run_at, str(job_id)),
            )


def cancel_pending_jobs_for_lead(lead_id: uuid.UUID, *, job_type: str) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE automation_jobs
                SET status = 'cancelled', updated_at = now(), last_error = NULL
                WHERE lead_id = %s AND job_type = %s AND status = 'pending'
                """,
                (str(lead_id), job_type),
            )
            return cur.rowcount


def create_appointment(
    lead_id: uuid.UUID, window_label: str, status: str = "proposed"
) -> uuid.UUID:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO appointments (lead_id, window_label, status)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (str(lead_id), window_label, status),
            )
            row = cur.fetchone()
            assert row is not None
            return row["id"]


# =============================================================================
# F2+A4: Engine de agendamento com slots fixos
# =============================================================================
# Slots fixos do dia e rótulos usados no texto ao cliente / resumo interno.
SLOT_LABELS: dict[str, str] = {
    "morning_early": "08h-10h",
    "morning_late": "10h-12h",
    "afternoon_early": "14h-16h",
    "afternoon_late": "16h-18h",
}
SLOTS_ORDER: tuple[str, ...] = (
    "morning_early",
    "morning_late",
    "afternoon_early",
    "afternoon_late",
)
# Limite diário por equipe (4 slots = 1 equipe saturada).
# DESIGN DECISION: por enquanto tratamos "equipe única" e bloqueamos o slot se
# já houver 1 appointment ativo nele. Quando adicionarmos mais equipes, bastará
# contar por (date, slot, team_id).
APPOINTMENT_ACTIVE_STATUSES: tuple[str, ...] = (
    "pending_team_assignment",
    "proposed",
    "confirmed",
    "realloc",
)


def list_appointments_for_date(appointment_date: date) -> list[dict[str, Any]]:
    """Retorna appointments ATIVOS (não cancelados/concluídos) em uma data."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, lead_id, scheduled_date, slot, team_id, status,
                       window_label, notes, created_at
                FROM appointments
                WHERE scheduled_date = %s
                  AND status = ANY(%s)
                ORDER BY slot ASC, created_at ASC
                """,
                (appointment_date, list(APPOINTMENT_ACTIVE_STATUSES)),
            )
            return _jsonify_rows([dict(r) for r in cur.fetchall()])


def check_slot_availability(appointment_date: date) -> dict[str, bool]:
    """Retorna dict {slot_name: livre?} para a data informada."""
    taken = {row["slot"] for row in list_appointments_for_date(appointment_date) if row.get("slot")}
    return {slot: (slot not in taken) for slot in SLOTS_ORDER}


def create_slot_appointment(
    lead_id: uuid.UUID,
    *,
    appointment_date: date,
    slot: str,
    notes: str = "",
    window_label: str = "",
) -> dict[str, Any]:
    """
    Cria appointment estruturado (slot + date) com status pending_team_assignment.
    Valida limite 4/dia (DESIGN DECISION: 1 equipe = 4 slots únicos).
    Retorna dict com o appointment criado.
    """
    if slot not in SLOT_LABELS:
        raise ValueError(f"Slot inválido: {slot}. Use um de {list(SLOT_LABELS)}.")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM appointments
                WHERE scheduled_date = %s
                  AND slot = %s
                  AND status = ANY(%s)
                """,
                (appointment_date, slot, list(APPOINTMENT_ACTIVE_STATUSES)),
            )
            row = cur.fetchone() or {}
            if int(row.get("total") or 0) >= 1:
                raise ValueError(
                    f"Slot {slot} em {appointment_date.isoformat()} já está ocupado."
                )
            # Limite de 4/dia (redundância defensiva caso introduzamos >1 slot por período no futuro).
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM appointments
                WHERE scheduled_date = %s
                  AND status = ANY(%s)
                """,
                (appointment_date, list(APPOINTMENT_ACTIVE_STATUSES)),
            )
            row_day = cur.fetchone() or {}
            if int(row_day.get("total") or 0) >= 4:
                raise ValueError(
                    f"Dia {appointment_date.isoformat()} já está com 4 atendimentos."
                )
            label = window_label or f"{appointment_date.strftime('%d/%m/%Y')} {SLOT_LABELS[slot]}"
            cur.execute(
                """
                INSERT INTO appointments (
                    lead_id, window_label, status, scheduled_date, slot, notes
                )
                VALUES (%s, %s, 'pending_team_assignment', %s, %s, %s)
                RETURNING id, lead_id, scheduled_date, slot, team_id, status,
                          window_label, notes, created_at
                """,
                (str(lead_id), label, appointment_date, slot, notes or None),
            )
            row_out = cur.fetchone()
            assert row_out is not None
            return {k: _jsonify_value(v) for k, v in row_out.items()}


def get_appointment(appointment_id: uuid.UUID) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, l.display_name, l.phone, l.external_user_id,
                       l.external_channel, l.address, l.latitude, l.longitude,
                       l.service_type, l.quoted_amount
                FROM appointments a
                JOIN leads l ON l.id = a.lead_id
                WHERE a.id = %s
                """,
                (str(appointment_id),),
            )
            row = cur.fetchone()
            return {k: _jsonify_value(v) for k, v in row.items()} if row else None


def update_appointment_status(
    appointment_id: uuid.UUID,
    *,
    status: str,
    team_id: str | None = None,
    scheduled_date: date | None = None,
    slot: str | None = None,
) -> dict[str, Any]:
    """Atualiza status / equipe / data / slot de um appointment."""
    allowed_status = {
        "pending_team_assignment",
        "proposed",
        "confirmed",
        "realloc",
        "cancelled",
        "done",
        "completed",
    }
    if status not in allowed_status:
        raise ValueError(f"Status inválido: {status}")
    sets = ["status = %s"]
    params: list[Any] = [status]
    if team_id is not None:
        sets.append("team_id = %s")
        params.append(team_id)
    if scheduled_date is not None:
        sets.append("scheduled_date = %s")
        params.append(scheduled_date)
    if slot is not None:
        if slot not in SLOT_LABELS:
            raise ValueError(f"Slot inválido: {slot}")
        sets.append("slot = %s")
        params.append(slot)
    sets.append("updated_at = now()")
    params.append(str(appointment_id))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE appointments SET {', '.join(sets)} WHERE id = %s RETURNING *",
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError("Appointment não encontrado")
            return {k: _jsonify_value(v) for k, v in row.items()}


def mark_lead_completed(lead_id: uuid.UUID) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stage FROM leads WHERE id = %s FOR UPDATE", (str(lead_id),))
            row = cur.fetchone()
            if not row:
                raise LookupError("Lead não encontrado")
            state_machine.assert_transition(row["stage"], "completed")
            cur.execute(
                """
                UPDATE leads
                SET stage = 'completed', completed_at = now(), updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (str(lead_id),),
            )
            out = cur.fetchone()
            assert out is not None
            return dict(out)


def dashboard_stage_counts() -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT stage, COUNT(*)::int AS total
                FROM leads
                GROUP BY stage
                ORDER BY stage
                """
            )
            return [dict(r) for r in cur.fetchall()]


def dashboard_upcoming_appointments(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, l.external_user_id, l.display_name, l.phone, l.address
                FROM appointments a
                JOIN leads l ON l.id = a.lead_id
                ORDER BY a.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return _jsonify_rows([dict(r) for r in cur.fetchall()])


def dashboard_jobs(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.*, l.external_user_id, l.display_name, l.phone, l.stage
                FROM automation_jobs j
                JOIN leads l ON l.id = j.lead_id
                ORDER BY j.run_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            return _jsonify_rows([dict(r) for r in cur.fetchall()])


def dashboard_recent_messages(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.*, l.external_user_id, l.display_name, l.phone
                FROM messages m
                JOIN leads l ON l.id = m.lead_id
                ORDER BY m.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return _jsonify_rows([dict(r) for r in cur.fetchall()])


def create_finance_entry(
    *,
    lead_id: uuid.UUID | None,
    appointment_id: uuid.UUID | None,
    entry_type: str,
    category: str,
    description: str | None,
    amount: Decimal | float | int,
    due_date: datetime | None = None,
    status: str = "pending",
    metadata: dict[str, Any] | None = None,
) -> uuid.UUID:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance_entries (
                    lead_id, appointment_id, entry_type, category, description,
                    amount, due_date, status, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(lead_id) if lead_id else None,
                    str(appointment_id) if appointment_id else None,
                    entry_type,
                    category,
                    description,
                    Decimal(str(amount)),
                    due_date.date() if due_date else None,
                    status,
                    Json(metadata or {}),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            return row["id"]


def dashboard_finance_entries(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.*, l.external_user_id, l.display_name, l.phone
                FROM finance_entries f
                LEFT JOIN leads l ON l.id = f.lead_id
                ORDER BY f.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return _jsonify_rows([dict(r) for r in cur.fetchall()])


def dashboard_finance_summary() -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN entry_type = 'income' AND status <> 'cancelled' THEN amount ELSE 0 END), 0)::numeric AS total_income,
                    COALESCE(SUM(CASE WHEN entry_type = 'expense' AND status <> 'cancelled' THEN amount ELSE 0 END), 0)::numeric AS total_expense,
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END), 0)::numeric AS total_pending
                FROM finance_entries
                """
            )
            row = cur.fetchone() or {}
            return {
                "total_income": str(row.get("total_income") or 0),
                "total_expense": str(row.get("total_expense") or 0),
                "total_pending": str(row.get("total_pending") or 0),
            }


def dashboard_finance_forecast_from_pipeline() -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN quoted_amount IS NOT NULL THEN quoted_amount ELSE 0 END), 0)::numeric AS quoted_total,
                    COUNT(*)::int AS leads_total,
                    COALESCE(SUM(CASE WHEN stage = 'scheduled' THEN 1 ELSE 0 END), 0)::int AS scheduled_total
                FROM leads
                """
            )
            row = cur.fetchone() or {}
            return {
                "quoted_total": str(row.get("quoted_total") or 0),
                "leads_total": row.get("leads_total") or 0,
                "scheduled_total": row.get("scheduled_total") or 0,
            }


def ensure_finance_schema() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS finance_entries (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    lead_id         UUID REFERENCES leads (id) ON DELETE SET NULL,
                    appointment_id  UUID REFERENCES appointments (id) ON DELETE SET NULL,
                    entry_type      TEXT NOT NULL,
                    category        TEXT NOT NULL,
                    description     TEXT,
                    amount          NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
                    due_date        DATE,
                    paid_at         TIMESTAMPTZ,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    metadata        JSONB NOT NULL DEFAULT '{}',
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS finance_entries_status_idx ON finance_entries (status, entry_type)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS finance_entries_due_idx ON finance_entries (due_date)"
            )
