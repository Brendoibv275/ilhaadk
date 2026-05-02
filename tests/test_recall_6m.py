# Copyright 2025 Ilha Ar.
"""H — cobertura do recall 6m pós-conclusão de appointment.

Três camadas cobertas:
1. Worker `_process_followup_recall_6m` — gera mensagem [FOLLOWUP:6m_recall] e
   pula se lead inexiste ou se tem appointment ativo.
2. Endpoint `POST /appointments/{id}/complete` — marca status=completed e
   enfileira o job recall_6m com idempotência.
3. `get_pricing_quote(service_type="limpeza_recall_6m")` retorna R$ 280.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sdr_ilha_ar import repository as repo
from sdr_ilha_ar import webhook_api
from sdr_ilha_ar import tools_impl
from sdr_ilha_ar.workers import processor


# -----------------------------------------------------------------------------
# 1) Worker — _process_followup_recall_6m
# -----------------------------------------------------------------------------


@pytest.fixture
def lead_id() -> uuid.UUID:
    return uuid.uuid4()


def _job(lead_id: uuid.UUID) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "lead_id": str(lead_id),
        "payload": {"reason": "appointment_completed", "offer_amount_brl": 280.0},
    }


def _patch_worker(
    monkeypatch,
    *,
    lead: dict[str, Any] | None,
    active_appointments: list[dict[str, Any]],
) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"append": []}
    monkeypatch.setattr(repo, "get_lead", lambda lid: dict(lead) if lead else None)
    monkeypatch.setattr(
        repo,
        "list_active_appointments_for_lead",
        lambda lid: list(active_appointments),
    )
    monkeypatch.setattr(
        repo,
        "append_message",
        lambda lead_id, role, body, metadata=None: calls["append"].append(
            {"lead_id": str(lead_id), "role": role, "body": body}
        ),
    )
    return calls


def test_recall_6m_dispara_oferta_r280(monkeypatch, lead_id):
    lead = {"id": str(lead_id), "display_name": "Carla", "stage": "completed"}
    calls = _patch_worker(monkeypatch, lead=lead, active_appointments=[])
    processor._process_followup_recall_6m(_job(lead_id))
    assert len(calls["append"]) == 1
    body = calls["append"][0]["body"]
    assert "[FOLLOWUP:6m_recall]" in body
    assert "Carla" in body
    assert "R$ 280" in body
    assert "limpeza de manutenção" in body.lower()


def test_recall_6m_usa_fallback_cliente_sem_nome(monkeypatch, lead_id):
    lead = {"id": str(lead_id), "display_name": None, "stage": "completed"}
    calls = _patch_worker(monkeypatch, lead=lead, active_appointments=[])
    processor._process_followup_recall_6m(_job(lead_id))
    assert len(calls["append"]) == 1
    assert "Cliente" in calls["append"][0]["body"]


def test_recall_6m_pula_se_lead_nao_existe(monkeypatch, lead_id):
    calls = _patch_worker(monkeypatch, lead=None, active_appointments=[])
    processor._process_followup_recall_6m(_job(lead_id))
    assert calls["append"] == []


def test_recall_6m_pula_se_tem_appointment_ativo(monkeypatch, lead_id):
    lead = {"id": str(lead_id), "display_name": "Carla", "stage": "scheduled"}
    active = [{"id": str(uuid.uuid4()), "status": "confirmed"}]
    calls = _patch_worker(monkeypatch, lead=lead, active_appointments=active)
    processor._process_followup_recall_6m(_job(lead_id))
    assert calls["append"] == []


def test_process_job_roteamento_followup_recall_6m(monkeypatch, lead_id):
    """process_job deve rotear `followup_recall_6m` pro handler correto e marcar complete_job."""
    lead = {"id": str(lead_id), "display_name": "Carla", "stage": "completed"}
    _patch_worker(monkeypatch, lead=lead, active_appointments=[])
    completed: list[uuid.UUID] = []
    monkeypatch.setattr(repo, "complete_job", lambda jid: completed.append(jid))
    monkeypatch.setattr(repo, "fail_job", lambda jid, err: pytest.fail(f"nao era pra falhar: {err}"))
    job = _job(lead_id)
    job["job_type"] = "followup_recall_6m"
    processor.process_job(job)
    assert len(completed) == 1


# -----------------------------------------------------------------------------
# 2) Endpoint POST /appointments/{id}/complete
# -----------------------------------------------------------------------------


@pytest.fixture
def appt_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def lead_id_appt() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(repo, "bootstrap_db_schema", lambda: None)
    monkeypatch.setattr(repo, "ensure_finance_schema", lambda: None)
    return TestClient(webhook_api.app)


@pytest.fixture
def mock_complete(monkeypatch, appt_id, lead_id_appt):
    base_appt = {
        "id": str(appt_id),
        "lead_id": str(lead_id_appt),
        "status": "confirmed",
        "scheduled_date": "2026-04-10",
        "slot": "morning_early",
    }
    updates: list[dict[str, Any]] = []
    enqueued: list[dict[str, Any]] = []

    def fake_get(aid):
        if str(aid) == str(appt_id):
            return dict(base_appt)
        return None

    def fake_update(aid, **kwargs):
        if str(aid) != str(appt_id):
            raise LookupError("Appointment não encontrado")
        updates.append(kwargs)
        merged = dict(base_appt)
        merged.update(kwargs)
        base_appt.update(merged)
        return merged

    def fake_enqueue(lid, job_type, run_at, payload, idempotency_key):
        jid = uuid.uuid4()
        enqueued.append(
            {
                "lead_id": str(lid),
                "job_type": job_type,
                "run_at": run_at,
                "payload": payload,
                "idempotency_key": idempotency_key,
                "job_id": jid,
            }
        )
        return jid

    monkeypatch.setattr(repo, "get_appointment", fake_get)
    monkeypatch.setattr(repo, "update_appointment_status", fake_update)
    monkeypatch.setattr(repo, "enqueue_job", fake_enqueue)
    monkeypatch.setattr(repo, "append_message", lambda *a, **kw: None)
    return {"updates": updates, "enqueued": enqueued}


def test_complete_appointment_dispara_recall_6m(client, appt_id, lead_id_appt, mock_complete):
    resp = client.post(f"/appointments/{appt_id}/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["appointment"]["status"] == "completed"
    assert body["recall_6m_job_id"] is not None
    # update_appointment_status recebeu status=completed.
    assert mock_complete["updates"] == [{"status": "completed"}]
    # 1 job enfileirado, tipo recall_6m, run_at ~180 dias, idempotency baseada no lead.
    assert len(mock_complete["enqueued"]) == 1
    enq = mock_complete["enqueued"][0]
    assert enq["job_type"] == "followup_recall_6m"
    assert enq["lead_id"] == str(lead_id_appt)
    assert enq["idempotency_key"] == f"recall6m_{lead_id_appt}"
    # Janela ~180 dias (tolerância de 1 dia pra drift de relógio em CI).
    delta = enq["run_at"] - datetime.now(timezone.utc)
    assert timedelta(days=179) <= delta <= timedelta(days=181)
    assert enq["payload"].get("offer_amount_brl") == 280.0


def test_complete_appointment_inexistente_retorna_404(client, mock_complete):
    other = uuid.uuid4()
    resp = client.post(f"/appointments/{other}/complete")
    assert resp.status_code == 404
    assert mock_complete["updates"] == []
    assert mock_complete["enqueued"] == []


def test_schedule_recall_6m_idempotente(monkeypatch, lead_id_appt):
    """Segunda chamada retorna None (job duplicado) sem estourar."""
    calls: list[str] = []

    def fake_enqueue(lid, job_type, run_at, payload, idempotency_key):
        calls.append(idempotency_key)
        if len(calls) == 1:
            return uuid.uuid4()
        return None  # simula ON CONFLICT DO NOTHING

    monkeypatch.setattr(repo, "enqueue_job", fake_enqueue)
    monkeypatch.setattr(repo, "append_message", lambda *a, **kw: None)

    jid1 = webhook_api.schedule_recall_6m_for_lead(lead_id_appt)
    jid2 = webhook_api.schedule_recall_6m_for_lead(lead_id_appt)
    assert jid1 is not None
    assert jid2 is None
    assert calls == [f"recall6m_{lead_id_appt}", f"recall6m_{lead_id_appt}"]


# -----------------------------------------------------------------------------
# 3) Pricing — limpeza_recall_6m retorna R$ 280
# -----------------------------------------------------------------------------


def test_get_pricing_quote_limpeza_recall_6m():
    out = tools_impl.get_pricing_quote(service_type="limpeza_recall_6m")
    assert out["status"] == "ok"
    assert out["amount_brl"] == 280.0
    assert out["currency"] == "BRL"
    assert "R$ 280" in out["summary"]


def test_get_pricing_quote_recall_6m_alias_curto():
    out = tools_impl.get_pricing_quote(service_type="recall_6m")
    assert out["status"] == "ok"
    assert out["amount_brl"] == 280.0


def test_fixed_service_quotes_contem_recall_6m():
    assert tools_impl.FIXED_SERVICE_QUOTES_BRL.get("limpeza_recall_6m") == 280.0
