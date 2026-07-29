"""Redacted structured diagnostics and machine-readable progress."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO

from .artifacts import SecretGuard


class RedactionFilter(logging.Filter):
    def __init__(self, guard: SecretGuard):
        super().__init__()
        self.guard = guard

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.guard.redact_text(record.getMessage())
        record.args = ()
        return True


def get_logger(name: str, *, guard: SecretGuard | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if guard and not any(isinstance(item, RedactionFilter) for item in logger.filters):
        logger.addFilter(RedactionFilter(guard))
    return logger


class ProgressEmitter:
    def __init__(
        self,
        *,
        json_output: bool = False,
        stream: TextIO | None = None,
        guard: SecretGuard | None = None,
    ):
        self.json_output = json_output
        self.stream = stream or sys.stderr
        self.guard = guard or SecretGuard()

    def stage(self, stage: str, status: str, *, run_id: str | None = None, **data: object) -> None:
        payload = {
            "stage": stage,
            "status": status,
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        payload = self.guard.redact(payload)
        if self.json_output:
            print(json.dumps(payload, sort_keys=True), file=self.stream)
        else:
            print(f"{stage}: {status}", file=self.stream)
