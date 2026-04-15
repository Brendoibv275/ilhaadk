# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Transições válidas de estágio do lead (regras no código, não só no LLM)."""

from __future__ import annotations

# Estágios persistidos em leads.stage
STAGES = frozenset(
    {
        "new",
        "qualified",
        "quoted",
        "awaiting_slot",
        "scheduled",
        "completed",
        "lost",
        "emergency_handoff",
    }
)

# origem -> destinos permitidos
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset(
        {"qualified", "quoted", "lost", "emergency_handoff"}
    ),  # quoted: atalho se já cotado no mesmo turno
    "qualified": frozenset({"quoted", "lost", "emergency_handoff"}),
    "quoted": frozenset({"awaiting_slot", "lost", "emergency_handoff"}),
    "awaiting_slot": frozenset({"scheduled", "lost", "emergency_handoff"}),
    "scheduled": frozenset({"completed", "lost", "emergency_handoff"}),
    "completed": frozenset({"lost", "emergency_handoff"}),
    "lost": frozenset({"new", "qualified"}),
    "emergency_handoff": frozenset({"new", "qualified", "quoted"}),
}


def can_transition(from_stage: str, to_stage: str) -> bool:
    if from_stage not in STAGES or to_stage not in STAGES:
        return False
    allowed = VALID_TRANSITIONS.get(from_stage, frozenset())
    return to_stage in allowed


def assert_transition(from_stage: str, to_stage: str) -> None:
    if not can_transition(from_stage, to_stage):
        msg = f"Transição inválida: {from_stage!r} -> {to_stage!r}"
        raise ValueError(msg)
