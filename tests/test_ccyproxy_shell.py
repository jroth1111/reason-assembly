from __future__ import annotations

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
    assert "ccycouncil sync --json" in source
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
