# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Pacote SDR Ilha Ar. Use `from sdr_ilha_ar.llm_app import root_agent` ou `agent.py` na raiz."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.adk.agents import Agent as AgentType

__all__ = ["root_agent"]


def __getattr__(name: str) -> Any:
    if name == "root_agent":
        from sdr_ilha_ar.llm_app import root_agent as _root

        return _root
    raise AttributeError(name)
