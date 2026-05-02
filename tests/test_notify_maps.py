# Copyright 2025 Ilha Ar.
"""Testes para I1 — link do Google Maps no resumo enviado ao grupo interno."""

from __future__ import annotations

from decimal import Decimal

from sdr_ilha_ar.notify import _build_maps_link, format_lead_notification


def _base_lead() -> dict:
    return {
        "display_name": "João Teste",
        "phone": "+5548999999999",
        "external_user_id": "5548999999999@s.whatsapp.net",
        "service_type": "manutencao",
        "address": None,
        "preferred_window": "manhã",
        "stage": "qualified",
        "latitude": None,
        "longitude": None,
    }


def test_build_maps_link_with_exact_coords() -> None:
    lead = _base_lead()
    lead["latitude"] = -27.5954
    lead["longitude"] = -48.5480
    link = _build_maps_link(lead)
    assert link == "📍 Localização: https://www.google.com/maps?q=-27.5954,-48.548"


def test_build_maps_link_with_decimal_coords() -> None:
    lead = _base_lead()
    lead["latitude"] = Decimal("-27.5954")
    lead["longitude"] = Decimal("-48.5480")
    link = _build_maps_link(lead)
    assert link.startswith("📍 Localização: https://www.google.com/maps?q=-27.5954")


def test_build_maps_link_falls_back_to_address_text() -> None:
    lead = _base_lead()
    lead["address"] = "Rua das Flores, 123, Florianópolis"
    link = _build_maps_link(lead)
    assert link.startswith("📍 Endereço (por texto, confirmar): https://www.google.com/maps?q=")
    # urlencoded — espaço vira '+', vírgula vira %2C
    assert "Rua+das+Flores%2C+123%2C+Florian" in link


def test_build_maps_link_when_nothing_informed() -> None:
    lead = _base_lead()
    assert _build_maps_link(lead) == "📍 Localização: não informada"


def test_format_lead_notification_includes_maps_line() -> None:
    lead = _base_lead()
    lead["latitude"] = -27.5954
    lead["longitude"] = -48.5480
    text = format_lead_notification("Novo lead", lead)
    assert "📍 Localização: https://www.google.com/maps?q=-27.5954,-48.548" in text
    # Linha deve estar presente no texto final
    lines = text.split("\n")
    assert any(l.startswith("📍") for l in lines)
