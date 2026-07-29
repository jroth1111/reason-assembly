from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

import reason_assembly
import state_compat
from artifacts import RunStore, SecretGuard
from conftest import FakeTransport
from identity import (
    LEGACY_CLI,
    LEGACY_EPHEMERAL_KEY_ENV,
    METADATA_CLIENT_VERSION,
    PRODUCT_NAME,
    PRODUCT_SLUG,
    RELEASE_TAG,
    SESSION_NAMESPACE,
    USER_AGENT,
    VERSION,
)
from protocols import CouncilEngine, CouncilRequest
from state_compat import (
    LEGACY_STATE_ENV,
    STATE_ENV,
    compatible_state_roots,
    iter_run_roots,
    locate_run_root,
    migrate_legacy_state,
    resolve_state_root,
)
from transport import ProxyTransport, proxy_config_path_from_env
from v4_state import PrivateJsonStore, initialize_v4_state


ROOT = Path(__file__).resolve().parents[1]


def completed_manifest() -> str:
    return json.dumps(
        {
            "schema_version": 4,
            "status": "completed",
            "completed_at": "2026-07-28T00:00:00+00:00",
        }
    ) + "\n"


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_canonical_identity_is_reason_assembly_051():
    assert PRODUCT_NAME == "Reason Assembly"
    assert PRODUCT_SLUG == "reason-assembly"
    assert VERSION == "0.5.1"
    assert LEGACY_CLI == "ccycouncil"
    assert USER_AGENT == "reason-assembly/0.5.1"
    assert SESSION_NAMESPACE == "reason-assembly:v4"
    assert METADATA_CLIENT_VERSION == "reason-assembly-v4"
    assert reason_assembly.parser().prog == "reason-assembly"


