# Copyright 2025 Ilha Ar.
"""Cobertura Parte I — Robustez.

Cobre as 4 funcionalidades:
- I/1 Idempotência: webhooks com mesmo provider_message_id são skipados.
- I/2 Inbox único (debounce 20s): a constante DEBOUNCE_SECONDS expõe a janela.
- I/3 Retry com backoff: `_retry_network_call` retenta em erros retriáveis.
- I/4 Healthcheck: /health expõe status/ok/degraded e os tempos.

Todos os testes mockam `repository` e dependências externas pra rodar sem
Postgres nem Evolution API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from sdr_ilha_ar import repository as repo
from sdr_ilha_ar import webhook_api


# ---------------------------------------------------------------------------
# Fixtures comuns
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Reseta estado in-memory entre testes (debounce pendentes + health)."""
    webhook_api._pending_by_phone.clear()
    webhook_api._handoff_cache.clear()
    webhook_api._last_inbound_received_at = None
    webhook_api._last_inbound_processed_at = None
    monkeypatch.setattr(repo, "bootstrap_db_schema", lambda: None)
    monkeypatch.setattr(repo, "ensure_finance_schema", lambda: None)
    # Sem instance restrita nos testes.
    monkeypatch.delenv("EVOLUTION_INSTANCE", raising=False)
    yield
    webhook_api._pending_by_phone.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_api.app)


def _evolution_payload(*, message_id: str, phone: str, text: str) -> dict[str, Any]:
    """Payload Evolution mínimo válido."""
    return {
        "data": {
            "key": {
                "id": message_id,
                "remoteJid": f"{phone}@s.whatsapp.net",
                "fromMe": False,
            },
            "message": {"conversation": text},
            "messageType": "conversation",
            "pushName": "Cliente Teste",
        },
        "instance": "ilha-ar",
    }


# ---------------------------------------------------------------------------
# I/1 — Idempotência
# ---------------------------------------------------------------------------


def test_extract_provider_message_id_evolution():
    payload = _evolution_payload(message_id="ABC123", phone="5598999999999", text="oi")
    assert webhook_api._extract_provider_message_id(payload) == "ABC123"


def test_extract_provider_message_id_vazio_quando_ausente():
    assert webhook_api._extract_provider_message_id({}) == ""
    assert webhook_api._extract_provider_message_id({"data": {}}) == ""


