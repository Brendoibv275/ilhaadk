# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Cobertura de check_availability + book_slot (F2+A4 tools expostas ao LLM).

Estes testes mockam o repository para não exigir Postgres real: a engine de
slots já tem cobertura de integração própria; aqui focamos no contrato das
tools (parsing DD/MM/AAAA, mensagem tell_client em PT-BR, mapeamento de erro).
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest


class _FakeToolContext:
    """Mimetiza o mínimo do ToolContext do ADK usado por `_resolve_lead_id`."""

    def __init__(self, lead_id: str | None = None) -> None:
        # Estado já resolvido -> tools usam esse lead_id direto, sem tocar no DB.
        self.state: dict[str, Any] = {}
        if lead_id is not None:
            self.state["lead_id"] = lead_id
        self.user_id = "5598999999999"


@pytest.fixture
def fake_ctx() -> _FakeToolContext:
    return _FakeToolContext(lead_id=str(uuid.uuid4()))


# -----------------------------------------------------------------------------
# check_availability
# -----------------------------------------------------------------------------


def test_check_availability_dia_vazio(monkeypatch, fake_ctx):
    """Todos os 4 slots livres → tell_client lista as 4 faixas."""
    from sdr_ilha_ar import tools_impl
    from sdr_ilha_ar import repository as lead_repo

    captured: dict[str, Any] = {}

    def fake_check(appointment_date: date) -> dict[str, bool]:
        captured["date"] = appointment_date
        return {slot: True for slot in lead_repo.SLOTS_ORDER}

    monkeypatch.setattr(lead_repo, "check_slot_availability", fake_check)

    out = tools_impl.check_availability("05/05/2026", fake_ctx)

    assert out["status"] == "ok"
    assert out["date"] == "05/05/2026"
    assert captured["date"] == date(2026, 5, 5)
    assert out["slots"] == {slot: "livre" for slot in lead_repo.SLOTS_ORDER}
    assert out["slot_labels"]["morning_early"] == "08h-10h"
    # tell_client deve mencionar pelo menos 2 faixas de horário.
    tc = out["tell_client"]
    assert "8h-10h" in tc
    assert "16h-18h" in tc
    assert "livres" in tc


def test_check_availability_depois_de_1_slot_ocupado(monkeypatch, fake_ctx):
    """Com 1 slot ocupado, ele deve aparecer como 'ocupado' e sumir da tell_client."""
    from sdr_ilha_ar import tools_impl
    from sdr_ilha_ar import repository as lead_repo

    def fake_check(appointment_date: date) -> dict[str, bool]:
        free = {slot: True for slot in lead_repo.SLOTS_ORDER}
        free["morning_early"] = False  # já bookado
        return free

    monkeypatch.setattr(lead_repo, "check_slot_availability", fake_check)

    out = tools_impl.check_availability("05/05/2026", fake_ctx)

    assert out["status"] == "ok"
    assert out["slots"]["morning_early"] == "ocupado"
    assert out["slots"]["morning_late"] == "livre"
    # 8h-10h NÃO deve aparecer no texto ao cliente.
    assert "8h-10h" not in out["tell_client"]
    assert "10h-12h" in out["tell_client"]


def test_check_availability_dia_cheio(monkeypatch, fake_ctx):
    """Todos ocupados → tell_client de dia cheio."""
    from sdr_ilha_ar import tools_impl
    from sdr_ilha_ar import repository as lead_repo

    def fake_check(appointment_date: date) -> dict[str, bool]:
        return {slot: False for slot in lead_repo.SLOTS_ORDER}

    monkeypatch.setattr(lead_repo, "check_slot_availability", fake_check)

    out = tools_impl.check_availability("05/05/2026", fake_ctx)

    assert out["status"] == "ok"
    assert "4 atendimentos" in out["tell_client"]


def test_check_availability_data_invalida(fake_ctx):
    from sdr_ilha_ar import tools_impl

    out = tools_impl.check_availability("2026-05-05", fake_ctx)
    assert out["status"] == "error"
    assert "DD/MM/AAAA" in out["message"]


# -----------------------------------------------------------------------------
# book_slot
# -----------------------------------------------------------------------------


