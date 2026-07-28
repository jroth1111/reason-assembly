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
    assert source.index("ccyproxy_catalogue_sync") < source.index(
        "Fetch /v1/models here"
    )
    assert source.index("command -v reason-assembly") < source.index("command -v ccycouncil")
    assert "command reason-assembly sync --json" in source
    assert "REASON_ASSEMBLY_SUPPRESS_DEPRECATION=1" in source
    assert "using deprecated ccycouncil fallback" in source
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


def test_ccyproxy_sync_executes_canonical_then_deprecated_fallback(tmp_path):
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "sync.log"
    canonical = bin_dir / "reason-assembly"
    legacy = bin_dir / "ccycouncil"
    canonical.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "printf 'reason-assembly %s config=%s\\n' \"$*\" \"${CCYPROXY_CONFIG:-}\" >>\"$SYNC_LOG\"\n"
    )
    legacy.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "printf 'ccycouncil %s suppress=%s config=%s\\n' \"$*\" "
        "\"${REASON_ASSEMBLY_SUPPRESS_DEPRECATION:-}\" "
        "\"${CCYPROXY_CONFIG:-}\" >>\"$SYNC_LOG\"\n"
        "if [ \"${REASON_ASSEMBLY_SUPPRESS_DEPRECATION:-}\" != 1 ]; then\n"
        "  printf 'wrapper deprecation warning\\n' >&2\n"
        "fi\n"
    )
    canonical.chmod(0o755)
    legacy.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "SYNC_LOG": str(log),
        "CCYPROXY_CONFIG": "test-proxy-config.yaml",
    }

    canonical_result = subprocess.run(
        [zsh, "-f", "-c", 'source "$1"; ccyproxy_catalogue_sync', "test", str(EXAMPLE)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert canonical_result.returncode == 0, canonical_result.stderr
    assert log.read_text().splitlines() == [
        "reason-assembly sync --json config=test-proxy-config.yaml"
    ]
    assert canonical_result.stderr == ""

    canonical.unlink()
    log.write_text("")
    fallback_result = subprocess.run(
        [
            zsh,
            "-f",
            "-c",
            'source "$1"; ccyproxy_catalogue_sync; ccyproxy_catalogue_sync',
            "test",
            str(EXAMPLE),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert fallback_result.returncode == 0, fallback_result.stderr
    assert log.read_text().splitlines() == [
        "ccycouncil sync --json suppress=1 config=test-proxy-config.yaml",
        "ccycouncil sync --json suppress=1 config=test-proxy-config.yaml",
    ]
    assert fallback_result.stderr.count("using deprecated ccycouncil fallback") == 1
    assert "wrapper deprecation warning" not in fallback_result.stderr
