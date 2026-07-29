"""Generic argv-based worker backend."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from .backend import WorkerResult


class SubprocessBackend:
    def __init__(self, argv: Sequence[str]):
        if not argv:
            raise ValueError("worker argv must not be empty")
        self.argv = tuple(argv)

    def execute(
        self,
        prompt: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> WorkerResult:
        try:
            result = subprocess.run(
                [*self.argv, prompt],
                cwd=cwd,
                env=dict(env),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = "".join(part for part in (exc.stdout, exc.stderr) if isinstance(part, str))
            return WorkerResult(124, output, True)
        return WorkerResult(result.returncode, result.stdout + result.stderr, False)
