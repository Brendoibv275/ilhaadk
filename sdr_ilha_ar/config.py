# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Configuração via variáveis de ambiente."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Parâmetros de runtime do SDR e da fila."""

    database_url: str | None
    app_name: str
    default_external_channel: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    google_review_url: str | None
    db_connect_timeout_seconds: int
    db_connect_retries: int
    db_retry_backoff_seconds: float
    audio_transcribe_model: str
    audio_fetch_timeout_seconds: int

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("DATABASE_URL"),
            app_name=os.environ.get("SDR_APP_NAME", "sdr_ilha_ar"),
            default_external_channel=os.environ.get(
                "SDR_DEFAULT_CHANNEL", "whatsapp"
            ),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            google_review_url=os.environ.get("GOOGLE_REVIEW_URL"),
            db_connect_timeout_seconds=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "5")),
            db_connect_retries=int(os.environ.get("DB_CONNECT_RETRIES", "2")),
            db_retry_backoff_seconds=float(os.environ.get("DB_RETRY_BACKOFF_SECONDS", "0.75")),
            audio_transcribe_model=os.environ.get(
                "SDR_AUDIO_TRANSCRIBE_MODEL", "gemini-3.1-flash-lite-preview"
            ),
            audio_fetch_timeout_seconds=int(os.environ.get("AUDIO_FETCH_TIMEOUT_SECONDS", "15")),
        )


settings = Settings.from_env()
