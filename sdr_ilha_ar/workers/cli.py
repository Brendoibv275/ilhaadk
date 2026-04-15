# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""CLI: `python -m sdr_ilha_ar.workers tick` (cron a cada 1–5 min)."""

from __future__ import annotations

import argparse
import logging
import sys

from sdr_ilha_ar.repository import DatabaseNotConfiguredError
from sdr_ilha_ar.workers.processor import run_tick

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Workers da fila automation_jobs")
    p.add_argument(
        "command",
        choices=["tick"],
        help="tick: processa jobs com run_at <= agora",
    )
    p.add_argument("--limit", type=int, default=20)
    args = p.parse_args(argv)
    if args.command == "tick":
        try:
            n = run_tick(limit=args.limit)
            logger.info("Processados %s job(s).", n)
        except DatabaseNotConfiguredError as e:
            logger.error("%s", e)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
