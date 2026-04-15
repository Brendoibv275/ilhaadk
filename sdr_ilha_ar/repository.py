# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Acesso síncrono ao PostgreSQL para leads, mensagens, jobs e eventos."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import time
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from sdr_ilha_ar.config import settings
from sdr_ilha_ar import state_machine

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
                INSERT INTO leads (external_channel, external_user_id, last_inbound_at)
                VALUES (%s, %s, CASE WHEN %s THEN now() ELSE NULL END)
                ON CONFLICT (external_channel, external_user_id)
                DO UPDATE SET
                    last_inbound_at = CASE
                        WHEN %s THEN now()
                        ELSE leads.last_inbound_at
                    END
                RETURNING id
                """,
                (external_channel, external_user_id, touch_inbound, touch_inbound),
            )
            row = cur.fetchone()
            assert row is not None
            return row["id"]


def get_lead(lead_id: uuid.UUID) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE id = %s", (str(lead_id),))
            return cur.fetchone()


def save_lead_field(lead_id: uuid.UUID, field_name: str, value: Any) -> dict[str, Any]:
    if field_name not in ALLOWED_LEAD_FIELDS:
        raise ValueError(f"Campo não permitido: {field_name}")

    col = field_name
    if field_name == "quoted_amount":
        if value is None:
            py_val: Any = None
        else:
            py_val = Decimal(str(value))
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
            return dict(out)


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
