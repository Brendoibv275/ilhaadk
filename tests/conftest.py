# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Configuração de testes: stubs mínimos para rodar sem dependências externas.

Em produção o projeto usa `google-adk`, `psycopg`, etc. A suíte de testes
unitários não precisa das implementações reais — só dos símbolos importados
no topo dos módulos. Este conftest cria stubs leves para os pacotes que
podem não estar disponíveis no ambiente de testes (CI enxuto, etc.), sem
afetar o comportamento em produção (onde os pacotes reais já são importados
antes e os stubs nem entram em ação).
"""

from __future__ import annotations

import sys
import types


def _stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _ensure_google_adk_stub() -> None:
    try:
        import google.adk.tools  # noqa: F401
        import google.adk.agents  # noqa: F401
        return
    except Exception:
        pass

    google_mod = sys.modules.get("google") or types.ModuleType("google")
    adk_mod = types.ModuleType("google.adk")
    tools_mod = types.ModuleType("google.adk.tools")
    agents_mod = types.ModuleType("google.adk.agents")

    class ToolContext:  # pragma: no cover
        pass

    class Agent:  # pragma: no cover
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

    tools_mod.ToolContext = ToolContext
    agents_mod.Agent = Agent
    adk_mod.tools = tools_mod
    adk_mod.agents = agents_mod
    google_mod.adk = adk_mod

    sys.modules.setdefault("google", google_mod)
    sys.modules["google.adk"] = adk_mod
    sys.modules["google.adk.tools"] = tools_mod
    sys.modules["google.adk.agents"] = agents_mod


def _ensure_psycopg_stub() -> None:
    try:
        import psycopg  # noqa: F401
        import psycopg.rows  # noqa: F401
        import psycopg.types.json  # noqa: F401
        return
    except Exception:
        pass

    psycopg_mod = _stub("psycopg")
    rows_mod = _stub("psycopg.rows")
    types_mod = _stub("psycopg.types")
    json_mod = _stub("psycopg.types.json")

    class Connection:  # pragma: no cover
        pass

    class OperationalError(Exception):  # pragma: no cover
        pass

    def connect(*args, **kwargs):  # pragma: no cover
        raise OperationalError("psycopg stub — DB não disponível nos testes")

    def dict_row(*args, **kwargs):  # pragma: no cover
        return None

    class Json:  # pragma: no cover
        def __init__(self, value) -> None:
            self.value = value

    psycopg_mod.Connection = Connection
    psycopg_mod.OperationalError = OperationalError
    psycopg_mod.connect = connect
    psycopg_mod.rows = rows_mod
    psycopg_mod.types = types_mod
    rows_mod.dict_row = dict_row
    types_mod.json = json_mod
    json_mod.Json = Json


def _ensure_dotenv_stub() -> None:
    try:
        import dotenv  # noqa: F401
        return
    except Exception:
        pass
    mod = _stub("dotenv")

    def load_dotenv(*args, **kwargs):  # pragma: no cover
        return False

    mod.load_dotenv = load_dotenv


_ensure_google_adk_stub()
_ensure_psycopg_stub()
_ensure_dotenv_stub()
