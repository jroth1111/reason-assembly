from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "ccy" + "council",
    "model" + "-council",
)


def _matches(label: str, payload: bytes) -> list[str]:
    lowered_label = label.casefold()
    try:
        lowered_text = payload.decode("utf-8").casefold()
    except UnicodeDecodeError:
        lowered_text = ""
    return [token for token in FORBIDDEN if token in lowered_label or token in lowered_text]


def test_tracked_tree_contains_only_canonical_identity():
    tracked = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    findings: list[str] = []
    for encoded in tracked:
        if not encoded:
            continue
        relative = encoded.decode("utf-8")
        path = ROOT / relative
        if not path.is_file():
            continue
        for token in _matches(relative, path.read_bytes()):
            findings.append(f"{relative}: {token}")
    assert not findings, "retired identity found:\n" + "\n".join(findings)


def test_built_wheel_contains_only_canonical_identity(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(tmp_path.glob("reason_assembly-0.6.0-*.whl"))
    assert len(wheels) == 1

    findings: list[str] = []
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        for name in names:
            for token in _matches(name, archive.read(name)):
                findings.append(f"{name}: {token}")
    assert not findings, "retired identity found in wheel:\n" + "\n".join(findings)

    removed_launcher = "bin/" + ("ccy" + "council")
    removed_skill = "compat/" + ("model" + "-council")
    assert all(removed_launcher not in name for name in names)
    assert all(removed_skill not in name for name in names)
    assert any(name.endswith("share/reason-assembly/SKILL.md") for name in names)
