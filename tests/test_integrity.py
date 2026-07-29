from datetime import datetime, timezone

from artifacts import RunStore, SecretGuard
from contracts import RunManifest


def make_store(tmp_path):
    store = RunStore(tmp_path, "demo-run", SecretGuard())
    store.write_json("verdict.json", {"decision": "safe"})
    manifest = RunManifest(
        run_id="demo-run",
        mode="decide",
        budget="quick",
        created_at=datetime.now(timezone.utc),
        status="completed",
        prompt_sha256="0" * 64,
        call_cap=0,
    )
    store.write_json("manifest.json", manifest)
    return store


def test_seal_verify_and_detect_tamper(tmp_path):
    store = make_store(tmp_path)
    assert store.seal_manifest()
    assert store.verify_integrity() == []
    store.write_json("verdict.json", {"decision": "changed"})
    assert store.verify_integrity() == ["verdict.json"]


def test_unsealed_manifest_is_compatible(tmp_path):
    assert make_store(tmp_path).verify_integrity() == ["manifest not sealed"]
