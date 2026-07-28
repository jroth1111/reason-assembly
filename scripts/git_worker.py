from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from artifacts import RunStore, SecretGuard, sha256_text
from contracts import (
    CandidateSummary,
    CommandReceipt,
    Contribution,
    ContributionGraph,
    JudgmentBallot,
    PeerReviewDecision,
    SanitizedSnapshot,
    ValidationReceipt,
    Verdict,
    WorkerReceipt,
)
from protocols import (
    STATE,
    CouncilEngine,
    CouncilRequest,
    ProtocolResult,
    clean_blockers,
)
from deliberation import deterministic_order, judgment_assessment
from routing import Route, shuffled_labels
from transport import ProxySettings, ProxyTransport
from verification import command_shell, snapshot_sources
from v4 import (
    default_reporting_rules,
    digest,
    lock_rubric,
    selective_judgment,
)


def git(
    repo: Path,
    *args: str,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        input=input_text,
        capture_output=True,
        check=check,
    )


def repository_root(repo: str | Path) -> Path:
    candidate = Path(repo).expanduser().resolve()
    result = git(candidate, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def resolve_base(repo: Path, base: str) -> str:
    return git(repo, "rev-parse", f"{base}^{{commit}}").stdout.strip()


def require_clean_base(repo: str | Path, base: str) -> tuple[Path, str]:
    root = repository_root(repo)
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status.strip():
        raise RuntimeError("implementation requires a clean repository")
    commit = resolve_base(root, base)
    return root, commit


def _untracked_patch(root: Path) -> str:
    untracked = git(
        root, "ls-files", "--others", "--exclude-standard", "-z"
    ).stdout.split("\0")
    chunks: list[str] = []
    for relative in sorted(item for item in untracked if item):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--binary",
                "--no-index",
                "--",
                "/dev/null",
                relative,
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(
                f"failed to capture untracked file {relative}: {result.stderr}"
            )
        chunks.append(result.stdout)
    return "".join(chunks)


def resolve_review_target(
    repo: str | Path,
    *,
    base: str | None = None,
    range_spec: str | None = None,
    commit: str | None = None,
    staged: bool = False,
    working_tree: bool = False,
) -> tuple[Path, str, str]:
    root = repository_root(repo)
    selected = sum(
        bool(item) for item in (base, range_spec, commit, staged, working_tree)
    )
    if selected > 1:
        raise RuntimeError("review targets are mutually exclusive")
    description: str
    if working_tree:
        diff = git(root, "diff", "--binary", "HEAD").stdout + _untracked_patch(root)
        description = "working tree"
    elif staged:
        diff = git(root, "diff", "--cached", "--binary").stdout
        description = "staged changes"
    elif commit:
        resolved = resolve_base(root, commit)
        parents = git(root, "rev-list", "--parents", "-n", "1", resolved).stdout.split()
        if len(parents) > 1:
            diff = git(root, "diff", "--binary", f"{parents[1]}..{resolved}").stdout
        else:
            empty_tree = git(
                root, "hash-object", "-t", "tree", "/dev/null"
            ).stdout.strip()
            diff = git(root, "diff", "--binary", empty_tree, resolved).stdout
        description = f"commit {resolved}"
    elif range_spec:
        diff = git(root, "diff", "--binary", range_spec).stdout
        description = f"range {range_spec}"
    elif base:
        resolved = resolve_base(root, base)
        diff = git(root, "diff", "--binary", f"{resolved}...HEAD").stdout
        description = f"base {resolved}"
    else:
        dirty = git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
        if dirty.strip():
            diff = git(root, "diff", "--binary", "HEAD").stdout + _untracked_patch(root)
            description = "default dirty working tree"
        else:
            upstream = git(
                root,
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
                check=False,
            )
            if upstream.returncode:
                raise RuntimeError(
                    "no changes to review and no upstream range is configured"
                )
            reference = upstream.stdout.strip()
            diff = git(root, "diff", "--binary", f"{reference}...HEAD").stdout
            description = f"default upstream range {reference}...HEAD"
    if not diff.strip():
        raise RuntimeError(f"no changes exist for review target: {description}")
    return root, diff, description


def parse_codex_events(
    text: str,
) -> tuple[list[CommandReceipt], list[CommandReceipt], str]:
    commands: list[CommandReceipt] = []
    tests: list[CommandReceipt] = []
    messages: list[str] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict):
            item = event if isinstance(event, dict) else {}
        item_type = str(item.get("type", ""))
        status = str(item.get("status", event.get("type", "")))
        if item_type in {"command_execution", "command"} and (
            "completed" in status or item.get("exit_code") is not None
        ):
            command = item.get("command") or item.get("cmd") or ""
            output = (
                item.get("aggregated_output")
                or item.get("output")
                or item.get("stdout")
                or ""
            )
            receipt = CommandReceipt(
                command=str(command),
                exit_code=item.get("exit_code"),
                output=str(output)[-12_000:],
                phase="worker",
            )
            commands.append(receipt)
            if re.search(
                r"(?i)(?:^|\s)(pytest|py\.test|npm test|pnpm test|yarn test|"
                r"vitest|jest|cargo test|go test|rspec|bundle exec rake test|"
                r"diff --check|verify|validate)(?:\s|$)",
                receipt.command,
            ):
                tests.append(receipt)
        if item_type in {"agent_message", "message", "final_response"}:
            value = item.get("text") or item.get("content") or item.get("message")
            if isinstance(value, str):
                messages.append(value)
    return commands, tests, messages[-1] if messages else ""


def verification_mode(task: str, requested: str) -> str:
    if requested != "auto":
        return requested
    if re.search(r"(?i)\b(docs?|documentation|readme|changelog|typo)\b", task):
        return "docs"
    if re.search(r"(?i)\b(refactor|invariant|rename|reorganize|cleanup)\b", task):
        return "invariant"
    return "regression"


def detect_test_commands(repo: Path) -> list[str]:
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists():
        if (repo / "uv.lock").exists():
            return ["uv run pytest -q"]
        return ["pytest -q"]
    if (repo / "package.json").exists():
        try:
            package = json.loads((repo / "package.json").read_text())
        except (OSError, json.JSONDecodeError):
            package = {}
        if (package.get("scripts") or {}).get("test"):
            if (repo / "pnpm-lock.yaml").exists():
                return ["pnpm test"]
            if (repo / "yarn.lock").exists():
                return ["yarn test"]
            return ["npm test"]
    if (repo / "Cargo.toml").exists():
        return ["cargo test"]
    if (repo / "go.mod").exists():
        return ["go test ./..."]
    return []


