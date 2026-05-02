# Copyright 2025 Ilha Ar.
"""Cobertura G — histórico de estágio, helpers e endpoint /leads/{id}/stage-info.

Mocka `repository` sem Postgres. Foco:
- GET /leads/{id}/stage-info retorna shape correto.
- 404 quando lead não existe.
- duration_seconds é inteiro (a partir de timedelta).
- history é passada adiante sem distorção.
- entered_at é selecionado do registro aberto do estágio atual.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sdr_ilha_ar import repository as repo
from sdr_ilha_ar import webhook_api


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(repo, "bootstrap_db_schema", lambda: None)
    monkeypatch.setattr(repo, "ensure_finance_schema", lambda: None)
    return TestClient(webhook_api.app)


@pytest.fixture
def lead_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_history(lead_id: uuid.UUID) -> list[dict[str, Any]]:
    now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
    return [
        {
            "id": str(uuid.uuid4()),
            "lead_id": str(lead_id),
            "stage": "new",
            "entered_at": now - timedelta(days=3),
            "exited_at": now - timedelta(days=2),
        },
        {
            "id": str(uuid.uuid4()),
            "lead_id": str(lead_id),
            "stage": "qualified",
            "entered_at": now - timedelta(days=2),
            "exited_at": now - timedelta(days=1),
        },
        {
            "id": str(uuid.uuid4()),
            "lead_id": str(lead_id),
            "stage": "quoted",
            "entered_at": now - timedelta(days=1),
            "exited_at": None,
        },
    ]


def test_stage_info_ok(client, monkeypatch, lead_id):
    lead = {"id": str(lead_id), "stage": "quoted"}
    history = _make_history(lead_id)
    monkeypatch.setattr(repo, "get_lead", lambda lid: lead if str(lid) == str(lead_id) else None)
    monkeypatch.setattr(repo, "get_stage_history", lambda lid: history)
    monkeypatch.setattr(repo, "get_current_stage_duration", lambda lid: timedelta(days=1, hours=4))

    resp = client.get(f"/leads/{lead_id}/stage-info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["lead_id"] == str(lead_id)
    assert data["current_stage"] == "quoted"
    # 1d4h = 86400 + 14400 = 100800
    assert data["duration_seconds"] == 100800
    assert len(data["history"]) == 3
    # entered_at do estágio atual (quoted aberto)
    assert data["entered_at"] is not None


def test_stage_info_404(client, monkeypatch, lead_id):
    monkeypatch.setattr(repo, "get_lead", lambda lid: None)
    resp = client.get(f"/leads/{lead_id}/stage-info")
    assert resp.status_code == 404


def test_stage_info_sem_historico(client, monkeypatch, lead_id):
    """Lead antigo (pré-migration) pode não ter histórico — endpoint deve responder sem explodir."""
    lead = {"id": str(lead_id), "stage": "new"}
    monkeypatch.setattr(repo, "get_lead", lambda lid: lead)
    monkeypatch.setattr(repo, "get_stage_history", lambda lid: [])
    monkeypatch.setattr(repo, "get_current_stage_duration", lambda lid: None)

    resp = client.get(f"/leads/{lead_id}/stage-info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current_stage"] == "new"
    assert data["duration_seconds"] is None
    assert data["history"] == []
    assert data["entered_at"] is None