def test_canonical_and_deprecated_commands_report_canonical_version():
    canonical = subprocess.run(
        [str(ROOT / "bin" / "reason-assembly"), "--version"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": "", "UV_FROZEN": "1"},
    )
    assert canonical.returncode == 0, canonical.stderr
    assert canonical.stdout.strip() == "reason-assembly 0.5.1"
    assert canonical.stderr == ""

    legacy = subprocess.run(
        [str(ROOT / "bin" / "ccycouncil"), "--version"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": "", "UV_FROZEN": "1"},
    )
    assert legacy.returncode == 0, legacy.stderr
    assert legacy.stdout.strip() == "reason-assembly 0.5.1"
    assert "deprecated" in legacy.stderr.lower()
    assert "reason-assembly" in legacy.stderr


def test_state_environment_prefers_canonical_and_accepts_legacy(tmp_path):
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    assert resolve_state_root({STATE_ENV: str(canonical), LEGACY_STATE_ENV: str(legacy)}) == canonical
    assert resolve_state_root({LEGACY_STATE_ENV: str(legacy)}) == legacy


def test_legacy_run_discovery_remains_active_with_canonical_precedence(tmp_path):
    canonical = tmp_path / "reason-assembly"
    legacy = tmp_path / "ccycouncil"
    for root, run_ids in (
        (canonical, ("canonical-only", "shared")),
        (legacy, ("legacy-only", "shared")),
    ):
        for run_id in run_ids:
            (root / "runs" / run_id).mkdir(parents=True)

    assert locate_run_root(canonical, "legacy-only", legacy=legacy) == legacy
    assert locate_run_root(canonical, "shared", legacy=legacy) == canonical
    assert dict(iter_run_roots(canonical, legacy=legacy)) == {
        "canonical-only": canonical,
        "legacy-only": legacy,
        "shared": canonical,
    }


def test_proxy_config_environment_prefers_product_namespace_and_accepts_adapter(tmp_path):
    canonical = tmp_path / "reason-assembly-config.yaml"
    adapter = tmp_path / "ccyproxy-config.yaml"
    assert proxy_config_path_from_env(
        {
            "REASON_ASSEMBLY_PROXY_CONFIG": str(canonical),
            "CCYPROXY_CONFIG": str(adapter),
        }
    ) == canonical
    assert proxy_config_path_from_env({"CCYPROXY_CONFIG": str(adapter)}) == adapter


def test_transport_sync_receipts_follow_configured_application_state(
    tmp_path, fake_settings, monkeypatch
):
    configured = tmp_path / "private-state"
    monkeypatch.setenv(STATE_ENV, str(configured))
    transport = ProxyTransport(fake_settings)
    try:
        assert transport.sync_state_root == configured / "v4"
    finally:
        asyncio.run(transport.close())


def test_council_engine_routes_sync_receipts_to_selected_state(
    tmp_path, fake_settings
):
    configured = tmp_path / "programmatic-state"

    class RecordingTransport(FakeTransport):
        def __init__(self, settings, *, budget=None, sync_state_root=None, **kwargs):
            super().__init__(settings, budget=budget, **kwargs)
            self.sync_state_root = sync_state_root

    engine = CouncilEngine(
        CouncilRequest(mode="decide", prompt="Verify state routing."),
        state=configured,
        settings=fake_settings,
        transport_factory=RecordingTransport,
    )

    assert engine.transport.sync_state_root == configured.resolve() / "v4"


def test_legacy_state_copy_is_non_destructive_collision_safe_and_idempotent(tmp_path):
    legacy = tmp_path / "ccycouncil"
    canonical = tmp_path / "reason-assembly"
    legacy_run = legacy / "runs" / "legacy-run"
    legacy_run.mkdir(parents=True)
    (legacy_run / "manifest.json").write_text(completed_manifest())
    (legacy / "v4").mkdir()
    (legacy / "v4" / "calibration.json").write_text('{"schema_version": 4}\n')
    os.chmod(legacy_run, 0o700)
    os.chmod(legacy_run / "manifest.json", 0o600)

    canonical_collision = canonical / "v4" / "calibration.json"
    canonical_collision.parent.mkdir(parents=True)
    canonical_collision.write_text("canonical-wins\n")
    before = tree_hash(legacy)

    first = migrate_legacy_state(canonical, legacy)
    second = migrate_legacy_state(canonical, legacy)

    assert first.copied_files >= 1
    assert second.copied_files == 0
    assert canonical_collision.read_text() == "canonical-wins\n"
    assert (canonical / "runs" / "legacy-run" / "manifest.json").read_text() == (
        legacy_run / "manifest.json"
    ).read_text()
    assert tree_hash(legacy) == before
    assert stat.S_IMODE((canonical / "runs" / "legacy-run").stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (canonical / "runs" / "legacy-run" / "manifest.json").stat().st_mode
    ) == 0o600
    assert locate_run_root(canonical, "legacy-run", legacy=legacy) == canonical
    assert compatible_state_roots(canonical, legacy=legacy) == [canonical, legacy]


def test_incomplete_legacy_run_is_discovered_but_not_imported_until_terminal(tmp_path):
    legacy = tmp_path / "ccycouncil"
    canonical = tmp_path / "reason-assembly"
    legacy_run = legacy / "runs" / "live-run"
    legacy_run.mkdir(parents=True)
    (legacy_run / "manifest.json").write_text(
        json.dumps({"schema_version": 4, "status": "running"}) + "\n"
    )

    first = migrate_legacy_state(canonical, legacy)

    assert first.skipped_incomplete == 1
    assert not (canonical / "runs" / "live-run").exists()
    assert locate_run_root(canonical, "live-run", legacy=legacy) == legacy

    (legacy_run / "manifest.json").write_text(completed_manifest())
    second = migrate_legacy_state(canonical, legacy)

    assert second.errors == []
    assert (canonical / "runs" / "live-run" / "manifest.json").is_file()


def test_legacy_run_change_during_staging_aborts_atomic_import(
    tmp_path, monkeypatch
):
    legacy = tmp_path / "ccycouncil"
    canonical = tmp_path / "reason-assembly"
    legacy_run = legacy / "runs" / "changing-run"
    legacy_run.mkdir(parents=True)
    (legacy_run / "manifest.json").write_text(completed_manifest())
    verdict = legacy_run / "verdict.json"
    verdict.write_text("original\n")
    original_copy = state_compat._copy_file_without_overwrite
    mutated = False

    def copy_then_mutate(source, target):
        nonlocal mutated
        copied = original_copy(source, target)
        if not mutated:
            verdict.write_text("changed while migration was staging the run\n")
            mutated = True
        return copied

    monkeypatch.setattr(
        state_compat,
        "_copy_file_without_overwrite",
        copy_then_mutate,
    )
    result = migrate_legacy_state(canonical, legacy)

    assert any(error.startswith("run-changed:changing-run:") for error in result.errors)
    assert not (canonical / "runs" / "changing-run").exists()
    assert locate_run_root(canonical, "changing-run", legacy=legacy) == legacy


def test_special_legacy_artifact_is_rejected_without_partial_import(tmp_path):
    legacy = tmp_path / "ccycouncil"
    canonical = tmp_path / "reason-assembly"
    legacy_run = legacy / "runs" / "special-run"
    legacy_run.mkdir(parents=True)
    (legacy_run / "manifest.json").write_text(completed_manifest())
    os.mkfifo(legacy_run / "unexpected.pipe", mode=0o600)

    result = migrate_legacy_state(canonical, legacy)

    assert any("UnsupportedFileType" in error for error in result.errors)
    assert not (canonical / "runs" / "special-run").exists()
    assert locate_run_root(canonical, "special-run", legacy=legacy) == legacy


def test_changed_legacy_import_falls_back_read_only_without_shadowing(tmp_path, monkeypatch):
    legacy = tmp_path / "ccycouncil"
    canonical = tmp_path / "reason-assembly"
    legacy_run = legacy / "runs" / "completed-run"
    legacy_run.mkdir(parents=True)
    (legacy_run / "manifest.json").write_text(completed_manifest())
    verdict = legacy_run / "verdict.json"
    verdict.write_text("original\n")
    assert migrate_legacy_state(canonical, legacy).errors == []

    original_stat = verdict.stat()
    verdict.write_text("changed!\n")
    os.utime(
        verdict,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert locate_run_root(canonical, "completed-run", legacy=legacy) == legacy
    assert dict(iter_run_roots(canonical, legacy=legacy))["completed-run"] == legacy
    rerun = migrate_legacy_state(canonical, legacy)
    assert any(error.startswith("run-stale:completed-run:") for error in rerun.errors)

    monkeypatch.setattr(reason_assembly, "STATE", canonical)
    monkeypatch.setenv(STATE_ENV, str(canonical))
    monkeypatch.setenv(LEGACY_STATE_ENV, str(legacy))
    legacy_store = reason_assembly.open_store("completed-run")
    before = tree_hash(legacy_run)
    before_mode = stat.S_IMODE(legacy_run.stat().st_mode)

    assert legacy_store.root == legacy.resolve()
    assert legacy_store.read_json("manifest.json")["status"] == "completed"
    assert not legacy_store._target("missing/artifact.json").exists()
    assert not (legacy_run / "missing").exists()
    with pytest.raises(RuntimeError, match="read-only"):
        reason_assembly.require_writable_canonical_store(legacy_store)

    monkeypatch.setattr(
        reason_assembly,
        "load_v4_run",
        lambda _run_id: (legacy_store, SimpleNamespace()),
    )
    with pytest.raises(RuntimeError, match="read-only"):
        reason_assembly.outcome_command(SimpleNamespace(run_id="completed-run"))

    assert tree_hash(legacy_run) == before
    assert stat.S_IMODE(legacy_run.stat().st_mode) == before_mode


def test_custom_state_is_isolated_unless_legacy_override_is_explicit(
    tmp_path, monkeypatch
):
    custom = tmp_path / "custom"
    default_legacy = tmp_path / "default-legacy"
    explicit_legacy = tmp_path / "explicit-legacy"
    (default_legacy / "runs" / "global-run").mkdir(parents=True)
    (explicit_legacy / "runs" / "explicit-run").mkdir(parents=True)
    monkeypatch.setattr(state_compat, "default_legacy_state_root", lambda: default_legacy)
    monkeypatch.setenv(STATE_ENV, str(custom))
    monkeypatch.delenv(LEGACY_STATE_ENV, raising=False)

    assert compatible_state_roots(custom) == [custom.resolve()]
    assert locate_run_root(custom, "global-run") == custom.resolve()

    monkeypatch.setenv(LEGACY_STATE_ENV, str(explicit_legacy))
    assert compatible_state_roots(custom) == [
        custom.resolve(),
        explicit_legacy.resolve(),
    ]
    assert locate_run_root(custom, "explicit-run") == explicit_legacy.resolve()


def test_atomic_run_store_reservation_retries_all_visible_collisions(tmp_path):
    guard = SecretGuard()
    canonical = tmp_path / "reason-assembly"
    legacy = tmp_path / "ccycouncil"
    existing = RunStore(canonical, "canonical-run", guard)
    existing.write_text("manifest.json", "canonical evidence\n")
    (legacy / "runs" / "legacy-run").mkdir(parents=True)
    candidates = iter(("canonical-run", "legacy-run", "fresh-run"))

    reserved = RunStore.create_unique(
        canonical,
        guard,
        run_id_factory=lambda: next(candidates),
        collision_roots=(canonical, legacy),
    )

    assert reserved.run_id == "fresh-run"
    assert existing._target("manifest.json").read_text() == "canonical evidence\n"
    assert (legacy / "runs" / "legacy-run").is_dir()


def test_v4_initialization_never_overwrites_existing_state(tmp_path):
    root = tmp_path / "v4"
    existing = {
        "schema_version": 4,
        "examples": [{"run_id": "preserve-me"}],
    }
    calibration = PrivateJsonStore(root / "calibration.json", {})
    calibration.write(existing)

    initialize_v4_state(root)

    assert calibration.read() == existing
    assert (root / "reliability.json").is_file()


def test_v4_initialization_publishes_once_under_concurrency(tmp_path):
    default = {"schema_version": 4, "examples": []}
    store = PrivateJsonStore(tmp_path / "v4" / "calibration.json", default)
    barrier = Barrier(8)

    def initialize() -> bool:
        barrier.wait()
        return store.initialize(default)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: initialize(), range(8)))

    assert results.count(True) == 1
    assert store.read() == default


def test_concurrent_v4_initialization_is_guarded(tmp_path):
    root = tmp_path / "v4"
    workers = 8
    barrier = Barrier(workers)

    def initialize():
        barrier.wait()
        initialize_v4_state(root)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(lambda _index: initialize(), range(workers)))

    expected = {
        "reliability.json",
        "cofailure.json",
        "calibration.json",
        "operation-effects.json",
        "anchors.json",
        "route-epochs.json",
    }
    assert {path.name for path in root.iterdir()} == expected
    for path in root.iterdir():
        assert isinstance(json.loads(path.read_text()), dict)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_interrupted_run_import_never_shadows_complete_legacy_evidence(
    tmp_path, monkeypatch
):
    legacy = tmp_path / "ccycouncil"
    canonical = tmp_path / "reason-assembly"
    legacy_run = legacy / "runs" / "legacy-run"
    legacy_run.mkdir(parents=True)
    (legacy_run / "manifest.json").write_text(completed_manifest())
    (legacy_run / "verdict.json").write_text("verdict\n")
    original_copy = state_compat._copy_file_without_overwrite
    calls = 0

    def fail_second_copy(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic interrupted copy")
        return original_copy(source, target)

    monkeypatch.setattr(
        state_compat, "_copy_file_without_overwrite", fail_second_copy
    )
    failed = migrate_legacy_state(canonical, legacy)

    assert failed.errors
    assert not (canonical / "runs" / "legacy-run").exists()
    assert locate_run_root(canonical, "legacy-run", legacy=legacy) == legacy
    staging_root = canonical / state_compat.STAGING_DIRECTORY
    assert not staging_root.exists() or not any(staging_root.iterdir())
    failed_marker = json.loads(
        (canonical / state_compat.MIGRATION_MARKER).read_text()
    )
    assert failed_marker["complete"] is False
    assert failed_marker["status"] == "incomplete"
    assert "completed_at" not in failed_marker

    monkeypatch.setattr(
        state_compat, "_copy_file_without_overwrite", original_copy
    )
    completed = migrate_legacy_state(canonical, legacy)

    assert completed.errors == []
    assert (canonical / "runs" / "legacy-run" / "manifest.json").read_text() == (
        completed_manifest()
    )
    assert (canonical / "runs" / "legacy-run" / "verdict.json").read_text() == (
        "verdict\n"
    )
    completed_marker = json.loads(
        (canonical / state_compat.MIGRATION_MARKER).read_text()
    )
    assert completed_marker["complete"] is True
    assert completed_marker["status"] == "complete"
    assert "completed_at" in completed_marker


def test_colliding_run_ids_are_never_merged(tmp_path):
    legacy = tmp_path / "ccycouncil"
    canonical = tmp_path / "reason-assembly"
    legacy_run = legacy / "runs" / "same-run"
    canonical_run = canonical / "runs" / "same-run"
    legacy_run.mkdir(parents=True)
    canonical_run.mkdir(parents=True)
    (legacy_run / "verdict.json").write_text("legacy-only\n")
    (legacy_run / "private").mkdir()
    (legacy_run / "private" / "identity-map.json").write_text("legacy-private\n")
    (canonical_run / "manifest.json").write_text("canonical-only\n")
    canonical_before = tree_hash(canonical_run)

    result = migrate_legacy_state(canonical, legacy)

    assert result.skipped_existing >= 1
    assert tree_hash(canonical_run) == canonical_before
    assert (canonical_run / "manifest.json").read_text() == "canonical-only\n"
    assert not (canonical_run / "verdict.json").exists()
    assert not (canonical_run / "private").exists()
    assert locate_run_root(canonical, "same-run", legacy=legacy) == canonical


def test_missing_legacy_root_refreshes_prior_incomplete_marker(tmp_path):
    canonical = tmp_path / "reason-assembly"
    canonical.mkdir()
    marker = canonical / state_compat.MIGRATION_MARKER
    marker.write_text('{"schema_version":1,"status":"incomplete"}\n')

    result = migrate_legacy_state(canonical, tmp_path / "missing-legacy")

    assert result.errors == []
    refreshed = json.loads(marker.read_text())
    assert refreshed["status"] == "no_legacy_source"
    assert refreshed["legacy_available"] is False
    assert refreshed["complete"] is True


def test_invalid_state_path_is_reported_without_traceback(tmp_path):
    invalid = tmp_path / "state-file"
    invalid.write_text("not a directory\n")
    result = subprocess.run(
        [str(ROOT / "bin" / "reason-assembly"), "stats"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "REASON_ASSEMBLY_STATE": str(invalid), "PYTHONPATH": ""},
    )
    assert result.returncode == 2
    assert "reason-assembly:" in result.stderr
    assert "Traceback" not in result.stderr


def test_empty_stats_reports_a_stable_zero_result_without_traceback(tmp_path):
    state = tmp_path / "empty-state"
    environment = {
        **os.environ,
        "REASON_ASSEMBLY_STATE": str(state),
        "PYTHONPATH": "",
        "UV_FROZEN": "1",
    }

    human = subprocess.run(
        [str(ROOT / "bin" / "reason-assembly"), "stats"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert human.returncode == 0, human.stderr
    assert human.stdout == "runs_with_observed_outcomes=0\n"
    assert human.stderr == ""

    machine = subprocess.run(
        [str(ROOT / "bin" / "reason-assembly"), "stats", "--json"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert machine.returncode == 0, machine.stderr
    payload = json.loads(machine.stdout)
    assert payload["runs_with_observed_outcomes"] == 0
    assert payload["routing_changed"] is False
    assert payload["reliability_diagnostics"] == {}
    assert payload["reliability"]["buckets"] == []
    assert payload["model"] == {}
    assert payload["family"] == {}
    assert payload["role"] == {}
    assert payload["domain"] == {}
    assert payload["mode"] == {}


def test_release_identity_is_consistent_across_all_release_surfaces():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    root_package = next(
        package
        for package in lock["package"]
        if package.get("source") in ({"virtual": "."}, {"editable": "."})
    )
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert project["name"] == PRODUCT_SLUG == "reason-assembly"
    assert project["version"] == VERSION == "0.5.1"
    assert root_package["name"] == PRODUCT_SLUG
    assert root_package["version"] == VERSION
    assert f"## {VERSION} - " in changelog
    assert RELEASE_TAG == "v0.5.1"

    for command in ("reason-assembly", "ccycouncil"):
        completed = subprocess.run(
            [str(ROOT / "bin" / command), "--version"],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "PYTHONPATH": "", "UV_FROZEN": "1"},
        )
        assert completed.returncode == 0
        assert completed.stdout.strip() == f"{PRODUCT_SLUG} {VERSION}"


def test_no_stale_canonical_identity_tokens_remain():
    forbidden = (
        "ccycouncil/0.4.1",
        "ccycouncil-v4",
        "ccycouncil:v4",
        "scripts/ccycouncil.py",
        "from ccycouncil import",
        "import ccycouncil",
        "ccycouncil-codex-",
        "ccycouncil-pycache-",
        "ccycouncil@local",
    )
    surfaces = [
        ROOT / "pyproject.toml",
        ROOT / "SKILL.md",
        ROOT / "agents" / "openai.yaml",
        ROOT / "references" / "protocol.md",
        *sorted((ROOT / "scripts").glob("*.py")),
    ]

    for path in surfaces:
        text = path.read_text()
        for token in forbidden:
            assert token not in text, f"stale identity token {token!r} in {path}"


def test_skill_identity_is_canonical_with_explicit_deprecated_alias():
    skill = (ROOT / "SKILL.md").read_text()
    manifest = (ROOT / "agents" / "openai.yaml").read_text()
    alias_root = ROOT / "compat" / "model-council"
    alias = (alias_root / "SKILL.md").read_text()
    alias_manifest = (alias_root / "agents" / "openai.yaml").read_text()

    assert "name: reason-assembly" in skill
    assert "$reason-assembly" in skill
    assert "reason-assembly" in manifest
    assert "Reason Assembly" in manifest
    assert "name: model-council" in alias
    assert "deprecated" in alias.lower()
    assert "$reason-assembly" in alias
    assert "Model Council (deprecated)" in alias_manifest
    assert "allow_implicit_invocation: false" in alias_manifest
    assert LEGACY_EPHEMERAL_KEY_ENV in (ROOT / "README.md").read_text()


def test_gitignore_protects_canonical_and_legacy_private_state():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".reason-assembly/" in ignored
    assert ".ccycouncil/" in ignored