def _mute_side_effects(monkeypatch):
    """Silencia chamadas best-effort (append_message, label, advance funnel)."""
    from sdr_ilha_ar import tools_impl
    from sdr_ilha_ar import repository as lead_repo

    monkeypatch.setattr(lead_repo, "append_message", lambda *a, **kw: None)
    monkeypatch.setattr(tools_impl, "_advance_lead_to_scheduled", lambda _lid: None)
    monkeypatch.setattr(tools_impl, "_label_lead_chat", lambda *_a, **_kw: None)


def test_book_slot_sucesso(monkeypatch, fake_ctx):
    from sdr_ilha_ar import tools_impl
    from sdr_ilha_ar import repository as lead_repo

    _mute_side_effects(monkeypatch)
    appt_id = uuid.uuid4()
    captured: dict[str, Any] = {}

    def fake_create(lead_id, *, appointment_date, slot, notes="", window_label=""):
        captured.update(
            {
                "lead_id": lead_id,
                "appointment_date": appointment_date,
                "slot": slot,
                "notes": notes,
            }
        )
        return {
            "id": str(appt_id),
            "lead_id": str(lead_id),
            "scheduled_date": appointment_date.isoformat(),
            "slot": slot,
            "status": "pending_team_assignment",
        }

    monkeypatch.setattr(lead_repo, "create_slot_appointment", fake_create)

    out = tools_impl.book_slot("05/05/2026", "morning_early", fake_ctx, notes="pin OK")

    assert out["status"] == "ok"
    assert out["appointment_id"] == str(appt_id)
    assert out["date"] == "05/05/2026"
    assert out["slot"] == "morning_early"
    assert out["slot_label"] == "08h-10h"
    assert out["requires_team_assignment"] is True
    assert "Prontinho" in out["tell_client"]
    assert "05/05" in out["tell_client"]
    assert "8h-10h" in out["tell_client"]
    assert captured["appointment_date"] == date(2026, 5, 5)
    assert captured["slot"] == "morning_early"
    assert captured["notes"] == "pin OK"


def test_book_slot_falha_slot_ocupado(monkeypatch, fake_ctx):
    from sdr_ilha_ar import tools_impl
    from sdr_ilha_ar import repository as lead_repo

    _mute_side_effects(monkeypatch)

    def fake_create(*args, **kwargs):
        raise ValueError("Slot morning_early em 2026-05-05 já está ocupado.")

    monkeypatch.setattr(lead_repo, "create_slot_appointment", fake_create)

    out = tools_impl.book_slot("05/05/2026", "morning_early", fake_ctx)

    assert out["status"] == "error"
    assert "ocupado" in out["message"]
    # tell_client deve orientar o cliente a escolher outro horário do mesmo dia.
    assert "outro horário" in out["tell_client"] or "outro horario" in out["tell_client"]


def test_book_slot_falha_dia_cheio(monkeypatch, fake_ctx):
    from sdr_ilha_ar import tools_impl
    from sdr_ilha_ar import repository as lead_repo

    _mute_side_effects(monkeypatch)

    def fake_create(*args, **kwargs):
        raise ValueError("Dia 2026-05-05 já está com 4 atendimentos.")

    monkeypatch.setattr(lead_repo, "create_slot_appointment", fake_create)

    out = tools_impl.book_slot("05/05/2026", "afternoon_late", fake_ctx)

    assert out["status"] == "error"
    assert "4 atendimentos" in out["message"]
    assert "4 atendimentos" in out["tell_client"]
    assert "outro dia" in out["tell_client"]


def test_book_slot_data_invalida(fake_ctx):
    from sdr_ilha_ar import tools_impl

    out = tools_impl.book_slot("amanhã", "morning_early", fake_ctx)
    assert out["status"] == "error"
    assert "DD/MM/AAAA" in out["message"]


def test_book_slot_slot_invalido(fake_ctx):
    from sdr_ilha_ar import tools_impl

    out = tools_impl.book_slot("05/05/2026", "tarde_da_noite", fake_ctx)
    assert out["status"] == "error"
    assert "Slot inválido" in out["message"]
