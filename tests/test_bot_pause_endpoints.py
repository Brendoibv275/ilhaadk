# Copyright 2025 Ilha Ar.
"""Cobertura dos endpoints F — /leads/{id}/pause-bot, /resume-bot, /bot-status.

Mocka o `repository` para não depender de Postgres. Foco no contrato HTTP:
- status, shape do body
- chamada dos wrappers `pause_bot_for_lead` / `resume_bot_for_lead`
- 404 quando o lead não existe
- handoff cache é atualizado ao pausar e limpo ao retomar
"""

from __future__ import annotations

import uuid
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


@pytest.fixture
def base_lead(lead_id) -> dict[str, Any]:
    return {
        "id": str(lead_id),
        "external_channel": "whatsapp",
        "external_user_id": "5548999999999",
        "phone": "+5548999999999",
        "display_name": "Maria Teste",
        "stage": "quoted",
        "bot_paused": False,
        "bot_paused_at": None,
        "bot_paused_by": None,
        "bot_paused_reason": None,
        "bot_reactivated_at": None,
        "bot_reactivated_by": None,
        "equipe_responsavel": None,
    }


@pytest.fixture
def mock_repo(monkeypatch, base_lead, lead_id):
    calls: dict[str, list[Any]] = {"pause": [], "resume": [], "append": []}

    def fake_get_lead(lid):
        if str(lid) == str(lead_id):
            return dict(base_lead)
        return None

    def fake_pause(lid, *, reason: str = "", by: str = "system"):
        calls["pause"].append({"lid": str(lid), "reason": reason, "by": by})
        if str(lid) != str(lead_id):
            raise LookupError("Lead não encontrado")
        base_lead.update(
            {
                "bot_paused": True,
                "bot_paused_at": "2026-05-02T12:00:00+00:00",
                "bot_paused_by": by,
                "bot_paused_reason": reason,
            }
        )
        return dict(base_lead)

    def fake_resume(lid, *, by: str = "system", reason: str = "manual_resume"):
        calls["resume"].append({"lid": str(lid), "reason": reason, "by": by})
        if str(lid) != str(lead_id):
            raise LookupError("Lead não encontrado")
        base_lead.update(
            {
                "bot_paused": False,
                "bot_reactivated_at": "2026-05-02T12:05:00+00:00",
                "bot_reactivated_by": by,
            }
        )
        return dict(base_lead)

    def fake_append(**kwargs):
        calls["append"].append(kwargs)

    monkeypatch.setattr(repo, "get_lead", fake_get_lead)
    monkeypatch.setattr(repo, "pause_bot_for_lead", fake_pause)
    monkeypatch.setattr(repo, "resume_bot_for_lead", fake_resume)
    monkeypatch.setattr(repo, "set_bot_paused", lambda *a, **kw: fake_resume(a[0], **{k: v for k, v in kw.items() if k in ("by", "reason")}) if not kw.get("paused", True) else fake_pause(a[0], **{k: v for k, v in kw.items() if k in ("by", "reason")}))
    monkeypatch.setattr(repo, "append_message", fake_append)

    return calls


# -----------------------------------------------------------------------------
# GET /leads/{id}/bot-status
# -----------------------------------------------------------------------------


def test_bot_status_not_found(client, mock_repo):
    other = uuid.uuid4()
    resp = client.get(f"/leads/{other}/bot-status")
    assert resp.status_code == 404


def test_bot_status_ok_nao_pausado(client, lead_id, mock_repo):
    resp = client.get(f"/leads/{lead_id}/bot-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lead_id"] == str(lead_id)
    assert body["bot_paused"] is False
    assert body["bot_paused_reason"] is None


# -----------------------------------------------------------------------------
# POST /leads/{id}/pause-bot
# -----------------------------------------------------------------------------


def test_pause_bot_sem_body(client, lead_id, mock_repo):
    # Limpa cache pra estado determinístico.
    webhook_api._handoff_cache.clear()

    resp = client.post(f"/leads/{lead_id}/pause-bot")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["bot_paused"] is True
    assert body["bot_paused_reason"] == "manual_pause"
    assert body["bot_paused_by"] == "frontend"
    assert mock_repo["pause"] == [
        {"lid": str(lead_id), "reason": "manual_pause", "by": "frontend"}
    ]


def test_pause_bot_com_reason(client, lead_id, mock_repo):
    webhook_api._handoff_cache.clear()

    resp = client.post(
        f"/leads/{lead_id}/pause-bot",
        json={"reason": "cliente pediu falar com humano"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_paused_reason"] == "cliente pediu falar com humano"
    assert mock_repo["pause"][0]["reason"] == "cliente pediu falar com humano"
    # Mensagem tool registrada.
    tool_msgs = [c for c in mock_repo["append"] if c.get("role") == "tool"]
    assert any("pausado manualmente" in c["body"] for c in tool_msgs)


def test_pause_bot_sincroniza_cache_handoff(client, lead_id, mock_repo):
    # Pré-popula o cache com uma chave que contém o external_user_id do lead.
    webhook_api._handoff_cache.clear()
    key = "inst1|5548999999999@s.whatsapp.net"
    webhook_api._handoff_cache[key] = False

    resp = client.post(f"/leads/{lead_id}/pause-bot", json={})
    assert resp.status_code == 200
    assert webhook_api._handoff_cache[key] is True


# -----------------------------------------------------------------------------
# POST /leads/{id}/resume-bot
# -----------------------------------------------------------------------------


def test_resume_bot_ok(client, lead_id, mock_repo):
    webhook_api._handoff_cache.clear()
    webhook_api._handoff_cache["inst1|5548999999999"] = True

    resp = client.post(f"/leads/{lead_id}/resume-bot")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["bot_paused"] is False
    assert body["bot_reactivated_by"] == "frontend"
    assert mock_repo["resume"] == [
        {"lid": str(lead_id), "reason": "manual_resume", "by": "frontend"}
    ]
    # Cache foi limpo para esse external_user_id.
    assert "inst1|5548999999999" not in webhook_api._handoff_cache
