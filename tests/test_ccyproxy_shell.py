from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ccyproxy-sync.zsh"


def ccyproxy_source() -> str:
    return EXAMPLE.read_text()


def test_ccyproxy_sync_precedes_route_resolution_and_raw_filtering():
    source = ccyproxy_source()
    assert source.index("ccyproxy_catalogue_sync") < source.index("Fetch /v1/models here")
    assert "command reason-assembly sync --json" in source
    assert ("ccy" + "council") not in source
    assert "intersect every alias candidate with the raw" in source
    assert "Never select a candidate on alias" in source


def test_ccyproxy_shell_definition_is_valid_zsh():
    if shutil.which("zsh") is None:
        pytest.skip("zsh is not installed")
    result = subprocess.run(
        ["zsh", "-n"],
        input=ccyproxy_source(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_ccyproxy_sync_executes_canonical_command(tmp_path):
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "sync.log"
    canonical = bin_dir / "reason-assembly"
    canonical.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "printf 'reason-assembly %s config=%s\\n' \"$*\" \"${CCYPROXY_CONFIG:-}\" >>\"$SYNC_LOG\"\n"
    )
    canonical.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "SYNC_LOG": str(log),
        "CCYPROXY_CONFIG": "test-proxy-config.yaml",
    }

    result = subprocess.run(
        [zsh, "-f", "-c", 'source "$1"; ccyproxy_catalogue_sync', "test", str(EXAMPLE)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "reason-assembly sync --json config=test-proxy-config.yaml"
    ]
    assert result.stderr == ""
