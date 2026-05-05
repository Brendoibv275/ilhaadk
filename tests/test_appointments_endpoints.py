# Copyright 2025 Ilha Ar.
"""Cobertura dos endpoints REST F5+A5 (confirm / realloc / cancel).

Estes testes mockam o `repository` para não exigir Postgres e mockam o envio
Evolution para não bater em rede. O foco é o contrato HTTP: status, body,
mensagem PT-BR ao lead e erro 404 quando o appointment não existe.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sdr_ilha_ar import repository as repo
from sdr_ilha_ar import webhook_api


@pytest.fixture
def client(monkeypatch) -> TestClient:
    # Evita tentativa de bootstrap do schema em startup.
    monkeypatch.setattr(repo, "bootstrap_db_schema", lambda: None)
    monkeypatch.setattr(repo, "ensure_finance_schema", lambda: None)
    return TestClient(webhook_api.app)


@pytest.fixture
def appt_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def lead_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def base_appt(appt_id, lead_id) -> dict[str, Any]:
    # Shape idêntico ao retorno de repo.get_appointment (join com leads).
    return {
        "id": str(appt_id),
        "lead_id": str(lead_id),
        "status": "pending_team_assignment",
        "scheduled_date": "2026-05-05",
        "slot": "morning_early",
        "window_label": "05/05/2026 08h-10h",
        "team_id": None,
        "notes": "",
        "display_name": "João Teste",
        "phone": "+5548999999999",
        "external_user_id": "5548999999999@s.whatsapp.net",
        "external_channel": "whatsapp",
        "address": "Rua das Flores, 123",
        "latitude": None,
        "longitude": None,
        "service_type": "manutencao",
        "quoted_amount": None,
    }


@pytest.fixture
def mock_repo_and_send(monkeypatch, base_appt, appt_id):
    """Mocka get_appointment, update_appointment_status, append_message e envio."""
    sent_messages: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []

    def fake_get(aid: uuid.UUID) -> dict[str, Any] | None:
        if str(aid) == str(appt_id):
            return dict(base_appt)
        return None

    def fake_update(aid: uuid.UUID, **kwargs: Any) -> dict[str, Any]:
        if str(aid) != str(appt_id):
            raise LookupError("Appointment não encontrado")
        updates.append(kwargs)
        merged = dict(base_appt)
        merged.update({k: v for k, v in kwargs.items() if v is not None})
        # `scheduled_date` chega como date; normalizamos para ISO string.
        sd = kwargs.get("scheduled_date")
        if sd is not None:
            merged["scheduled_date"] = sd.isoformat() if hasattr(sd, "isoformat") else str(sd)
        # Aplica também a nova versão no "banco" para o segundo get_appointment.
        base_appt.update(merged)
        return merged

    def fake_send(*, remote_jid: str, phone: str, text: str, evolution_instance: str = "") -> None:
        sent_messages.append({"remote_jid": remote_jid, "phone": phone, "text": text})

    monkeypatch.setattr(repo, "get_appointment", fake_get)
    monkeypatch.setattr(repo, "update_appointment_status", fake_update)
    monkeypatch.setattr(repo, "append_message", lambda *a, **kw: None)
    monkeypatch.setattr(webhook_api, "_send_whatsapp_reply", fake_send)

    return {"sent": sent_messages, "updates": updates}


# -----------------------------------------------------------------------------
# CONFIRM
# -----------------------------------------------------------------------------


def test_confirm_sem_team(client, appt_id, mock_repo_and_send):
    resp = client.post(f"/appointments/{appt_id}/confirm", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["appointment"]["status"] == "confirmed"
    msg = body["message_sent"]
    assert "✅" in msg
    assert "05/05/2026" in msg
    assert "08h-10h" in msg
    assert "equipe técnica" in msg  # texto sem team_id
    # Atualização correta no repo.
    assert mock_repo_and_send["updates"] == [{"status": "confirmed", "team_id": None}]
    # Mensagem enviada ao lead via Evolution.
    assert len(mock_repo_and_send["sent"]) == 1
    assert mock_repo_and_send["sent"][0]["phone"] == "+5548999999999"
    assert mock_repo_and_send["sent"][0]["text"] == msg


def test_confirm_com_team(client, appt_id, mock_repo_and_send):
    resp = client.post(
        f"/appointments/{appt_id}/confirm",
        json={"team_id": "equipe-azul"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["appointment"]["team_id"] == "equipe-azul"
    msg = body["message_sent"]
    # team_id fica salvo no appointment, mas a msg NÃO expõe UUID/ID pro cliente.
    assert "Nossa equipe técnica" in msg
    assert "equipe-azul" not in msg  # nunca vazar identificador interno
    assert "05/05/2026" in msg
    assert mock_repo_and_send["updates"] == [
        {"status": "confirmed", "team_id": "equipe-azul"}
    ]


def test_confirm_appointment_nao_existe(client, mock_repo_and_send):
    other_id = uuid.uuid4()
    resp = client.post(f"/appointments/{other_id}/confirm", json={})
    assert resp.status_code == 404
    assert "não encontrado" in resp.json()["detail"].lower()
    assert mock_repo_and_send["sent"] == []
    assert mock_repo_and_send["updates"] == []


# -----------------------------------------------------------------------------
# REALLOC
# -----------------------------------------------------------------------------


def test_realloc_sucesso(client, appt_id, mock_repo_and_send):
    resp = client.post(
        f"/appointments/{appt_id}/realloc",
        json={"new_date": "12/05/2026", "new_slot": "afternoon_late"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["appointment"]["status"] == "realloc"
    msg = body["message_sent"]
    # Nome do lead (só primeiro nome).
    assert "João" in msg
    assert "12/05/2026" in msg
    assert "16h-18h" in msg
    assert "SIM" in msg and "NÃO" in msg
    # update_appointment_status recebeu os 3 campos.
    upd = mock_repo_and_send["updates"][0]
    assert upd["status"] == "realloc"
    assert upd["scheduled_date"] == date(2026, 5, 12)
    assert upd["slot"] == "afternoon_late"
    assert len(mock_repo_and_send["sent"]) == 1


def test_realloc_slot_invalido(client, appt_id, mock_repo_and_send):
    resp = client.post(
        f"/appointments/{appt_id}/realloc",
        json={"new_date": "12/05/2026", "new_slot": "madrugada"},
    )
    assert resp.status_code == 400
    assert "Slot inválido" in resp.json()["detail"]
    assert mock_repo_and_send["updates"] == []
    assert mock_repo_and_send["sent"] == []


def test_realloc_data_invalida(client, appt_id, mock_repo_and_send):
    resp = client.post(
        f"/appointments/{appt_id}/realloc",
        json={"new_date": "2026-05-12", "new_slot": "afternoon_late"},
    )
    assert resp.status_code == 400
    assert "DD/MM/AAAA" in resp.json()["detail"]


def test_realloc_appointment_nao_existe(client, mock_repo_and_send):
    other_id = uuid.uuid4()
    resp = client.post(
        f"/appointments/{other_id}/realloc",
        json={"new_date": "12/05/2026", "new_slot": "morning_late"},
    )
    assert resp.status_code == 404
    assert mock_repo_and_send["sent"] == []


# -----------------------------------------------------------------------------
# CANCEL
# -----------------------------------------------------------------------------


def test_cancel_sem_motivo(client, appt_id, mock_repo_and_send):
    resp = client.post(f"/appointments/{appt_id}/cancel", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["appointment"]["status"] == "cancelled"
    msg = body["message_sent"]
    assert "João" in msg
    assert "cancelar seu agendamento" in msg
    # Sem motivo, nada entre parênteses.
    assert "(" not in msg
    assert mock_repo_and_send["updates"] == [{"status": "cancelled"}]


def test_cancel_com_motivo(client, appt_id, mock_repo_and_send):
    resp = client.post(
        f"/appointments/{appt_id}/cancel",
        json={"reason": "equipe sem disponibilidade"},
    )
    assert resp.status_code == 200
    msg = resp.json()["message_sent"]
    assert "(equipe sem disponibilidade)" in msg
    assert "remarcar" in msg


def test_cancel_appointment_nao_existe(client, mock_repo_and_send):
    other_id = uuid.uuid4()
    resp = client.post(f"/appointments/{other_id}/cancel", json={})
    assert resp.status_code == 404
    assert mock_repo_and_send["sent"] == []


# -----------------------------------------------------------------------------
# Tolerância do envio: falha no Evolution não deve quebrar o endpoint
# (status já foi persistido).
# -----------------------------------------------------------------------------


def test_confirm_continua_ok_mesmo_com_falha_no_envio(
    client, appt_id, monkeypatch, mock_repo_and_send
):
    def exploding_send(**_kwargs):
        raise RuntimeError("Evolution offline")

    monkeypatch.setattr(webhook_api, "_send_whatsapp_reply", exploding_send)

    resp = client.post(f"/appointments/{appt_id}/confirm", json={})
    assert resp.status_code == 200
    assert resp.json()["appointment"]["status"] == "confirmed"
