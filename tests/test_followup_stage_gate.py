# Copyright 2025 Ilha Ar.
"""G — Worker de follow-up agora decide disparo a partir de `get_stage_duration_for("quoted")`.

Foco:
- Lead em outro estágio (ex: 'scheduled') ⇒ follow-up é pulado.
- Lead em 'quoted' há menos tempo que o threshold ⇒ follow-up é pulado.
- Lead em 'quoted' há tempo suficiente ⇒ follow-up é enviado (append_message chamado).
- Templates legados (sem threshold) seguem funcionando normalmente.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from sdr_ilha_ar import repository as repo
from sdr_ilha_ar.workers import processor


@pytest.fixture
def lead_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_job(lead_id: uuid.UUID, template: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "lead_id": str(lead_id),
        "payload": {"template": template, "followup_tag": template},
    }


def _patch_repo(monkeypatch, *, lead: dict[str, Any], stage_duration: timedelta | None):
    calls: dict[str, list[Any]] = {"append": []}
    monkeypatch.setattr(repo, "get_lead", lambda lid: dict(lead))
    monkeypatch.setattr(
        repo,
        "get_stage_duration_for",
        lambda lid, stage: stage_duration,
    )
    monkeypatch.setattr(
        repo,
        "append_message",
        lambda lead_id, role, body, metadata=None: calls["append"].append(
            {"lead_id": str(lead_id), "role": role, "body": body}
        ),
    )
    return calls


def test_followup_pulado_quando_lead_nao_esta_em_quoted(monkeypatch, lead_id):
    lead = {"id": str(lead_id), "stage": "scheduled", "display_name": "Maria"}
    calls = _patch_repo(monkeypatch, lead=lead, stage_duration=timedelta(days=1))
    processor._process_send_followup(_make_job(lead_id, "followup_45min"))
    assert calls["append"] == []  # pulado


def test_followup_pulado_quando_abaixo_do_threshold(monkeypatch, lead_id):
    lead = {"id": str(lead_id), "stage": "quoted", "display_name": "Maria"}
    # em quoted há 10 min, threshold 45min
    calls = _patch_repo(monkeypatch, lead=lead, stage_duration=timedelta(minutes=10))
    processor._process_send_followup(_make_job(lead_id, "followup_45min"))
    assert calls["append"] == []


def test_followup_disparado_quando_threshold_atingido(monkeypatch, lead_id):
    lead = {"id": str(lead_id), "stage": "quoted", "display_name": "Maria"}
    calls = _patch_repo(monkeypatch, lead=lead, stage_duration=timedelta(minutes=50))
    processor._process_send_followup(_make_job(lead_id, "followup_45min"))
    assert len(calls["append"]) == 1
    assert "[FOLLOWUP:45min]" in calls["append"][0]["body"]


def test_followup_sem_historico_prossegue(monkeypatch, lead_id):
    """Lead antigo sem histórico em lead_stage_history: não quebra, dispara normal."""
    lead = {"id": str(lead_id), "stage": "quoted", "display_name": "Maria"}
    calls = _patch_repo(monkeypatch, lead=lead, stage_duration=None)
    processor._process_send_followup(_make_job(lead_id, "followup_1h"))
    assert len(calls["append"]) == 1


def test_followup_legacy_template_nao_e_gated(monkeypatch, lead_id):
    """Template legado 'followup' não tem threshold — sempre dispara."""
    lead = {"id": str(lead_id), "stage": "new", "display_name": "Maria"}
    calls = _patch_repo(monkeypatch, lead=lead, stage_duration=None)
    processor._process_send_followup(_make_job(lead_id, "followup"))
    assert len(calls["append"]) == 1


def test_followup_3d_requer_quoted_amount(monkeypatch, lead_id):
    """Regra de negócio: followup_3d sem quoted_amount vira mensagem neutra."""
    lead = {"id": str(lead_id), "stage": "quoted", "display_name": "Maria", "quoted_amount": None}
    calls = _patch_repo(monkeypatch, lead=lead, stage_duration=timedelta(days=4))
    processor._process_send_followup(_make_job(lead_id, "followup_3d"))
    assert len(calls["append"]) == 1
    assert "[FOLLOWUP:3d_no_quote]" in calls["append"][0]["body"]
