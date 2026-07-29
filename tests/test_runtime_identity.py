from __future__ import annotations

import json
import os
import stat
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from identity import (
    METADATA_CLIENT_VERSION,
    PRODUCT_NAME,
    PRODUCT_SLUG,
    RELEASE_TAG,
    SESSION_NAMESPACE,
    STATE_ENV,
    USER_AGENT,
    VERSION,
)
from state_compat import resolve_state_root
from v4_state import PrivateJsonStore, initialize_v4_state


ROOT = Path(__file__).resolve().parents[1]


def test_canonical_identity_is_reason_assembly_060():
    assert PRODUCT_NAME == "Reason Assembly"
    assert PRODUCT_SLUG == "reason-assembly"
    assert VERSION == "0.6.0"
    assert USER_AGENT == "reason-assembly/0.6.0"
    assert RELEASE_TAG == "v0.6.0"
    assert SESSION_NAMESPACE == "reason-assembly:v4"
    assert METADATA_CLIENT_VERSION == "reason-assembly-v4"


def test_canonical_command_reports_version():
    completed = subprocess.run(
        [str(ROOT / "bin" / "reason-assembly"), "--version"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": "", "UV_FROZEN": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "reason-assembly 0.6.0"
    assert completed.stderr == ""


def test_state_environment_is_canonical_only(tmp_path):
    canonical = tmp_path / "canonical"
    assert resolve_state_root({STATE_ENV: str(canonical)}) == canonical
    retired_state_key = "CCY" + "COUNCIL_STATE"
    assert resolve_state_root({retired_state_key: str(tmp_path / "retired")}, home=tmp_path) == (
        tmp_path / ".local" / "state" / "reason-assembly"
    )


def test_release_identity_matches_package_and_lock():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    root_package = next(
        package
        for package in lock["package"]
        if package.get("source") in ({"virtual": "."}, {"editable": "."})
    )
    assert project["name"] == root_package["name"] == PRODUCT_SLUG
    assert project["version"] == root_package["version"] == VERSION


def test_v4_initialization_never_overwrites_existing_state(tmp_path):
    root = tmp_path / "v4"
    existing = {"schema_version": 4, "examples": [{"run_id": "preserve-me"}]}
    calibration = PrivateJsonStore(root / "calibration.json", {})
    calibration.write(existing)
    initialize_v4_state(root)
    assert calibration.read() == existing
    assert (root / "reliability.json").is_file()


def test_concurrent_v4_initialization_is_guarded(tmp_path):
    root = tmp_path / "v4"
    barrier = Barrier(8)

    def initialize():
        barrier.wait()
        initialize_v4_state(root)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: initialize(), range(8)))

    expected = {
        "reliability.json",
        "cofailure.json",
        "calibration.json",
        "operation-effects.json",
        "anchors.json",
        "route-epochs.json",
    }
    names = {path.name for path in root.iterdir()}
    assert expected <= names
    assert {f"{name}.lock" for name in expected} <= names
    for name in expected:
        path = root / name
        assert isinstance(json.loads(path.read_text()), dict)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
