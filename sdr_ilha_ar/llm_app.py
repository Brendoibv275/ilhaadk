# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Agente raiz SDR (triagem / pré-vendas) com tools Postgres + fila."""

from __future__ import annotations

import os

from google.adk.agents import Agent

from sdr_ilha_ar.prompts import INSTRUCTION
from sdr_ilha_ar.tools_impl import (
    book_slot,
    check_availability,
    enqueue_automation_job,
    get_current_datetime,
    get_lead_status,
    get_pricing_quote,
    mark_quote_sent,
    register_appointment_request,
    request_human_handoff,
    save_lead_field,
    set_lead_stage,
)

root_agent = Agent(
    model=os.environ.get("SDR_MODEL", "gemini-3.1-flash-lite-preview"),
    name="ilha_ar_sdr",
    description="Assistente Virtual Ilha Ar — atendimento e qualificação de leads (São Luís).",
    instruction=INSTRUCTION,
    tools=[
        get_current_datetime,
        get_pricing_quote,
        save_lead_field,
        get_lead_status,
        set_lead_stage,
        enqueue_automation_job,
        request_human_handoff,
        mark_quote_sent,
        register_appointment_request,
        check_availability,
        book_slot,
    ],
)
