from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from artifacts import RunStore, SecretGuard
from conftest import FakeTransport
from contracts import CandidateSummary, CommandReceipt, Contribution, WorkerReceipt
from git_worker import (
    ImplementationEngine,
    ImplementationRequest,
    apply_run,
    contribution_selection_issues,
    git,
    pack_patch,
    parse_codex_events,
    resolve_base,
    resolve_review_target,
    run_implementation,
)


def init_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    return path


def fake_codex(bin_dir: Path) -> Path:
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(
        """#!/usr/bin/env python3
import json, pathlib, sys
prompt = sys.argv[-1]
root = pathlib.Path.cwd()
if "Create only a focused regression test patch" in prompt:
    target = root / "tests" / "test_feature.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("import sys, pathlib\\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\\nfrom calc import value\\n\\ndef test_value():\\n    assert value() == 2\\n")
    message = "Added a focused failing regression test."
else:
    (root / "calc.py").write_text("def value():\\n    return 2\\n")
    message = "Implemented value behavior and retained the regression test."
print(json.dumps({"item":{"type":"command_execution","status":"completed","command":"pytest -q","exit_code":0,"aggregated_output":"ok"}}))
print(json.dumps({"item":{"type":"agent_message","text":message}}))
"""
    )
    script.chmod(0o755)
    return script


def test_review_target_selection_and_empty_refusal(tmp_path):
    repo = init_repo(tmp_path / "repo", {"a.txt": "one\n"})
    with pytest.raises(RuntimeError, match="no changes"):
        resolve_review_target(repo)
    (repo / "a.txt").write_text("two\n")
    root, diff, description = resolve_review_target(repo)
    assert root == repo.resolve()
    assert "default dirty working tree" == description
    assert "+two" in diff
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    _, staged, _ = resolve_review_target(repo, staged=True)
    assert "+two" in staged
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        resolve_review_target(repo, staged=True, working_tree=True)


def test_review_commit_range_base_and_untracked(tmp_path):
    repo = init_repo(tmp_path / "repo", {"a.txt": "one\n"})
    base = resolve_base(repo, "HEAD")
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("new\n")
    _, working, _ = resolve_review_target(repo, working_tree=True)
    assert "new.txt" in working
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "second",
        ],
        check=True,
    )
    head = resolve_base(repo, "HEAD")
    assert "+two" in resolve_review_target(repo, commit=head)[1]
    assert "+two" in resolve_review_target(repo, range_spec=f"{base}..{head}")[1]
    assert "+two" in resolve_review_target(repo, base=base)[1]


def test_patch_packing_is_file_coverage_aware():
    patch = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    packed = pack_patch(patch, 180)
    assert "PATCH sha256=" in packed
    assert "PATCH COVERAGE" in packed
    assert "tests/test_a.py" in packed
    assert "sha256" in packed


def test_contribution_graph_refuses_missing_dependencies_and_conflicts():
    rows = [
        Contribution(
            id="K-A",
            candidate_label="A",
            description="feature",
            acceptance_ids=["AC-1"],
            dependencies=["K-B"],
            conflicts=["K-C"],
            patch_sha256="a" * 64,
            verified=True,
            verification_receipt_ids=["FT-1"],
            provenance=["patches/a.patch"],
        ),
        Contribution(
            id="K-C",
            candidate_label="C",
            description="incompatible",
            acceptance_ids=["AC-2"],
            patch_sha256="c" * 64,
            verified=True,
        ),
    ]
    conflicts, dependencies, coverage = contribution_selection_issues(
        rows, {"K-A", "K-C"}, ["AC-1", "AC-2"]
    )
    assert conflicts == ["K-C"]
    assert dependencies == ["K-B"]
    assert coverage == {"AC-1": ["K-A"], "AC-2": ["K-C"]}


def test_real_codex_jsonl_event_parsing():
    text = (
        '{"item":{"type":"command_execution","status":"completed",'
        '"command":"pytest -q","exit_code":0,"aggregated_output":"ok"}}\n'
        '{"item":{"type":"agent_message","text":"done"}}\n'
    )
    commands, tests, design = parse_codex_events(text)
    assert commands == tests
    assert commands[0].exit_code == 0
    assert design == "done"