def run_test_command(
    repo: Path, command: str, timeout: int, phase: str
) -> CommandReceipt:
    try:
        with tempfile.TemporaryDirectory(
            prefix="ccycouncil-pycache-"
        ) as pycache:
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "PATH",
                    "TMPDIR",
                    "LANG",
                    "LC_ALL",
                    "TERM",
                    "CI",
                    "SSL_CERT_FILE",
                    "SSL_CERT_DIR",
                }
            }
            env["PYTHONPYCACHEPREFIX"] = pycache
            result = subprocess.run(
                [command_shell(), "-c", command],
                cwd=repo,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=env,
            )
        output = (result.stdout + "\n" + result.stderr)[-12_000:]
        return CommandReceipt(
            command=command,
            exit_code=result.returncode,
            output=output,
            phase=phase,
        )
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or "") + "\n" + (error.stderr or "")
        return CommandReceipt(
            command=command,
            exit_code=None,
            output=str(output)[-12_000:],
            phase=phase,
            timed_out=True,
        )


def changed_files(repo: Path) -> list[str]:
    result = git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
    files: list[str] = []
    for line in result.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('"'))
    return sorted(set(files))


def test_only(files: list[str]) -> bool:
    if not files:
        return False
    return all(
        re.search(
            r"(?i)(^|/)(tests?|spec|fixtures?)(/|$)|"
            r"(?:^|/)(?:test_|spec_)|(?:\.test|\.spec)\.[^.]+$",
            path,
        )
        is not None
        for path in files
    )


def capture_patch(repo: Path) -> str:
    git(repo, "add", "-N", ".", check=False)
    return git(repo, "diff", "--binary", "HEAD").stdout


def patch_sections(patch: str) -> list[tuple[str, str]]:
    starts = [match.start() for match in re.finditer(r"(?m)^diff --git ", patch)]
    if not starts:
        return [("patch", patch)] if patch else []
    sections = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(patch)
        chunk = patch[start:end]
        header = chunk.splitlines()[0]
        match = re.search(r" b/(.+)$", header)
        path = match.group(1) if match else f"section-{index + 1}"
        sections.append((path, chunk))
    return sections


def pack_patch(patch: str, max_chars: int) -> str:
    digest = sha256_text(patch)
    sections = patch_sections(patch)
    ordered = sorted(
        sections,
        key=lambda item: (
            0 if re.search(r"(?i)(test|spec|fixture)", item[0]) else 1,
            item[0],
        ),
    )
    header = f"[PATCH sha256={digest} files={len(sections)}]\n"
    if len(header) + len(patch) <= max_chars:
        return header + patch
    included: list[str] = []
    omitted: list[tuple[str, str]] = []
    chunks = [header]
    used = len(header)
    for path, section in ordered:
        if used + len(section) <= max_chars:
            chunks.append(section)
            included.append(path)
            used += len(section)
        else:
            omitted.append((path, sha256_text(section)))
    manifest = (
        "\n[PATCH COVERAGE included="
        + json.dumps(included)
        + " omitted="
        + json.dumps([{"path": path, "sha256": digest} for path, digest in omitted])
        + "]\n"
    )
    while len("".join(chunks)) + len(manifest) > max_chars and len(chunks) > 1:
        removed = chunks.pop()
        path = included.pop()
        omitted.append((path, sha256_text(removed)))
        manifest = (
            "\n[PATCH COVERAGE included="
            + json.dumps(included)
            + " omitted="
            + json.dumps([{"path": path, "sha256": digest} for path, digest in omitted])
            + "]\n"
        )
    return "".join(chunks) + manifest


