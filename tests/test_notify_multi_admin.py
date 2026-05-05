# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Testa multi-admin em ADMIN_WHATSAPP_NUMBER (split por vírgula)."""

from __future__ import annotations

from unittest.mock import patch

from sdr_ilha_ar import notify


def test_single_admin_number_keeps_legacy_shape():
    """Compat: 1 número → retorna dict direto do _send_text_to_destination."""
    with patch.object(notify, "settings") as mock_settings:
        mock_settings.admin_whatsapp_number = "5598984666860"
        with patch.object(
            notify,
            "_send_text_to_destination",
            return_value={"status": "ok", "response": "{}"},
        ) as mock_send:
            result = notify.send_admin_whatsapp_message("teste")
            assert result == {"status": "ok", "response": "{}"}
            mock_send.assert_called_once_with(
                "5598984666860", "teste", instance_hint=""
            )


def test_multi_admin_numbers_sends_to_each():
    """Dois números separados por vírgula → envia pra ambos."""
    with patch.object(notify, "settings") as mock_settings:
        mock_settings.admin_whatsapp_number = "5598984666860,5598985818664"
        with patch.object(
            notify,
            "_send_text_to_destination",
            return_value={"status": "ok", "response": "{}"},
        ) as mock_send:
            result = notify.send_admin_whatsapp_message("oi")
            assert result["status"] == "ok"
            assert result["recipients"] == 2
            assert len(result["results"]) == 2
            assert mock_send.call_count == 2
            called_numbers = {call.args[0] for call in mock_send.call_args_list}
            assert called_numbers == {"5598984666860", "5598985818664"}


def test_multi_admin_partial_failure_still_ok():
    """Se um número falha e outro passa → status=ok."""
    with patch.object(notify, "settings") as mock_settings:
        mock_settings.admin_whatsapp_number = "55988,55999"
        responses = iter([
            {"status": "error", "message": "boom"},
            {"status": "ok"},
        ])
        with patch.object(
            notify,
            "_send_text_to_destination",
            side_effect=lambda *a, **k: next(responses),
        ):
            result = notify.send_admin_whatsapp_message("x")
            assert result["status"] == "ok"
            assert result["recipients"] == 2


def test_multi_admin_all_fail_returns_error():
    """Todos falharam → status=error."""
    with patch.object(notify, "settings") as mock_settings:
        mock_settings.admin_whatsapp_number = "55988,55999"
        with patch.object(
            notify,
            "_send_text_to_destination",
            return_value={"status": "error", "message": "boom"},
        ):
            result = notify.send_admin_whatsapp_message("x")
            assert result["status"] == "error"
            assert result["recipients"] == 2


def test_no_admin_configured_skipped():
    """Vazio → skipped, nada enviado."""
    with patch.object(notify, "settings") as mock_settings:
        mock_settings.admin_whatsapp_number = None
        with patch.object(notify, "_send_text_to_destination") as mock_send:
            result = notify.send_admin_whatsapp_message("x")
            assert result["status"] == "skipped"
            mock_send.assert_not_called()


def test_whitespace_and_empty_entries_stripped():
    """Entradas com espaço/vazias são ignoradas."""
    with patch.object(notify, "settings") as mock_settings:
        mock_settings.admin_whatsapp_number = " 55988 , , 55999 ,"
        with patch.object(
            notify,
            "_send_text_to_destination",
            return_value={"status": "ok"},
        ) as mock_send:
            result = notify.send_admin_whatsapp_message("x")
            assert result["recipients"] == 2
            called = {call.args[0] for call in mock_send.call_args_list}
            assert called == {"55988", "55999"}
