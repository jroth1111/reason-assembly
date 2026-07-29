"""Worker backend contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class WorkerResult:
    returncode: int
    output: str
    timed_out: bool = False


class WorkerBackend(Protocol):
    def execute(
        self,
        prompt: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> WorkerResult: ...


Argv = Sequence[str]