def contribution_selection_issues(
    contributions: list[Contribution],
    selected_ids: set[str],
    acceptance_ids: list[str],
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    selected = [item for item in contributions if item.id in selected_ids]
    conflicts = sorted(
        {
            conflict
            for item in selected
            for conflict in item.conflicts
            if conflict in selected_ids
        }
    )
    dependencies = sorted(
        {
            dependency
            for item in selected
            for dependency in item.dependencies
            if dependency not in selected_ids
        }
    )
    coverage = {
        acceptance_id: [
            item.id for item in selected if acceptance_id in item.acceptance_ids
        ]
        for acceptance_id in acceptance_ids
    }
    return conflicts, dependencies, coverage


@dataclass
class ImplementationRequest:
    repo: str
    base: str
    task: str
    budget_requested: str = "adaptive"
    contexts: list[tuple[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    verify_commands: list[str] = field(default_factory=list)
    route_overrides: list[str] = field(default_factory=list)
    max_calls: int | None = None
    quorum_grace: float | None = None
    test_commands: list[str] = field(default_factory=list)
    verification_mode: str = "auto"
    worker_timeout: int = 900
    parent_run_id: str | None = None
    ancestry_relation: str | None = None
    prior_models: list[str] = field(default_factory=list)
    manifest_mode: str | None = None
    judgment_risk: float = 0.05


def _worker_config(route: Route, settings: ProxySettings, codex_home: Path) -> None:
    context = route.capability.context_window or 128_000
    content = (
        f'model = "{route.model}"\n'
        f'model_reasoning_effort = "{route.effort}"\n'
        'model_provider = "ccyproxy"\n'
        f"model_context_window = {context}\n"
        f"model_auto_compact_token_limit = {int(context * 0.8)}\n"
        'approval_policy = "never"\n'
        'sandbox_mode = "workspace-write"\n'
        "[model_providers.ccyproxy]\n"
        'name = "CLIProxyAPI"\n'
        f'base_url = "{settings.base_url}/v1"\n'
        'env_key = "CCYCOUNCIL_EPHEMERAL_KEY"\n'
        'wire_api = "responses"\n'
        "[sandbox_workspace_write]\n"
        "network_access = false\n"
    )
    (codex_home / "config.toml").write_text(content)
    os.chmod(codex_home / "config.toml", 0o600)


def invoke_codex(
    repo: Path,
    route: Route,
    settings: ProxySettings,
    prompt: str,
    timeout: int,
) -> tuple[int, str, bool]:
    codex_home = Path(tempfile.mkdtemp(prefix="ccycouncil-codex-"))
    os.chmod(codex_home, 0o700)
    _worker_config(route, settings, codex_home)
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "TERM",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
    }
    env.update(
        {
            "HOME": str(codex_home),
            "CODEX_HOME": str(codex_home),
            "CCYCOUNCIL_EPHEMERAL_KEY": settings.api_key,
        }
    )
    try:
        result = subprocess.run(
            [
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                prompt,
            ],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout + result.stderr, False
    except subprocess.TimeoutExpired as error:
        output = str(error.stdout or "") + str(error.stderr or "")
        return 124, output, True
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)


def _candidate_key(label: str) -> str:
    return label.rsplit(" ", 1)[-1].lower()


def _candidate_prompt(
    task: str,
    contract_json: str,
    evidence_json: str,
    base: str,
    mode: str,
    phase: str,
    focus_acceptance_ids: list[str],
) -> str:
    anchor = (
        f"ORIGINAL BASE COMMIT: {base}\n"
        f"VERIFICATION MODE: {mode}\n"
        f"TASK CONTRACT:\n{contract_json}\n"
        f"EVIDENCE INVENTORY:\n{evidence_json}\n"
        f"PRIMARY ACCEPTANCE FOCUS: {json.dumps(focus_acceptance_ids)}\n"
        f"TASK:\n{task}\n"
    )
    if phase == "tests":
        return (
            anchor
            + "\nCreate only a focused regression test patch. Do not implement the "
            "behavioral fix. Run the specified or repository test command and leave "
            "the new test failing for the intended reason. Do not commit."
        )
    return (
        anchor
        + "\nRe-anchor on the current Git state. Implement the task completely while "
        "preserving any test-only patch already present. Run relevant tests. Do not "
        "commit, access the network, or introduce credentials."
    )


def execute_candidate(
    *,
    label: str,
    worktree: Path,
    route: Route,
    settings: ProxySettings,
    store: RunStore,
    task: str,
    task_contract: Any,
    evidence_refs: list[dict[str, Any]],
    base_commit: str,
    mode: str,
    commands: list[str],
    timeout: int,
    focus_acceptance_ids: list[str],
) -> WorkerReceipt:
    key = _candidate_key(label)
    all_commands: list[CommandReceipt] = []
    worker_tests: list[CommandReceipt] = []
    logs: list[str] = []
    baseline_proven = False
    final_proven = False
    failure: str | None = None
    exit_code = 0
    design = ""

    baseline = [
        run_test_command(worktree, command, timeout, "baseline") for command in commands
    ]
    all_commands.extend(baseline)
    worker_tests.extend(baseline)
    if commands and not all(
        item.exit_code == 0 and not item.timed_out for item in baseline
    ):
        failure = "baseline verification command failed"
    else:
        baseline_proven = bool(commands) or mode == "docs"

    if not failure and mode == "regression":
        if not commands:
            failure = "regression mode requires a detected or supplied test command"
        else:
            prompt = _candidate_prompt(
                task,
                json.dumps(task_contract.model_dump(mode="json"), sort_keys=True),
                json.dumps(evidence_refs, sort_keys=True),
                base_commit,
                mode,
                "tests",
                focus_acceptance_ids,
            )
            exit_code, output, timed_out = invoke_codex(
                worktree, route, settings, prompt, timeout
            )
            logs.append(output)
            parsed_commands, parsed_tests, message = parse_codex_events(output)
            all_commands.extend(parsed_commands)
            worker_tests.extend(parsed_tests)
            design = message or design
            files = changed_files(worktree)
            if timed_out:
                failure = "test-only worker timed out"
            elif exit_code:
                failure = "test-only worker failed"
            elif not test_only(files):
                failure = "phase one was not a test-only patch"
            else:
                failing = [
                    run_test_command(worktree, command, timeout, "test-red")
                    for command in commands
                ]
                all_commands.extend(failing)
                worker_tests.extend(failing)
                if not all(
                    item.exit_code not in (0, None) and not item.timed_out
                    for item in failing
                ):
                    failure = (
                        "new regression test did not fail against the base behavior"
                    )
                else:
                    baseline_proven = True

    if not failure:
        prompt = _candidate_prompt(
            task,
            json.dumps(task_contract.model_dump(mode="json"), sort_keys=True),
            json.dumps(evidence_refs, sort_keys=True),
            base_commit,
            mode,
            "implementation",
            focus_acceptance_ids,
        )
        exit_code, output, timed_out = invoke_codex(
            worktree, route, settings, prompt, timeout
        )
        logs.append(output)
        parsed_commands, parsed_tests, message = parse_codex_events(output)
        all_commands.extend(parsed_commands)
        worker_tests.extend(parsed_tests)
        design = message or design
        if timed_out:
            failure = "implementation worker timed out"
        elif exit_code:
            failure = "implementation worker failed"

    git(worktree, "add", "-N", ".", check=False)
    diff_check = git(worktree, "diff", "--check", "HEAD", check=False)
    all_commands.append(
        CommandReceipt(
            command="git diff --check HEAD",
            exit_code=diff_check.returncode,
            output=(diff_check.stdout + diff_check.stderr)[-12_000:],
            phase="engine-final",
        )
    )
    patch = capture_patch(worktree)
    files = changed_files(worktree)
    if not failure and diff_check.returncode:
        failure = "git diff --check failed"
    if not failure and not patch.strip():
        failure = "worker produced no patch"
    if not failure:
        try:
            store.guard.reject_added_credentials(patch)
        except RuntimeError as error:
            failure = str(error)

    final_tests = [
        run_test_command(worktree, command, timeout, "test-green")
        for command in commands
    ]
    all_commands.extend(final_tests)
    worker_tests.extend(final_tests)
    if commands:
        final_proven = all(
            item.exit_code == 0 and not item.timed_out for item in final_tests
        )
    else:
        final_proven = mode == "docs" and diff_check.returncode == 0
    if not failure and not final_proven:
        failure = "final verification command failed"

    store.write_text(f"workers/{key}.jsonl", "\n".join(logs))
    patch_path = store.write_text(f"patches/candidate-{key}.patch", patch)
    valid = failure is None
    acceptance = {
        criterion.id: (
            "verified by baseline/final evidence"
            if valid
            else f"not verified: {failure}"
        )
        for criterion in task_contract.acceptance_criteria
    }
    receipt = WorkerReceipt(
        label=label,
        model=route.model,
        family=route.family,
        base_commit=base_commit,
        exit_code=exit_code,
        design=design,
        changed_files=files,
        acceptance_results=acceptance,
        commands=all_commands,
        tests=worker_tests,
        risks=[] if valid else [failure or "unknown worker failure"],
        patch_sha256=sha256_text(patch) if patch else None,
        patch_artifact=str(patch_path.relative_to(store.path)),
        baseline_proven=baseline_proven,
        final_proven=final_proven,
        valid=valid,
        failure_reason=failure,
        focus_acceptance_ids=focus_acceptance_ids,
    )
    store.write_json(f"private/receipts/candidate-{key}.json", receipt)
    return receipt


class ImplementationEngine:
    def __init__(
        self,
        request: ImplementationRequest,
        *,
        state: Path = STATE,
        settings: ProxySettings | None = None,
        transport_factory: type[ProxyTransport] = ProxyTransport,
    ):
        self.request = request
        self.repo, self.base_commit = require_clean_base(request.repo, request.base)
        council_request = CouncilRequest(
            mode="implement",
            manifest_mode=request.manifest_mode or "implement",
            prompt=request.task,
            budget_requested=request.budget_requested,
            contexts=request.contexts,
            sources=request.sources,
            verify_commands=request.verify_commands,
            route_overrides=request.route_overrides,
            max_calls=request.max_calls,
            quorum_grace=request.quorum_grace,
            parent_run_id=request.parent_run_id,
            ancestry_relation=request.ancestry_relation,
            prior_models=request.prior_models,
            repo=str(self.repo),
            base_commit=self.base_commit,
            judgment_risk=min(request.judgment_risk, 0.05),
        )
        self.engine = CouncilEngine(
            council_request,
            state=state,
            settings=settings,
            transport_factory=transport_factory,
        )
        self.worktrees: list[Path] = []
        self.final_branch: str | None = None
        self.final_worktree: Path | None = None

    def add_detached_worktree(self, path: Path) -> None:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        git(
            self.repo,
            "worktree",
            "add",
            "--detach",
            str(path),
            self.base_commit,
        )
        self.worktrees.append(path)

    def add_final_worktree(self, path: Path) -> str:
        branch = f"ccycouncil/{self.engine.run_id}/final"
        git(
            self.repo,
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            self.base_commit,
        )
        self.worktrees.append(path)
        self.final_branch = branch
        self.final_worktree = path
        return branch

    def cleanup(self, keep_final_branch: bool) -> None:
        for path in reversed(self.worktrees):
            if path.exists() and self.engine.store.path not in path.resolve().parents:
                continue
            git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                str(path),
                check=False,
            )
        git(self.repo, "worktree", "prune", check=False)
        if self.final_branch and not keep_final_branch:
            git(
                self.repo,
                "branch",
                "-D",
                self.final_branch,
                check=False,
            )

    @staticmethod
    def worker_focus(acceptance_ids: list[str], worker_count: int) -> list[list[str]]:
        if worker_count <= 0:
            return []
        rows = [[] for _ in range(worker_count)]
        for index, acceptance_id in enumerate(acceptance_ids):
            rows[index % worker_count].append(acceptance_id)
        for index, row in enumerate(rows):
            if not row and acceptance_ids:
                row.append(acceptance_ids[index % len(acceptance_ids)])
        return rows

    @staticmethod
    def peer_review_decision(
        receipts: list[WorkerReceipt],
        summaries: list[CandidateSummary],
    ) -> PeerReviewDecision:
        if len(receipts) < 2:
            return PeerReviewDecision(
                enabled=False,
                reasons=["fewer than two verified candidates"],
            )
        focus_sets = [set(item.focus_acceptance_ids) for item in receipts]
        complementary = sorted(
            set().union(*focus_sets)
            if any(
                left != right
                for index, left in enumerate(focus_sets)
                for right in focus_sets[index + 1 :]
            )
            else set()
        )
        conflicting_files = sorted(
            {
                filename
                for index, left in enumerate(summaries)
                for right in summaries[index + 1 :]
                if left.patch_sha256 != right.patch_sha256
                for filename in set(left.changed_files) & set(right.changed_files)
            }
        )
        weak = sorted(
            item.label
            for item in receipts
            if not item.final_proven
            or not any(
                test.phase in {"test-green", "engine-final"}
                and test.exit_code == 0
                and not test.timed_out
                for test in item.tests
            )
        )
        reasons = []
        if complementary:
            reasons.append("candidates emphasize complementary acceptance criteria")
        if conflicting_files:
            reasons.append("different patches overlap changed files")
        if weak:
            reasons.append("candidate verification is weak")
        return PeerReviewDecision(
            enabled=bool(reasons),
            complementary_criteria=complementary,
            potential_conflicting_files=conflicting_files,
            weakly_verified_candidates=weak,
            reasons=reasons or ["no complementary, conflicting, or weak evidence"],
        )

    def summaries(
        self, receipts: list[WorkerReceipt], max_chars: int
    ) -> list[CandidateSummary]:
        each = max(2000, max_chars // max(1, len(receipts)))
        summaries = []
        for receipt in receipts:
            patch = self.engine.store._target(receipt.patch_artifact or "").read_text()
            contributions = [
                Contribution(
                    id=f"K-{sha256_text(receipt.label + acceptance_id)[:12]}",
                    candidate_label=receipt.label,
                    description=result,
                    acceptance_ids=[acceptance_id],
                    changed_files=receipt.changed_files,
                    patch_sha256=receipt.patch_sha256 or sha256_text(patch),
                    verified=receipt.final_proven,
                    verification_receipt_ids=[
                        "T-"
                        + sha256_text(
                            test.command
                            + str(test.exit_code)
                            + str(test.timed_out)
                            + test.output
                        )[:12]
                        for test in receipt.tests
                        if test.exit_code == 0 and not test.timed_out
                    ],
                    provenance=[
                        receipt.patch_artifact or "",
                        receipt.patch_sha256 or sha256_text(patch),
                    ],
                    conflicts=[
                        value.split(":", 1)[1].strip()
                        for value in receipt.risks
                        if value.lower().startswith("conflicts:")
                    ],
                    dependencies=[
                        value.split(":", 1)[1].strip()
                        for value in receipt.risks
                        if value.lower().startswith("depends:")
                    ],
                )
                for acceptance_id, result in sorted(receipt.acceptance_results.items())
            ]
            summaries.append(
                CandidateSummary(
                    label=receipt.label,
                    design=(
                        "Verified candidate changing: "
                        + ", ".join(receipt.changed_files)
                    ),
                    changed_files=receipt.changed_files,
                    acceptance_results=receipt.acceptance_results,
                    tests=receipt.tests,
                    risks=receipt.risks,
                    patch_sha256=receipt.patch_sha256 or sha256_text(patch),
                    patch_excerpt=pack_patch(patch, each),
                    contributions=contributions,
                    focus_acceptance_ids=receipt.focus_acceptance_ids,
                )
            )
        return summaries

    async def peer_reviews(
        self,
        receipts: list[WorkerReceipt],
        summaries: list[CandidateSummary],
    ) -> list[JudgmentBallot]:
        decision = self.peer_review_decision(receipts, summaries)
        self.engine.store.write_json("implementation-policy/peer-review.json", decision)
        if not decision.enabled:
            return []
        routes = {route.model: route for route in self.engine.member_routes}
        calls = []
        metadata = []
        for index, receipt in enumerate(receipts):
            target = receipts[(index + 1) % len(receipts)]
            route = routes[receipt.model]
            payload = {
                "target_candidate": next(
                    item.model_dump(mode="json")
                    for item in summaries
                    if item.label == target.label
                ),
            }
            calls.append(
                self.engine.validated_call(
                    route,
                    participant=f"candidate-review-{index + 1}",
                    stage="candidate-peer-review",
                    role="Anonymous implementation peer reviewer",
                    instructions=(
                        "Score the target against each acceptance criterion using only "
                        "engine receipts. action=select when credible or reject when "
                        "blocked. Do not infer model identity."
                    ),
                    payload=payload,
                    contract=JudgmentBallot,
                    max_output_tokens=5000,
                )
            )
            metadata.append((receipt.label, target.label))
        raw = await asyncio.gather(*calls, return_exceptions=True)
        reviews = []
        for index, (result, (label, target)) in enumerate(zip(raw, metadata)):
            if isinstance(result, BaseException):
                self.engine.store.append_event(
                    "candidate_peer_review_failed",
                    label=label,
                    error=self.engine.guard.redact_text(str(result))[:500],
                )
                continue
            review, usage = result
            review.order = [target]
            reviews.append(review)
            self.engine.store.write_json(
                f"candidate-reviews/review-{index + 1}.json", review
            )
            self.engine.store.append_event(
                "candidate_peer_review", label=label, usage=usage
            )
        return reviews

    async def judge_candidates(
        self,
        contract: Any,
        summaries: list[CandidateSummary],
        reviews: list[JudgmentBallot],
    ) -> Verdict:
        assert self.engine.judge_route is not None
        labels = [item.label for item in summaries]
        by_label = {item.label: item for item in summaries}
        payload_base = {
            "task_contract": contract.model_dump(mode="json"),
            "anonymous_peer_reviews": [
                review.model_dump(mode="json") for review in reviews
            ],
        }

        async def make_ballot(
            order: list[str], suffix: str, route: Route | None = None
        ) -> JudgmentBallot:
            active = route or self.engine.judge_route
            value, call_usage = await self.engine.validated_call(
                active,
                participant=f"implementation-judge-{suffix}",
                stage="implementation-judging",
                role="Blind implementation fusion judge",
                instructions=(
                    "Return action=select, integrate, or reject with acceptance-level "
                    "scores. Select a base candidate for select/integrate. Integrate "
                    "only verified, dependency-complete, non-conflicting components."
                ),
                payload={
                    **payload_base,
                    "candidates": [
                        by_label[label].model_dump(mode="json") for label in order
                    ],
                },
                contract=JudgmentBallot,
                max_output_tokens=7000,
            )
            value.order = order
            value.blockers = clean_blockers(value.blockers)
            self.engine.store.write_json(f"judging/implementation-{suffix}.json", value)
            self.engine.store.append_event(
                "implementation_judgment_ballot",
                suffix=suffix,
                usage=call_usage,
            )
            return value

        first = await make_ballot(labels, "first")
        second = await make_ballot(list(reversed(labels)), "reversed")
        ballots = [first, second]
        consistency = judgment_assessment(first, second, set())
        self.engine.store.write_json(
            "judging/implementation-consistency.json", consistency
        )
        chosen = first
        if not consistency.consistent and (first.action, first.selected_candidate) != (
            second.action,
            second.selected_candidate,
        ):
            tiebreak_route = self.engine.route_for_role(
                "judge",
                contract,
                exclude_families={self.engine.judge_route.family},
            )
            if tiebreak_route and self.engine.remaining_calls() >= 2:
                tie = await make_ballot(
                    deterministic_order(
                        self.engine.run_id, labels, "implementation-tiebreak"
                    ),
                    "tiebreaker",
                    tiebreak_route,
                )
                ballots.append(tie)
                first_key = (first.action, first.selected_candidate)
                second_key = (second.action, second.selected_candidate)
                tie_key = (tie.action, tie.selected_candidate)
                if tie_key == second_key:
                    chosen = second
                    consistency.consistent = True
                elif tie_key == first_key:
                    chosen = first
                    consistency.consistent = True
                else:
                    chosen.action = "reject"
                    chosen.blockers.append(
                        "mirrored implementation judgment did not converge"
                    )
            else:
                chosen.action = "reject"
                chosen.blockers.append(
                    "mirrored implementation judgment did not converge"
                )
            self.engine.store.write_json(
                "judging/implementation-consistency.json", consistency
            )
        label_set = set(labels)
        criterion_totals: dict[str, int] = {
            label: sum(
                score.score
                for ballot in ballots
                for score in ballot.scores
                if score.candidate_label == label
            )
            for label in labels
        }
        if chosen.action in {"select", "integrate"} and any(criterion_totals.values()):
            chosen.selected_candidate = sorted(
                labels,
                key=lambda label: (
                    -criterion_totals[label],
                    label,
                ),
            )[0]
        if chosen.action in {"select", "integrate"} and (
            chosen.selected_candidate not in label_set
        ):
            chosen.action = "reject"
            chosen.blockers.append("judge selected an invalid candidate label")
        scored_criteria = {
            item.criterion_id for item in chosen.scores if item.score > 0
        }
        selective = selective_judgment(
            scores_and_correct=[],
            score=chosen.reported_confidence
            * len(scored_criteria) / max(1, len(contract.acceptance_criteria)),
            judgment_risk=0.05,
            high_risk=True,
            implementation=True,
            deterministic=False,
            independently_verified=False,
        )
        self.engine.store.write_json("selective-judgment.json", selective)
        verdict = Verdict(
            decision="; ".join(chosen.rationale) or chosen.action,
            rationale=chosen.rationale,
            confidence=selective.confidence_high,
            blockers=chosen.blockers,
            action=chosen.action,
            selected_candidate=chosen.selected_candidate,
            selected_contribution_ids=chosen.accepted_claim_ids,
            integration_plan=[
                contribution.description
                for summary in summaries
                for contribution in summary.contributions
                if contribution.id in chosen.accepted_claim_ids
            ],
            acceptance_reasons={
                score.criterion_id: score.reason for score in chosen.scores
            },
            evidence_refs=sorted(
                {
                    receipt_id
                    for summary in summaries
                    for contribution in summary.contributions
                    if contribution.id in set(chosen.accepted_claim_ids)
                    for receipt_id in contribution.verification_receipt_ids
                }
            ),
            calibrated=selective.calibrated,
            judgment_risk=0.05,
            abstained=True,
            finality="abort",
        )
        self.engine.store.append_event("implementation_judgment", action=verdict.action)
        return verdict

    async def completion_review(
        self,
        selected: WorkerReceipt,
        contract: Any,
        final_patch: str,
        final_tests: list[CommandReceipt],
        *,
        excluded_families: set[str] | None = None,
        index: int = 1,
    ) -> ValidationReceipt:
        excluded = set(excluded_families or set()) | {selected.family}
        route = self.engine.route_for_role(
            "validator", contract, exclude_families=excluded
        )
        if not route:
            return ValidationReceipt(
                label=f"completion-review-{index}",
                family="none",
                status="insufficient",
                verdict_sha256=sha256_text(final_patch),
                blockers=["no cross-family completion reviewer"],
            )
        validator = route
        self.engine.identity_map[f"Completion reviewer {index}"] = {
            "model": route.model,
            "family": route.family,
            "effort": route.effort,
            "role": "validator",
        }
        payload = {
            "task_contract": contract.model_dump(mode="json"),
            "final_patch": pack_patch(
                final_patch,
                int((validator.capability.context_window or 64_000) * 2),
            ),
            "engine_tests": [
                receipt.model_dump(mode="json") for receipt in final_tests
            ],
        }
        receipt, usage = await self.engine.validated_call(
            validator,
            participant=f"completion-review-{index}",
            stage="completion-review",
            role="Cross-family completion reviewer",
            instructions=(
                "Check the final integrated diff and engine-owned test receipts against "
                "every acceptance criterion. Return blocker_free only if the result is "
                "complete and no material regression or credential risk remains."
            ),
            payload=payload,
            contract=ValidationReceipt,
            max_output_tokens=5000,
        )
        receipt.label = f"completion-review-{index}"
        receipt.family = route.family
        receipt.verdict_sha256 = sha256_text(final_patch)
        self.engine.store.write_json(
            (
                "validations/completion.json"
                if index == 1
                else f"validations/completion-{index}.json"
            ),
            receipt,
        )
        self.engine.store.append_event(
            "completion_review", status=receipt.status, usage=usage
        )
        return receipt

    async def run(self) -> ProtocolResult:
        store = self.engine.store
        keep_final = False
        mode = verification_mode(self.request.task, self.request.verification_mode)
        commands = self.request.test_commands or detect_test_commands(self.repo)
        store.write_json(
            "snapshot.json",
            SanitizedSnapshot(
                mode="implement",
                budget_requested=self.request.budget_requested,
                prompt=self.request.task,
                contexts=[
                    {"source": source, "content": content}
                    for source, content in self.request.contexts
                ],
                sources=self.request.sources,
                verify_commands=self.request.verify_commands,
                repo=str(self.repo),
                base_commit=self.base_commit,
                verification_mode=mode,
                test_commands=commands,
            ),
        )
        store.append_event(
            "started",
            budget=self.engine.budget_name,
            verification_mode=mode,
            base_commit=self.base_commit,
        )
        self.engine.persist_manifest()
        try:
            if self.request.sources:
                await snapshot_sources(
                    self.request.sources,
                    self.engine.inventory,
                    store,
                )
            self.engine.inventory.snapshot(store)
            routes, _, _ = await self.engine.preflight(role="worker")
            contract = await self.engine.task_contract()
            routes = self.engine.reroute_primary_role(contract, "worker")
            self.engine.manifest.task_kind = "implementation"
            rubric = lock_rubric(
                [item.model_dump(mode="json") for item in contract.acceptance_criteria],
                ["every selected contribution requires deterministic test receipts"],
                "verified contribution graph with acceptance-level integration",
                "implementation",
            )
            reporting = default_reporting_rules()
            store.write_json("rubric.json", rubric)
            store.write_json("reporting-rules.json", reporting)
            self.engine.manifest.rubric_sha256 = rubric["sha256"]
            self.engine.manifest.reporting_rules_sha256 = digest(
                reporting.model_dump(mode="json")
            )
            store.write_json("task-contract.json", contract)
            self.engine.manifest.task_contract_sha256 = sha256_text(
                json.dumps(contract.model_dump(mode="json"), sort_keys=True)
            )
            labels = shuffled_labels(self.engine.run_id, len(routes))
            focus_rows = self.worker_focus(
                [item.id for item in contract.acceptance_criteria],
                len(routes),
            )
            store.write_json(
                "implementation-policy/worker-focus.json",
                [
                    {
                        "label": label,
                        "acceptance_ids": focus,
                        "reason": (
                            "assigned from the currently uncovered acceptance set"
                        ),
                    }
                    for label, focus in zip(labels, focus_rows)
                ],
            )
            worktree_rows = []
            for label, route, focus in zip(labels, routes, focus_rows):
                path = store.path / "worktrees" / _candidate_key(label)
                self.add_detached_worktree(path)
                worktree_rows.append((label, route, path, focus))
                self.engine.identity_map[label] = {
                    "model": route.model,
                    "family": route.family,
                    "effort": route.effort,
                    "role": "worker",
                }
                self.engine.identity_map[f"{label} test-construction"] = {
                    "model": route.model,
                    "family": route.family,
                    "effort": route.effort,
                    "role": "test_constructor",
                }
            receipts = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        execute_candidate,
                        label=label,
                        worktree=path,
                        route=route,
                        settings=self.engine.settings,
                        store=store,
                        task=self.request.task,
                        task_contract=contract,
                        evidence_refs=[
                            ref.model_dump(mode="json")
                            for ref in self.engine.inventory.refs
                        ],
                        base_commit=self.base_commit,
                        mode=mode,
                        commands=commands,
                        timeout=self.request.worker_timeout,
                        focus_acceptance_ids=focus,
                    )
                    for label, route, path, focus in worktree_rows
                )
            )
            store.write_json("private/identity-map.json", self.engine.identity_map)
            valid = [receipt for receipt in receipts if receipt.valid]
            if not valid:
                raise RuntimeError("all implementation candidates failed verification")
            judge_context = self.engine.judge_route.capability.context_window or 64_000
            summaries = self.summaries(valid, int(judge_context * 2.2))
            store.write_json("candidate-summaries.json", summaries)
            reviews = await self.peer_reviews(valid, summaries)
            verdict = await self.judge_candidates(contract, summaries, reviews)
            all_contributions = [
                contribution
                for summary in summaries
                for contribution in summary.contributions
            ]
            selected_ids = set(verdict.selected_contribution_ids)
            if verdict.action == "select" and verdict.selected_candidate:
                selected_ids = {
                    item.id
                    for item in all_contributions
                    if item.candidate_label == verdict.selected_candidate
                }
            if verdict.action == "integrate" and not selected_ids:
                selected_ids = {item.id for item in all_contributions if item.verified}
            if verdict.action == "integrate" and verdict.selected_candidate:
                # The base patch is applied as a whole, so its complete component
                # set must be represented in the graph.
                selected_ids.update(
                    item.id
                    for item in all_contributions
                    if item.candidate_label == verdict.selected_candidate
                )
            selected_contributions = [
                item for item in all_contributions if item.id in selected_ids
            ]
            conflicts, dependencies, coverage = contribution_selection_issues(
                all_contributions,
                selected_ids,
                [item.id for item in contract.acceptance_criteria],
            )
            graph = ContributionGraph(
                contributions=all_contributions,
                selected_ids=sorted(selected_ids),
                rejected_ids=sorted(
                    item.id for item in all_contributions if item.id not in selected_ids
                ),
                unresolved_conflicts=conflicts
                + [f"missing dependency {item}" for item in dependencies],
                acceptance_coverage=coverage,
            )
            if verdict.action in {"select", "integrate"} and (
                conflicts or dependencies or not all(coverage.values())
            ):
                verdict.action = "reject"
                verdict.blockers.extend(
                    conflicts
                    or [f"missing dependency {item}" for item in dependencies]
                    or ["selected contributions do not cover every criterion"]
                )
            verdict.selected_contribution_ids = graph.selected_ids
            store.write_json("contribution-graph.json", graph)
            store.write_json("verdict.json", verdict)
            if verdict.action == "reject":
                self.engine.manifest.status = "blocked"
                self.engine.manifest.completed_at = datetime.now(timezone.utc)
                self.engine.persist_manifest()
                return ProtocolResult(
                    self.engine.run_id,
                    verdict,
                    self.engine.exclusions,
                    self.engine.manifest,
                )
            selected = next(
                receipt
                for receipt in valid
                if receipt.label == verdict.selected_candidate
            )
            final_path = store.path / "worktrees" / "final"
            branch = self.add_final_worktree(final_path)
            selected_patch = store._target(selected.patch_artifact or "")
            apply_result = git(
                final_path,
                "apply",
                "--index",
                "--binary",
                str(selected_patch),
                check=False,
            )
            if apply_result.returncode:
                raise RuntimeError(
                    "selected base patch did not apply to the original base: "
                    + apply_result.stderr
                )
            if verdict.action == "integrate":
                if not self.engine.integrator_route:
                    raise RuntimeError("Sol integrator is not healthy")
                selected_component_labels = {
                    item.candidate_label
                    for item in selected_contributions
                    if item.candidate_label != selected.label
                }
                other = [
                    item
                    for item in summaries
                    if item.label in selected_component_labels
                ]
                integration_inputs = Path(
                    tempfile.mkdtemp(prefix=".ccycouncil-input-", dir=final_path)
                )
                patch_paths = []
                for item in valid:
                    if item.label == selected.label:
                        continue
                    if item.label not in selected_component_labels:
                        continue
                    source = store._target(item.patch_artifact or "")
                    target = integration_inputs / source.name
                    shutil.copyfile(source, target)
                    patch_paths.append(str(target.relative_to(final_path)))
                integration_prompt = (
                    f"TASK\n{self.request.task}\n\n"
                    f"TASK CONTRACT\n"
                    f"{json.dumps(contract.model_dump(mode='json'), sort_keys=True)}\n\n"
                    f"ORIGINAL BASE {self.base_commit}\n"
                    f"SELECTED BASE {selected.label} is already applied but uncommitted.\n"
                    f"COMPONENT PLAN\n{json.dumps(verdict.integration_plan)}\n"
                    f"CONTRIBUTION GRAPH\n"
                    f"{json.dumps(graph.model_dump(mode='json'), sort_keys=True)}\n"
                    f"OTHER CANDIDATE REPORTS\n"
                    f"{json.dumps([item.model_dump(mode='json') for item in other])}\n"
                    f"FULL OTHER PATCH FILES\n{json.dumps(patch_paths)}\n\n"
                    "Integrate only the accepted components into the current working "
                    "tree. Re-anchor on current Git state, run tests, introduce no "
                    "credentials, and do not commit."
                )
                self.engine.identity_map["Integrator"] = {
                    "model": self.engine.integrator_route.model,
                    "family": self.engine.integrator_route.family,
                    "effort": self.engine.integrator_route.effort,
                    "role": "integrator",
                }
                try:
                    code, output, timed_out = await asyncio.to_thread(
                        invoke_codex,
                        final_path,
                        self.engine.integrator_route,
                        self.engine.settings,
                        integration_prompt,
                        self.request.worker_timeout,
                    )
                finally:
                    shutil.rmtree(integration_inputs, ignore_errors=True)
                store.write_text("workers/integrator.jsonl", output)
                if timed_out or code:
                    raise RuntimeError("Sol integrator failed or timed out")
            git(final_path, "add", "-N", ".", check=False)
            diff_check = git(final_path, "diff", "--check", "HEAD", check=False)
            if diff_check.returncode:
                raise RuntimeError("final git diff --check failed")
            final_patch = capture_patch(final_path)
            self.engine.guard.reject_added_credentials(final_patch)
            final_tests = [
                run_test_command(
                    final_path, command, self.request.worker_timeout, "final"
                )
                for command in commands
            ]
            final_tests.append(
                CommandReceipt(
                    command="git diff --check HEAD",
                    exit_code=diff_check.returncode,
                    output=diff_check.stdout + diff_check.stderr,
                    phase="final",
                )
            )
            if self.request.verification_mode != "docs" and not commands:
                verdict.blockers.append(
                    "implementation semantic commit requires an explicit deterministic test command"
                )
                verdict.finality = "abort"
                verdict.abstained = True
                store.write_json("verdict.json", verdict)
                self.engine.manifest.status = "blocked"
                self.engine.manifest.finality = "abort"
                self.engine.manifest.completed_at = datetime.now(timezone.utc)
                self.engine.persist_manifest()
                return ProtocolResult(
                    self.engine.run_id,
                    verdict,
                    self.engine.exclusions,
                    self.engine.manifest,
                )
            if commands and not all(
                item.exit_code == 0 and not item.timed_out for item in final_tests
            ):
                raise RuntimeError("final engine-owned tests failed")
            store.write_json("receipts/final-tests.json", final_tests)
            final_receipt_ids = [
                "FT-"
                + sha256_text(
                    item.command
                    + str(item.exit_code)
                    + str(item.timed_out)
                    + item.output
                )[:12]
                for item in final_tests
                if item.exit_code == 0 and not item.timed_out
            ]
            for contribution in graph.contributions:
                if contribution.id in set(graph.selected_ids):
                    contribution.verification_receipt_ids = sorted(
                        set(contribution.verification_receipt_ids + final_receipt_ids)
                    )
                    contribution.provenance = sorted(
                        set(contribution.provenance + ["patches/final.patch"])
                    )
            verdict.evidence_refs = sorted(
                set(verdict.evidence_refs + final_receipt_ids)
            )
            store.write_json("contribution-graph.json", graph)
            store.write_json("verdict.json", verdict)
            required_completion_reviews = 2 if contract.risk_level == "high" else 1
            completions = []
            excluded_completion_families = {self.engine.judge_route.family}
            for index in range(1, required_completion_reviews + 1):
                completion = await self.completion_review(
                    selected,
                    contract,
                    final_patch,
                    final_tests,
                    excluded_families=excluded_completion_families,
                    index=index,
                )
                completions.append(completion)
                excluded_completion_families.add(completion.family)
                if completion.status != "blocker_free":
                    break
            if len(completions) != required_completion_reviews or any(
                item.status != "blocker_free" for item in completions
            ):
                completion_blockers = [
                    blocker for item in completions for blocker in item.blockers
                ]
                verdict.blockers = sorted(
                    set(
                        verdict.blockers
                        + completion_blockers
                        + (
                            ["independent high-risk validation incomplete"]
                            if len(completions) != required_completion_reviews
                            else []
                        )
                    )
                )
                verdict.decision = "BLOCKED: " + verdict.decision
                store.write_json("verdict.json", verdict)
                self.engine.manifest.status = "blocked"
                self.engine.manifest.completed_at = datetime.now(timezone.utc)
                self.engine.persist_manifest()
                return ProtocolResult(
                    self.engine.run_id,
                    verdict,
                    self.engine.exclusions,
                    self.engine.manifest,
                )
            store.write_text("patches/final.patch", final_patch)
            store.write_json("private/identity-map.json", self.engine.identity_map)
            git(final_path, "add", "-A")
            commit_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(final_path),
                    "-c",
                    "user.name=ccycouncil",
                    "-c",
                    "user.email=ccycouncil@local",
                    "commit",
                    "-m",
                    f"ccycouncil: {contract.objective[:60]}",
                ],
                text=True,
                capture_output=True,
            )
            if commit_result.returncode:
                raise RuntimeError(
                    "engine-owned final commit failed: " + commit_result.stderr
                )
            final_commit = resolve_base(final_path, "HEAD")
            parent = git(final_path, "rev-parse", "HEAD^").stdout.strip()
            count = int(
                git(
                    final_path,
                    "rev-list",
                    "--count",
                    f"{self.base_commit}..HEAD",
                ).stdout.strip()
            )
            if parent != self.base_commit or count != 1:
                raise RuntimeError("final result is not exactly one commit on the base")
            store.write_text("final-commit.txt", final_commit + "\n")
            self.engine.manifest.final_branch = branch
            self.engine.manifest.final_commit = final_commit
            verdict.finality = "semantic_commit"
            verdict.abstained = False
            verdict.judgment_risk = min(self.engine.manifest.judgment_risk, 0.05)
            self.engine.manifest.finality = "semantic_commit"
            self.engine.manifest.judgment_risk = verdict.judgment_risk
            self.engine.manifest.abstained = False
            selective_payload = store.read_json("selective-judgment.json")
            selective_payload.update({
                "accepted": True,
                "abstained": False,
                "confidence_low": verdict.confidence,
                "confidence_high": verdict.confidence,
                "reasons": [
                    "cold-start implementation accepted by deterministic final tests "
                    "and independent completion review"
                ],
            })
            store.write_json("selective-judgment.json", selective_payload)
            store.write_json("verdict.json", verdict)
            self.engine.manifest.status = "completed"
            self.engine.manifest.completed_at = datetime.now(timezone.utc)
            store.append_event(
                "completed",
                final_branch=branch,
                final_commit=final_commit,
            )
            keep_final = True
            self.engine.persist_manifest()
            return ProtocolResult(
                self.engine.run_id,
                verdict,
                self.engine.exclusions,
                self.engine.manifest,
            )
        except BaseException as error:
            self.engine.manifest.status = "failed"
            self.engine.manifest.completed_at = datetime.now(timezone.utc)
            store.append_event(
                "failed", error=self.engine.guard.redact_text(str(error))[:1000]
            )
            self.engine.persist_manifest()
            raise
        finally:
            self.cleanup(keep_final)
            self.engine.persist_manifest()
            await self.engine.close()


