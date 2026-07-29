"""Offline demonstration run with no provider or network access."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import RunStore, SecretGuard, sha256_text
from .contracts import RunManifest, Verdict
from .protocols import ProtocolResult, new_run_id


def run_demo(mode: str, prompt: str) -> ProtocolResult:
    root = Path(tempfile.mkdtemp(prefix="reason-assembly-demo-"))
    run_id = new_run_id()
    store = RunStore(root, run_id, SecretGuard())
    verdict = Verdict(
        decision="Demo result: compare evidence, preserve uncertainty, and verify before acting.",
        rationale=["Generated entirely offline from bundled deterministic demo logic."],
        confidence=0.5,
        action="synthesize",
        finality="verdict_commit",
        calibrated=False,
    )
    manifest = RunManifest(
        run_id=run_id,
        mode=mode,
        budget="quick",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        status="completed",
        prompt_sha256=sha256_text(prompt),
        call_cap=0,
        calls_used=0,
        finality="verdict_commit",
        calibrated=False,
    )
    store.write_json("verdict.json", verdict)
    manifest.artifacts = store.artifact_names()
    store.write_json("manifest.json", manifest)
    store.seal_manifest()
    manifest = RunManifest.model_validate(store.read_json("manifest.json"))
    return ProtocolResult(run_id, verdict, [], manifest)