def test_dynamic_worker_focus_and_peer_review_policy():
    focus = ImplementationEngine.worker_focus(["AC-1", "AC-2", "AC-3"], 2)
    assert set().union(*map(set, focus)) == {"AC-1", "AC-2", "AC-3"}
    assert focus[0] != focus[1]
    receipts = [
        WorkerReceipt(
            label=label,
            model=f"model-{label}",
            family=f"family-{label}",
            base_commit="a" * 40,
            exit_code=0,
            changed_files=["src/shared.py"],
            tests=[
                CommandReceipt(
                    command="pytest",
                    exit_code=0,
                    phase="test-green",
                )
            ],
            patch_sha256=patch,
            baseline_proven=True,
            final_proven=True,
            valid=True,
            focus_acceptance_ids=worker_focus,
        )
        for label, patch, worker_focus in zip(
            ["A", "B"],
            ["1" * 64, "2" * 64],
            focus,
        )
    ]
    summaries = [
        CandidateSummary(
            label=item.label,
            design="design",
            changed_files=item.changed_files,
            patch_sha256=item.patch_sha256 or "",
            patch_excerpt="diff",
            focus_acceptance_ids=item.focus_acceptance_ids,
        )
        for item in receipts
    ]
    decision = ImplementationEngine.peer_review_decision(receipts, summaries)
    assert decision.enabled
    assert decision.complementary_criteria == ["AC-1", "AC-2", "AC-3"]
    assert decision.potential_conflicting_files == ["src/shared.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["select", "integrate", "reject"])
async def test_disposable_repo_candidate_fusion_cleanup_and_apply_paths(
    tmp_path, fake_settings, monkeypatch, action
):
    repo = init_repo(
        tmp_path / f"repo-{action}",
        {
            "calc.py": "def value():\n    return 1\n",
            "tests/test_smoke.py": "def test_smoke():\n    assert True\n",
        },
    )
    fake = fake_codex(tmp_path / f"bin-{action}")
    monkeypatch.setenv("PATH", str(fake.parent) + os.pathsep + os.environ["PATH"])
    base = resolve_base(repo, "HEAD")
    FakeTransport.judge_action = action
    result = await run_implementation(
        ImplementationRequest(
            repo=str(repo),
            base=base,
            task="Change value() to return 2.",
            budget_requested="quick",
            test_commands=["pytest -q"],
            verification_mode="regression",
            worker_timeout=30,
        ),
        state=tmp_path / "state",
        settings=fake_settings,
        transport_factory=FakeTransport,
    )
    assert resolve_base(repo, "HEAD") == base
    worktree_output = git(repo, "worktree", "list", "--porcelain").stdout
    assert result.run_id not in worktree_output
    assert not any(
        artifact.startswith("worktrees/") for artifact in result.manifest.artifacts
    )
    store = RunStore.open_existing(
        tmp_path / "state",
        result.run_id,
        SecretGuard(fake_settings.exact_secrets),
    )
    assert list((store.path / "patches").glob("candidate-*.patch"))
    receipts = [
        json.loads(path.read_text())
        for path in (store.path / "private" / "receipts").glob("candidate-*.json")
    ]
    assert len(receipts) == 2
    assert all(item["baseline_proven"] and item["final_proven"] for item in receipts)
    if action == "reject":
        assert result.manifest.final_commit is None
        assert not git(repo, "branch", "--list", f"ccycouncil/{result.run_id}/*").stdout
        return
    assert result.manifest.final_commit
    assert result.manifest.final_branch
    assert store.read_json("verdict.json")["evidence_refs"]
    assert (
        int(
            git(
                repo,
                "rev-list",
                "--count",
                f"{base}..{result.manifest.final_commit}",
            ).stdout
        )
        == 1
    )
    wrong = init_repo(tmp_path / f"wrong-{action}", {"x": "x\n"})
    with pytest.raises(RuntimeError, match="wrong repository"):
        apply_run(
            tmp_path / "state",
            result.run_id,
            wrong,
            settings=fake_settings,
        )
    (repo / "dirty.txt").write_text("dirty\n")
    with pytest.raises(RuntimeError, match="clean checkout"):
        apply_run(
            tmp_path / "state",
            result.run_id,
            repo,
            settings=fake_settings,
        )
    (repo / "dirty.txt").unlink()
    applied = apply_run(
        tmp_path / "state",
        result.run_id,
        repo,
        settings=fake_settings,
    )
    assert applied == result.manifest.final_commit
    assert (repo / "calc.py").read_text() == "def value():\n    return 2\n"


@pytest.mark.asyncio
async def test_apply_refuses_changed_base(tmp_path, fake_settings, monkeypatch):
    repo = init_repo(
        tmp_path / "repo",
        {
            "calc.py": "def value():\n    return 1\n",
            "tests/test_smoke.py": "def test_smoke():\n    assert True\n",
        },
    )
    fake = fake_codex(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(fake.parent) + os.pathsep + os.environ["PATH"])
    base = resolve_base(repo, "HEAD")
    result = await run_implementation(
        ImplementationRequest(
            repo=str(repo),
            base=base,
            task="Change value() to return 2.",
            budget_requested="quick",
            test_commands=["pytest -q"],
            worker_timeout=30,
        ),
        state=tmp_path / "state",
        settings=fake_settings,
        transport_factory=FakeTransport,
    )
    (repo / "other.txt").write_text("other\n")
    subprocess.run(["git", "-C", str(repo), "add", "other.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "advance",
        ],
        check=True,
    )
    with pytest.raises(RuntimeError, match="unchanged original base"):
        apply_run(
            tmp_path / "state",
            result.run_id,
            repo,
            settings=fake_settings,
        )