async def run_implementation(
    request: ImplementationRequest,
    *,
    state: Path = STATE,
    settings: ProxySettings | None = None,
    transport_factory: type[ProxyTransport] = ProxyTransport,
) -> ProtocolResult:
    return await ImplementationEngine(
        request,
        state=state,
        settings=settings,
        transport_factory=transport_factory,
    ).run()


def apply_run(
    state: Path,
    run_id: str,
    repo: str | Path,
    settings: ProxySettings | None = None,
) -> str:
    guard = SecretGuard(settings.exact_secrets) if settings else SecretGuard()
    store = RunStore.open_existing(state, run_id, guard=guard)
    manifest = store.read_json("manifest.json")
    if manifest.get("schema_version") != 4:
        raise RuntimeError("apply supports schema-v4 runs only")
    verdict = store.read_json("verdict.json")
    if verdict.get("finality") != "semantic_commit":
        raise RuntimeError("apply requires an implementation semantic_commit")
    certificate = store.read_json("finality-certificate.json")
    if (
        certificate.get("finality") != "semantic_commit"
        or not certificate.get("accepted")
        or certificate.get("unresolved_claim_ids")
        or not certificate.get("deterministic_receipt_ids")
    ):
        raise RuntimeError("apply requires a clean reproducible finality certificate")
    taint_path = store._target("taint-state.json")
    if taint_path.exists() and store.read_json("taint-state.json").get("tainted_ids"):
        raise RuntimeError("apply refuses tainted implementation lineage")
    expected_repo = Path(manifest.get("repo") or "").resolve()
    actual_repo = repository_root(repo)
    if actual_repo != expected_repo:
        raise RuntimeError("apply was invoked from the wrong repository")
    status = git(
        actual_repo, "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout
    if status.strip():
        raise RuntimeError("apply requires a clean checkout")
    head = resolve_base(actual_repo, "HEAD")
    if head != manifest.get("base_commit"):
        raise RuntimeError("checkout is not at the unchanged original base")
    commit = manifest.get("final_commit")
    if not commit or manifest.get("status") != "completed":
        raise RuntimeError("run has no valid completed final artifact")
    exists = git(actual_repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False)
    if exists.returncode:
        raise RuntimeError("final commit object is unavailable")
    parent = git(actual_repo, "rev-parse", f"{commit}^").stdout.strip()
    if parent != head:
        raise RuntimeError("final artifact is not based directly on the checkout")
    result = git(actual_repo, "cherry-pick", commit, check=False)
    if result.returncode:
        git(actual_repo, "cherry-pick", "--abort", check=False)
        restored = resolve_base(actual_repo, "HEAD")
        if restored != head:
            raise RuntimeError("cherry-pick failed and checkout restoration failed")
        raise RuntimeError("cherry-pick conflicted and was aborted")
    return commit