def test_idempotencia_webhook_duplicado_eh_ignorado(monkeypatch, client):
    """I/1 — mensagem duplicada (mesmo provider_id) vira status=ignored."""
    # Primeira mensagem: register_processed_message retorna True (NOVA).
    # Segunda: retorna False (já registrada).
    calls = {"register": []}

    def fake_register(provider_id, lead_id=None):
        calls["register"].append(provider_id)
        # Primeira chamada = True, segunda = False.
        return len(calls["register"]) == 1

    monkeypatch.setattr(repo, "register_processed_message", fake_register)
    monkeypatch.setattr(repo, "mark_message_processed", lambda *a, **k: None)
    # Impede LLM / DB no fluxo downstream:
    async def fake_handle(payload):
        return {"reply": "", "delivery": "skipped"}

    monkeypatch.setattr(webhook_api, "handle_evolution_inbound", fake_handle)
    # Evita roteamento de inbox único (remote_jid/phone ficam vazios → caminho skipped).
    monkeypatch.setattr(
        webhook_api,
        "parse_evolution_inbound",
        lambda body: {
            "phone": "",
            "raw_remote_jid": "",
            "evolution_instance": "ilha-ar",
            "external_channel": "whatsapp",
            "has_audio": False,
            "text": "oi",
        },
    )

    payload = _evolution_payload(message_id="DUPE-1", phone="5598999999999", text="oi")

    r1 = client.post("/webhook/whatsapp", json=payload)
    r2 = client.post("/webhook/whatsapp", json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200
    body1 = r1.json()
    body2 = r2.json()
    # Primeira é processada; segunda é ignorada por duplicata.
    assert body2.get("reason") == "duplicate_provider_message_id"
    assert body2.get("provider_message_id") == "DUPE-1"
    assert calls["register"] == ["DUPE-1", "DUPE-1"]


def test_idempotencia_falha_banco_nao_bloqueia_fluxo(monkeypatch, client):
    """I/1 — se register_processed_message explode, webhook continua (fallback)."""

    def boom(*a, **kw):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(repo, "register_processed_message", boom)
    monkeypatch.setattr(repo, "mark_message_processed", lambda *a, **k: None)

    async def fake_handle(payload):
        return {"reply": "ok", "delivery": "skipped"}

    monkeypatch.setattr(webhook_api, "handle_evolution_inbound", fake_handle)
    monkeypatch.setattr(
        webhook_api,
        "parse_evolution_inbound",
        lambda body: {
            "phone": "",
            "raw_remote_jid": "",
            "evolution_instance": "ilha-ar",
            "external_channel": "whatsapp",
            "has_audio": False,
            "text": "oi",
        },
    )

    payload = _evolution_payload(message_id="X", phone="5598", text="oi")
    r = client.post("/webhook/whatsapp", json=payload)
    # Mesmo com falha no idempotency, não derruba a rota.
    assert r.status_code == 200
    assert r.json().get("reason") != "duplicate_provider_message_id"


# ---------------------------------------------------------------------------
# I/2 — Inbox único (debounce 20s)
# ---------------------------------------------------------------------------


def test_debounce_seconds_eh_20():
    """I/2 — janela de debounce pactuada com o Brendo é 20s."""
    assert webhook_api.DEBOUNCE_SECONDS == 20


def test_enqueue_payload_agrega_multiplas_mensagens_na_mesma_conversa(monkeypatch):
    """I/2 — enqueue 2 payloads do mesmo lead coalesce num único PendingConversation."""
    # Não esperamos o debounce real — só verificamos a agregação in-memory.
    captured = {"tasks": []}

    def fake_create_task(coro):
        # Evita rodar o coro de verdade (tem asyncio.sleep(20)).
        coro.close()
        class _T:
            def done(self): return False
            def cancel(self): pass
        t = _T()
        captured["tasks"].append(t)
        return t

    monkeypatch.setattr(webhook_api.asyncio, "create_task", fake_create_task)

    p1 = _evolution_payload(message_id="m1", phone="5598", text="oi")
    p2 = _evolution_payload(message_id="m2", phone="5598", text="tudo bem?")
    webhook_api._enqueue_payload(
        payload=p1,
        remote_jid="5598@s.whatsapp.net",
        phone="5598",
        external_channel="whatsapp",
        evolution_instance="ilha-ar",
    )
    webhook_api._enqueue_payload(
        payload=p2,
        remote_jid="5598@s.whatsapp.net",
        phone="5598",
        external_channel="whatsapp",
        evolution_instance="ilha-ar",
    )
    # Uma única conversa agregou os 2 payloads.
    assert len(webhook_api._pending_by_phone) == 1
    pending = list(webhook_api._pending_by_phone.values())[0]
    assert len(pending.payloads) == 2
    assert pending.message_ids == {"m1", "m2"}


def test_enqueue_payload_dedup_por_message_id(monkeypatch):
    """I/2 — mesmo message_id enviado 2x dentro da janela não duplica no lote."""

    def fake_create_task(coro):
        coro.close()
        class _T:
            def done(self): return False
            def cancel(self): pass
        return _T()

    monkeypatch.setattr(webhook_api.asyncio, "create_task", fake_create_task)

    p = _evolution_payload(message_id="dup", phone="5598", text="oi")
    webhook_api._enqueue_payload(
        payload=p,
        remote_jid="5598@s.whatsapp.net",
        phone="5598",
        external_channel="whatsapp",
        evolution_instance="ilha-ar",
    )
    webhook_api._enqueue_payload(
        payload=p,
        remote_jid="5598@s.whatsapp.net",
        phone="5598",
        external_channel="whatsapp",
        evolution_instance="ilha-ar",
    )
    pending = list(webhook_api._pending_by_phone.values())[0]
    assert len(pending.payloads) == 1
    assert pending.message_ids == {"dup"}


# ---------------------------------------------------------------------------
# I/3 — Retry com backoff
# ---------------------------------------------------------------------------


def test_retriable_network_error_timeout():
    assert webhook_api._is_retriable_network_error(requests.Timeout("slow"))
    assert webhook_api._is_retriable_network_error(requests.ConnectionError("down"))


def test_retriable_network_error_500_sim_400_nao():
    class _Resp:
        def __init__(self, code): self.status_code = code

    e500 = requests.HTTPError("5xx")
    e500.response = _Resp(502)
    e400 = requests.HTTPError("4xx")
    e400.response = _Resp(400)
    assert webhook_api._is_retriable_network_error(e500) is True
    assert webhook_api._is_retriable_network_error(e400) is False


def test_retry_tenta_3_vezes_em_erro_retriavel_e_sucede(monkeypatch):
    """I/3 — em Timeout, retenta 3x e na 3ª retorna o valor."""
    # Não queremos esperar 1+4=5s — mocka sleep.
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(s)

    # O _retry_network_call importa `time` internamente; monkeypatch global:
    import time as _time
    monkeypatch.setattr(_time, "sleep", fake_sleep)

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.Timeout("slow")
        return "ok"

    result = webhook_api._retry_network_call(flaky, op_name="test")
    assert result == "ok"
    assert calls["n"] == 3
    # Entre as 3 tentativas, dormiu 2 vezes (antes da 2ª e antes da 3ª).
    assert sleeps == [1.0, 4.0]


def test_retry_nao_retenta_em_erro_nao_retriavel(monkeypatch):
    """I/3 — ValueError (não-rede) levanta na primeira."""
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ValueError("payload ruim")

    with pytest.raises(ValueError):
        webhook_api._retry_network_call(boom, op_name="test")
    assert calls["n"] == 1


def test_retry_desiste_apos_max_attempts(monkeypatch):
    """I/3 — após 3 tentativas retriable, propaga a última exceção."""
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    calls = {"n": 0}

    def always_timeout():
        calls["n"] += 1
        raise requests.Timeout("never")

    with pytest.raises(requests.Timeout):
        webhook_api._retry_network_call(always_timeout, op_name="test")
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# I/4 — Healthcheck
# ---------------------------------------------------------------------------


def test_health_status_ok_quando_nunca_recebeu_nada(monkeypatch, client):
    """I/4 — sem histórico, status=ok (servidor acabou de subir)."""
    monkeypatch.setattr(
        repo,
        "get_last_processed_message_times",
        lambda: {"last_received_at": None, "last_processed_at": None},
    )
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["last_msg_received_at"] is None
    assert body["last_msg_processed_at"] is None
    assert body["workers_alive"] == 0
    assert body["time_since_last_msg_seconds"] is None


def test_health_status_ok_quando_recente(monkeypatch, client):
    """I/4 — mensagem recente (1 min atrás) → ok."""
    now = datetime.now(timezone.utc)
    webhook_api._last_inbound_received_at = now - timedelta(seconds=60)
    webhook_api._last_inbound_processed_at = now - timedelta(seconds=58)
    monkeypatch.setattr(
        repo,
        "get_last_processed_message_times",
        lambda: {"last_received_at": None, "last_processed_at": None},
    )
    r = client.get("/health")
    body = r.json()
    assert body["status"] == "ok"
    assert body["time_since_last_msg_seconds"] is not None
    assert body["time_since_last_msg_seconds"] > 55


def test_health_status_degraded_apos_10min_em_horario_comercial(monkeypatch, client):
    """I/4 — sem msg há 15min em horário comercial SL → degraded."""
    real_now = datetime.now(timezone.utc)
    webhook_api._last_inbound_received_at = real_now - timedelta(minutes=15)
    webhook_api._last_inbound_processed_at = real_now - timedelta(minutes=15)
    monkeypatch.setattr(
        repo,
        "get_last_processed_message_times",
        lambda: {"last_received_at": None, "last_processed_at": None},
    )
    # Garante o branch de horário comercial independente do relógio real.
    monkeypatch.setattr(webhook_api, "_is_business_hours_sl", lambda ref=None: True)

    r = client.get("/health")
    body = r.json()
    assert body["status"] == "degraded", body
    assert body["business_hours_sl"] is True
    assert body["time_since_last_msg_seconds"] > 600


def test_health_status_ok_fora_do_horario_comercial(monkeypatch, client):
    """I/4 — fora do horário comercial, mesmo com >10min parado, não degrada."""
    real_now = datetime.now(timezone.utc)
    webhook_api._last_inbound_received_at = real_now - timedelta(minutes=30)
    webhook_api._last_inbound_processed_at = real_now - timedelta(minutes=30)
    monkeypatch.setattr(
        repo,
        "get_last_processed_message_times",
        lambda: {"last_received_at": None, "last_processed_at": None},
    )
    monkeypatch.setattr(webhook_api, "_is_business_hours_sl", lambda ref=None: False)

    r = client.get("/health")
    body = r.json()
    assert body["status"] == "ok"
    assert body["business_hours_sl"] is False


def test_is_business_hours_sl_logica():
    """I/4 — 12h UTC = 9h SL (business); 3h UTC = 0h SL (fora)."""
    business = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
    night = datetime(2026, 5, 2, 3, 0, 0, tzinfo=timezone.utc)
    assert webhook_api._is_business_hours_sl(business) is True
    assert webhook_api._is_business_hours_sl(night) is False


def test_health_fallback_para_postgres_quando_memoria_vazia(monkeypatch, client):
    """I/4 — se state in-memory foi perdido (restart), puxa do Postgres."""
    db_received = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
    db_processed = datetime(2026, 5, 2, 12, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(
        repo,
        "get_last_processed_message_times",
        lambda: {"last_received_at": db_received, "last_processed_at": db_processed},
    )

    r = client.get("/health")
    body = r.json()
    assert body["last_msg_received_at"] is not None
    assert "2026-05-02T12:00:00" in body["last_msg_received_at"]
    assert body["last_msg_processed_at"] is not None
    assert "2026-05-02T12:00:05" in body["last_msg_processed_at"]
