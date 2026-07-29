# Reason Assembly v0.5.1 Hardening — Phased Implementation Plan

**Branch:** `feat/qoder-qwen38-hardening`
**Base:** `7fb8b31` (release 0.5.1)
**Scope:** All actionable recommendations from `qa/qoder-qwen-3.8-max-preview-review.md`
**Constraint envelope:** Preserve v0.5.1 behavior, protocol/finality/security invariants, aliases, legacy env/state compatibility; non-destructive migration; no live API calls; no push/merge/publish/tag; no secrets in artifacts; incremental shims over rewrites.

---

## Phase 0: Scaffolding & Test Infrastructure

**Goal:** Establish the branch-local test harness additions needed by all subsequent phases without touching production code paths.

### Files/Components

| File | Action |
|------|--------|
| `tests/conftest.py` | Extend with `tmp_state_root` fixture (isolated `tmp_path`-based state), `fake_codex` fixture (shell script stub) |
| `tests/helpers.py` | New: shared utilities — `make_manifest_v4()`, `write_run_artifacts()`, `concurrent_outcome()` |
| `pyproject.toml` | Add `hypothesis` to `[dependency-groups] dev` only (test-time; never a runtime dep) |

### Invariants

- No production module is imported differently.
- `pythonpath = ["scripts"]` remains unchanged.
- No new runtime dependencies added to `[project] dependencies`.

### Acceptance Criteria

- `uv run pytest -q` passes with existing suite plus new fixtures exercised by a trivial smoke test.
- `hypothesis` importable in test context only; `uv run python -c "import hypothesis"` succeeds; `python -c "import hypothesis"` in a wheel venv fails.

### Tests

- `tests/test_helpers.py`: fixture smoke tests.

### Migration/Compatibility

- None; test-only.

### Checkpoint

```
git tag: (none — local only)
gate: uv run pytest -q && uv run ruff check .
```

---

## Phase 1: Locked & Transactional State Updates (C3, H4)

**Goal:** Eliminate lost-update races on all mutable shared stores; make `outcome_command` crash-safe.

### 1A — Advisory Locking for Shared Stores

#### Files/Components

| File | Change |
|------|--------|
| `scripts/v4_state.py` | Add `LockedJsonStore` wrapper: `fcntl.flock(LOCK_EX)` around read-modify-write cycles in `PrivateJsonStore.write` and `initialize`. Expose context-manager `locked_write(mutator: Callable[[dict], dict])`. |
| `scripts/reliability.py` | `ReliabilityStore.write` → acquire exclusive flock on `reliability.json.lock` sibling file before temp-write + replace. |
| `scripts/v4.py` | `CoFailureStore` (if write path exists) and `CalibrationStore`, `OperationEffectStore` — same pattern via shared helper. |
| `scripts/state_compat.py` | Export `flock_exclusive(path) -> ContextManager` helper (already imports `fcntl`). |

#### Design

```python
# state_compat.py addition
@contextmanager
def flock_exclusive(lock_path: Path, timeout: float = 10.0) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock contention: {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
```

Lock files are siblings (`<store>.lock`), never the data file itself, so atomic replace never invalidates the lock fd.

#### Invariants

- Existing `PrivateJsonStore.initialize` os.link semantics preserved (create-if-absent is still race-free).
- Lock timeout raises `TimeoutError`, not silent corruption.
- No schema change; no `schema_version` bump.

#### Tests

- `tests/test_concurrency.py`: spawn 8 processes via `multiprocessing` each calling `ReliabilityStore.write` with distinct bucket; assert final JSON contains all 8.
- Same for `PrivateJsonStore.locked_write` on calibration/cofailure/operation-effects.
- Timeout test: hold lock in subprocess, assert `TimeoutError` within 11s.

### 1B — Transactional `outcome_command`

#### Files/Components

| File | Change |
|------|--------|
| `scripts/reason_assembly.py` | Refactor `outcome_command` (lines 747–1102): collect all mutations into a `TransactionPlan` dataclass; apply via `RunStore.write_transaction(plan)` that writes all artifacts under a single run-level flock, then updates manifest last. |
| `scripts/artifacts.py` | Add `RunStore.write_transaction(mutations: list[tuple[str, Any]])` — acquires `<run_root>/.run.lock`, writes all files atomically, releases. |

#### Design

- Read phase: gather genealogy, verdict, certificate, rollout-card, identity-map, ledger (read-only).
- Plan phase: compute new JSON payloads in memory.
- Write phase: under exclusive run lock, write all changed files, then `manifest.json` last (manifest is the commit point).
- Crash before manifest write → next invocation sees stale manifest → idempotent retry safe because `outcome.json` existence check gates re-entry.

#### Invariants

- `outcome.json` existence check remains the idempotency guard.
- Taint propagation logic unchanged; only write ordering changes.
- Finality downgrade rules unchanged.

#### Tests

- `tests/test_outcome_transaction.py`: simulate crash (raise mid-write via monkeypatch after 2nd file); assert either all-or-nothing visible on re-read.
- Existing `tests/test_reason_assembly_migration.py` continues passing.

### Migration/Compatibility

- Lock files are new siblings; ignored by `RunStore.artifact_names()` (dot-prefixed).
- No state format change; existing state directories work unmodified.

### Checkpoint

```
gate: uv run pytest -q tests/test_concurrency.py tests/test_outcome_transaction.py
gate: uv run pytest -q  (full suite)
```

---

## Phase 2: Proxy Retries & Failure Classification (H3)

**Goal:** Transient proxy failures don't kill runs; budget not consumed on retried attempts.

### Files/Components

| File | Change |
|------|--------|
| `scripts/transport.py` | Extract retry loop around the `while True` body in `ProxyTransport.ask`. Add `_RETRYABLE_STATUS = {500, 502, 503, 504}` and `_RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)`. |
| `scripts/transport.py` | Add `RetryPolicy` dataclass: `max_attempts=3`, `base_delay=1.0`, `max_delay=8.0`, `jitter=True`. |
| `scripts/transport.py` | Budget consumption moves inside the success path only (after 2xx confirmed). Current code consumes before the request — move `self.budget.consume(stage, model)` to after `response.is_error` check passes. |

### Design

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    backoff_factor: float = 2.0

    def delay(self, attempt: int) -> float:
        raw = min(self.base_delay * (self.backoff_factor ** attempt), self.max_delay)
        return raw * (0.5 + random.random() * 0.5)  # jitter
```

Classification:
- **Transient (retry):** 500, 502, 503, 504, `httpx.TimeoutException`, `httpx.ConnectError`, `httpx.ReadError`.
- **Permanent (raise immediately):** 400 (non-context), 401, 403, 404, 422, 429 (`QuotaError`).
- **Context overflow (special):** 400 with context markers → existing repack logic (unchanged).

Budget rule: `budget.consume()` is called exactly once per logical ask, only when a 2xx response is received. Retried attempts do not consume.

### Invariants

- `QuotaError` (429) never retried — preserves budget-protection semantics.
- `ContextError` repack logic unchanged (one repack attempt).
- Total wall-clock bounded: 3 attempts × (request timeout + max 8s delay) ≤ ~30s overhead.
- No new public API; `RetryPolicy` is internal, overridable via constructor kwarg for tests.

### Tests

- `tests/test_transport.py` additions:
  - `test_retry_on_500_then_success`: FakeTransport returns 500, 500, 200 → assert success, budget consumed once.
  - `test_no_retry_on_429`: immediate `QuotaError`.
  - `test_no_retry_on_400_non_context`: immediate `ProxyCallError`.
  - `test_budget_not_consumed_on_exhausted_retries`: 3×500 → `ProxyCallError`, budget unchanged.
  - `test_backoff_delays_monotonic`: assert computed delays within bounds.

### Migration/Compatibility

- Behavioral change is strictly additive (previously: fail on first 5xx; now: retry then fail). No config file needed for v0.5.1; `RetryPolicy` defaults are internal.
- Existing `FakeTransport` in `tests/conftest.py` unaffected (it never returns 5xx).

### Checkpoint

```
gate: uv run pytest -q tests/test_transport.py
gate: uv run pytest -q
```

---

## Phase 3: Remove `shell=True` from Command Verification (C2, M6)

**Goal:** Default to argv-based execution; provide explicit opt-in shell path for backward compatibility.

### Files/Components

| File | Change |
|------|--------|
| `scripts/verification.py` | `run_command_verifier`: parse `step.executor_input` with `shlex.split()` by default; pass `shell=False`. Add `allow_shell: bool = False` parameter. When `allow_shell=True`, retain current `shell=True` + `command_shell()` path. |
| `scripts/verification.py` | Replace hardcoded PATH (`/opt/homebrew/bin:...`) with `os.defpath` fallback + optional `REASON_ASSEMBLY_VERIFY_PATH` env override. |
| `scripts/contracts.py` | `VerificationStep` gains optional field `shell: bool = False` (schema-additive; existing manifests deserialize with default `False`). |
| `scripts/reason_assembly.py` | `--verify-command` CLI: add `--verify-shell` flag (default off). When set, marks step `shell=True` and emits a stderr warning. |
| `scripts/protocols.py` | Pass `allow_shell=step.shell` through to `run_command_verifier`. |

### Design

```python
def run_command_verifier(step, *, cwd, timeout=120, allow_shell=False):
    safe_env = _build_safe_env()
    if allow_shell:
        argv = step.executor_input
        kwargs = {"shell": True, "executable": command_shell()}
    else:
        argv = shlex.split(step.executor_input)
        kwargs = {"shell": False}
    result = subprocess.run(argv, cwd=cwd, env=safe_env, text=True,
                            capture_output=True, timeout=timeout, **kwargs)
```

PATH construction:

```python
def _build_safe_env() -> dict[str, str]:
    extra = os.environ.get("REASON_ASSEMBLY_VERIFY_PATH", "")
    base = os.defpath  # platform default (/usr/bin:/bin or similar)
    path = ":".join(filter(None, [extra, base]))
    return {"PATH": path, "LANG": "C.UTF-8", ...}
```

### Invariants

- Existing `--verify-command "pytest -x"` continues working (shlex.split handles simple cases).
- Commands requiring shell features (pipes, globs) require explicit `--verify-shell`; without it, they fail with a clear error message suggesting the flag.
- Receipt schema unchanged (`command_exit_code`, `observation`, etc.).
- Sandboxed env still strips proxy vars.

### Tests

- `tests/test_v4_verification.py` additions:
  - `test_argv_execution_no_shell`: command `echo hello` → success, no shell invoked (verify via `executor_input` containing `;` doesn't execute second command).
  - `test_shell_opt_in`: `shell=True` step with pipe → works.
  - `test_injection_blocked_without_shell`: `echo hi; rm -rf /` → treated as literal args, fails harmlessly.
  - `test_custom_path_env`: set `REASON_ASSEMBLY_VERIFY_PATH`, assert binary found.

### Migration/Compatibility

- **Backward compat:** Stored manifests with `VerificationStep` lacking `shell` field deserialize with `False` (Pydantic default). No schema bump.
- **Behavioral note:** Users who relied on shell features in `--verify-command` will see a clear error directing them to `--verify-shell`. Documented in CHANGELOG.

### Checkpoint

```
gate: uv run pytest -q tests/test_v4_verification.py
gate: uv run pytest -q
```

---

## Phase 4: Configurable Model Preferences & Role/Route Identities (C1)

**Goal:** Remove hard-coded operator-specific model IDs from routing logic; externalize to versioned config with safe defaults.

### Files/Components

| File | Change |
|------|--------|
| `scripts/routing.py` | `PREFERENCES` list → loaded from `RoutingPolicy` object. Module-level `PREFERENCES` retained as `_FALLBACK_PREFERENCES` (empty list by default in shipped config; current list preserved only as a test fixture). |
| `scripts/routing.py` | Add `RoutingPolicy` dataclass: `preferences: list[str]`, `judge_model: str | None`, `luna_model: str | None`, `integrator_model: str | None`. Loaded from TOML file. |
| `scripts/identity.py` | Add `ROUTING_POLICY_ENV = "REASON_ASSEMBLY_ROUTING_POLICY"` and `LEGACY_ROUTING_POLICY_ENV = "CCYCOUNCIL_ROUTING_POLICY"`. |
| `scripts/transport.py` | `ProxyTransport.__init__` accepts optional `routing_policy: RoutingPolicy`; passes to scoring. |
| `scripts/protocols.py` | Judge/luna/integrator model selection reads from policy; falls back to catalogue-derived best-fit when policy field is `None`. |
| New: `scripts/routing_policy.py` | `load_routing_policy(environ) -> RoutingPolicy`: reads TOML from env path or `<state_root>/routing-policy.toml`; validates against catalogue at sync time (not at import). Ships a commented example. |
| `references/routing-policy.example.toml` | Documented example with no real model IDs. |

### Design

```toml
# routing-policy.toml (operator-supplied)
# All fields optional; omitted fields use catalogue-derived defaults.
preferences = []  # ordered model IDs for role_fit bias; empty = pure capability scoring

[roles]
judge = null       # e.g. "model-id:effort" or null for auto-select
luna = null        # utility extraction model
integrator = null  # implementation integration model
```

Fallback behavior when no policy file exists:
- `preferences = []` → `role_fit` score derived purely from catalogue metadata (capabilities, family diversity).
- `judge/luna/integrator = None` → highest-scored healthy route for that role (existing behavior minus hard-coded ID).

### Invariants

- Scoring formula `0.45*role_fit + 0.30*reliability + 0.20*independence + 0.05*latency` unchanged.
- Family diversity quorum ≥2 unchanged.
- Exploration promotion (20% by run_id hash) unchanged.
- No live catalogue sync during tests (FakeTransport provides static catalogue).

### Tests

- `tests/test_routing_policy.py`:
  - `test_default_policy_no_preferences`: empty policy → scoring uses capability only.
  - `test_policy_file_override`: write temp TOML, assert preferences influence score.
  - `test_invalid_model_in_policy_warns`: policy references model not in catalogue → warning, not crash.
  - `test_legacy_env_var`: `CCYCOUNCIL_ROUTING_POLICY` respected.
- Existing routing tests pass with `_FALLBACK_PREFERENCES` injected via fixture.

### Migration/Compatibility

- No policy file → behavior equivalent to current code **minus** the hard-coded IDs (which is the fix).
- Operators who depend on current IDs can recreate them in `routing-policy.toml`.
- No state migration needed.

### Checkpoint

```
gate: uv run pytest -q tests/test_routing_policy.py tests/test_core.py
gate: uv run pytest -q
```

---

## Phase 5: Dependency Preflight & Configurable Worker Executable (H5)

**Goal:** Fail fast with actionable diagnostics when external worker binary is missing; make binary path configurable.

### Files/Components

| File | Change |
|------|--------|
| `scripts/git_worker.py` | `invoke_codex`: replace hardcoded `"codex"` with `settings.worker_executable` (default `"codex"`). Add `preflight_worker(settings) -> None` that runs `shutil.which(exe)` and `subprocess.run([exe, "--version"], ...)` with 5s timeout; raises `WorkerUnavailableError` with install instructions on failure. |
| `scripts/identity.py` | Add `WORKER_EXECUTABLE_ENV = "REASON_ASSEMBLY_WORKER"` and `LEGACY_WORKER_EXECUTABLE_ENV = "CCYCOUNCIL_WORKER"`. |
| `scripts/reason_assembly.py` | `implement_command`: call `preflight_worker()` before any worktree creation. |
| `scripts/contracts.py` | `ProxySettings` (or equivalent settings dataclass) gains `worker_executable: str = "codex"`. |

### Design

```python
class WorkerUnavailableError(RuntimeError):
    def __init__(self, executable: str):
        super().__init__(
            f"worker executable '{executable}' not found on PATH.\n"
            f"Install it or set REASON_ASSEMBLY_WORKER to an absolute path.\n"
            f"See: https://github.com/openai/codex (or equivalent backend)."
        )
```

### Invariants

- `invoke_codex` argv construction unchanged (only the binary path is parameterized).
- Ephemeral `CODEX_HOME` lifecycle unchanged.
- Environment stripping unchanged.
- No network calls in preflight (only `--version`).

### Tests

- `tests/test_git_worker.py` additions:
  - `test_preflight_missing_binary`: monkeypatch `shutil.which` → `None`; assert `WorkerUnavailableError`.
  - `test_preflight_success`: fake script on PATH → passes.
  - `test_custom_executable_env`: set env var, assert used.

### Migration/Compatibility

- Default remains `"codex"`; no behavior change for existing users.
- Env var is additive.

### Checkpoint

```
gate: uv run pytest -q tests/test_git_worker.py
gate: uv run pytest -q
```

---

## Phase 6: Proper Package Layout (H1)

**Goal:** Eliminate top-level namespace pollution while preserving all import paths for the transition period.

### Files/Components

| File | Change |
|------|--------|
| New: `src/reason_assembly/__init__.py` | Package root; re-exports `main`, `legacy_main`, `__version__`. |
| `src/reason_assembly/*.py` | All 15 modules moved from `scripts/` into package. Internal imports become relative (`from .contracts import ...`). |
| `scripts/` | Retained as thin shims: each file does `from reason_assembly.<module> import *` for one release cycle. |
| `pyproject.toml` | Replace `py-modules` with `[tool.setuptools.packages.find] where = ["src"]`. Keep `package-dir` removed. Console scripts → `reason_assembly:main` / `reason_assembly:legacy_main`. |
| `pyproject.toml` | `pythonpath` in pytest → `["src"]` (shims in `scripts/` still importable for legacy tests). |
| `src/reason_assembly/py.typed` | Empty marker file (addresses L4). |

### Design

Transition strategy:
1. Move source to `src/reason_assembly/`.
2. `scripts/*.py` become one-line re-export shims (e.g., `from reason_assembly.routing import *  # noqa: F401,F403`).
3. Wheel installs only the package (no top-level `routing.py`, `v4.py`, etc.).
4. Shims exist for source-checkout users and are removed in v0.7.0.

### Invariants

- `reason-assembly --version` and `ccycouncil --version` output unchanged.
- All data-files paths unchanged.
- `bin/reason-assembly` launcher updated to `python -m reason_assembly` (or retains `scripts/reason_assembly.py` shim path).
- CI smoke test assertions unchanged.

### Tests

- Existing full suite passes (imports resolve via `pythonpath = ["src"]`).
- New `tests/test_package_layout.py`:
  - `test_no_toplevel_modules_in_wheel`: build wheel, inspect `RECORD`, assert no top-level `.py` outside `reason_assembly/`.
  - `test_shim_imports`: `import routing` still works with scripts on path (deprecation warning optional).

### Migration/Compatibility

- Source installs via `bin/reason-assembly` continue working (shim).
- Wheel installs get clean namespace.
- No user state affected.

### Checkpoint

```
gate: uv run pytest -q
gate: uv run python -m compileall -q src tests
gate: uv build --wheel && inspect RECORD
gate: sh -n bin/reason-assembly bin/ccycouncil
```

---

## Phase 7: CouncilEngine & Outcome Decomposition (H2, H4)

**Goal:** Reduce `protocols.py` from ~1400 lines to an orchestration shell; extract testable pure-logic components.

### Files/Components

| File | Extracted From | Responsibility |
|------|---------------|----------------|
| `src/reason_assembly/protocol/judgment.py` | `protocols.py` judging section | Mirrored judging, ballot collection, tiebreaker |
| `src/reason_assembly/protocol/finality.py` | `protocols.py` + `v4.py` | Certificate construction, taint application |
| `src/reason_assembly/protocol/evidence.py` | `protocols.py` evidence section | Packing, extraction orchestration |
| `src/reason_assembly/protocol/hypotheses.py` | `protocols.py` hypothesis section | Independent proposal gathering |
| `src/reason_assembly/cli/outcome.py` | `reason_assembly.py:747-1102` | Outcome attribution logic (pure functions + I/O boundary) |

### Design Principles

- `CouncilEngine` retains orchestration (sequencing, budget, transport calls) but delegates to extracted modules.
- Extracted modules are pure where possible (accept data, return data); I/O at boundaries.
- No behavioral change; extraction is mechanical with import updates.
- Each extraction is a separate commit for bisectability.

### Invariants

- All protocol invariants (rubric lock, mirrored judging, family quorum, selective judgment thresholds) unchanged.
- Public CLI interface unchanged.
- Manifest/artifact format unchanged.

### Tests

- Existing suite passes after each extraction commit.
- New unit tests for `judgment.py` and `finality.py` with synthetic data (no FakeTransport needed).
- `tests/test_outcome_attribution.py`: pure-function tests for attribution logic extracted from `outcome_command`.

### Migration/Compatibility

- Internal refactor only; no user-visible change.
- Shim modules in `scripts/` updated to re-export from new locations.

### Checkpoint

```
gate: uv run pytest -q (after each extraction commit)
gate: uv run ruff check .
gate: wc -l src/reason_assembly/protocols.py  # target: < 600
```

---

## Phase 8: Structured Observability & Machine-Readable Progress (M5)

**Goal:** Replace ad-hoc stderr prints with structured logging; add `--json` progress mode.

### Files/Components

| File | Change |
|------|--------|
| New: `src/reason_assembly/observability.py` | `get_logger(name) -> logging.Logger` with JSON formatter (stdlib `logging` + `json.dumps`; no new deps). `ProgressEmitter` class: emits `{"stage": ..., "status": ..., "run_id": ..., "ts": ...}` lines to stderr when `--json-progress` is set. |
| `src/reason_assembly/reason_assembly.py` | Add `--json-progress` global flag; wire to `ProgressEmitter`. Add `--log-level` (default WARNING). |
| `src/reason_assembly/protocols.py` | Replace `print(..., file=sys.stderr)` with `logger.info(...)` / `emitter.stage(...)`. |
| `src/reason_assembly/git_worker.py` | Same treatment for implement stages. |

### Design

- Uses only stdlib `logging` — no new runtime dependency.
- JSON progress is opt-in; default behavior (human stderr) unchanged.
- Log levels: DEBUG (all transport payloads redacted), INFO (stage transitions), WARNING (fallbacks, retries), ERROR (failures).
- `SecretGuard.redact()` applied to all log messages via a logging filter.

### Invariants

- Default CLI output unchanged (no `--json-progress` → same stderr as today).
- No secrets in logs at any level (filter enforced).
- `events.jsonl` audit log unchanged (it is the authoritative record; logging is diagnostic).

### Tests

- `tests/test_observability.py`:
  - `test_json_progress_output`: run with `--json-progress`, parse stderr lines as JSON, assert stage sequence.
  - `test_secret_redaction_in_logs`: inject fake API key, assert absent from captured logs.
  - `test_default_stderr_unchanged`: without flag, output matches snapshot.

### Migration/Compatibility

- Additive flags; no existing behavior altered.

### Checkpoint

```
gate: uv run pytest -q tests/test_observability.py
gate: uv run pytest -q
```

---

## Phase 9: Demo UX & Safe Calibration Bootstrap (M7)

**Goal:** New users can produce a visible result without a live proxy; calibration cold-start doesn't permanently block.

### 9A — Demo Mode

| File | Change |
|------|--------|
| `src/reason_assembly/reason_assembly.py` | Add `--demo` flag to `decide`/`review`/`red-team`. When set, uses `DemoTransport` (canned responses, no network). |
| New: `src/reason_assembly/demo.py` | `DemoTransport` implementing the transport interface with fixture responses. Produces a complete run with `verdict_commit` finality. |
| `references/demo-evidence/` | Small static evidence files for demo runs. |

### 9B — Calibration Bootstrap (Conservative)

| File | Change |
|------|--------|
| `src/reason_assembly/v4.py` | Add `provisional` field to `SelectiveJudgmentReceipt` (default `False`). When calibration examples < threshold AND anchor validation passes, allow acceptance with `provisional=True` and `confidence` capped at 0.50 (below cold-start 0.65 cap). |
| `src/reason_assembly/v4.py` | Provisional acceptance **does not** grant `semantic_commit` — only `verdict_commit`. Implementation tasks still require full calibration for `semantic_commit`. |
| `src/reason_assembly/contracts.py` | `SelectiveJudgmentReceipt` gains `provisional: bool = False`. |

### Invariants

- **Finality preserved:** `semantic_commit` still requires ≥29 (or ≥59) calibration examples. Provisional path only affects `verdict_commit` for low-risk decisions.
- **Implementation tasks:** No change — abstain without deterministic/independent evidence regardless of calibration.
- **Taint/apply:** Unchanged; provisional verdicts can still be tainted and downgraded.
- Demo mode never writes to real state root (uses temp dir).

### Tests

- `tests/test_demo.py`: `decide --demo` completes, produces valid manifest, no network calls.
- `tests/test_v4_selective.py` additions:
  - `test_provisional_acceptance_below_threshold`: < 29 examples + anchors pass → `provisional=True`, finality=`verdict_commit`.
  - `test_provisional_never_semantic_commit`: implementation task + provisional → still abstains.
  - `test_full_calibration_unchanged`: ≥29 examples → non-provisional path identical to current.

### Migration/Compatibility

- `provisional` field defaults to `False`; existing receipts deserialize unchanged.
- Demo mode is opt-in; no effect on normal operation.

### Checkpoint

```
gate: uv run pytest -q tests/test_demo.py tests/test_v4_selective.py
gate: uv run pytest -q
```

---

## Phase 10: Pluggable Worker Backends (H5 extension)

**Goal:** Decouple implementation engine from the `codex` binary via a protocol interface.

### Files/Components

| File | Change |
|------|--------|
| New: `src/reason_assembly/workers/backend.py` | `WorkerBackend` Protocol: `execute(prompt, cwd, env, timeout) -> WorkerResult`. `WorkerResult` dataclass: `returncode`, `output`, `timed_out`. |
| New: `src/reason_assembly/workers/codex.py` | `CodexBackend(WorkerBackend)` — current `invoke_codex` logic. |
| New: `src/reason_assembly/workers/subprocess_backend.py` | `SubprocessBackend(WorkerBackend)` — generic argv execution for testing/alternative tools. |
| `src/reason_assembly/git_worker.py` | `ImplementationEngine` accepts `backend: WorkerBackend`; defaults to `CodexBackend`. |

### Invariants

- Default behavior unchanged (CodexBackend is default).
- Worktree isolation, env stripping, credential scanning all unchanged.
- Backend selection via `REASON_ASSEMBLY_WORKER_BACKEND` env (values: `codex`, `subprocess`).

### Tests

- `tests/test_git_worker.py`: existing tests pass with `CodexBackend` (mocked binary).
- `tests/test_worker_backends.py`: `SubprocessBackend` with `echo` → `WorkerResult(returncode=0, ...)`.

### Migration/Compatibility

- Additive; default unchanged.

### Checkpoint

```
gate: uv run pytest -q tests/test_worker_backends.py tests/test_git_worker.py
gate: uv run pytest -q
```

---

## Phase 11: Property-Based & Adversarial Tests (M2)

**Goal:** Fuzz critical parsing/aggregation paths without adding runtime dependencies.

### Files/Components

| File | Target |
|------|--------|
| `tests/test_property_extract_json.py` | `hypothesis` strategies for `extract_json`: arbitrary unicode, nested braces, multiple JSON objects, truncation. Assert: never raises unhandled exception; returns valid JSON or `None`. |
| `tests/test_property_aggregation.py` | `aggregate_ballots` with random ballot counts/labels. Assert: deterministic, no IndexError, winner always in candidate set. |
| `tests/test_property_taint.py` | Random DAGs fed to `propagate_taint`. Assert: taint is monotonic (superset of seeds), no cycles cause infinite loop. |
| `tests/test_property_calculation.py` | `calculate()` with random arithmetic expressions. Assert: never executes arbitrary code (no `__import__`, no attribute access). |

### Invariants

- `hypothesis` is dev-only (`[dependency-groups] dev`); never in wheel.
- Tests use `@settings(max_examples=200, deadline=5000)` to keep CI fast.
- No production code changes (these tests validate existing robustness; fixes go in separate commits if they find bugs).

### Tests

- All four files above.

### Migration/Compatibility

- Test-only; no production change.

### Checkpoint

```
gate: uv run pytest -q tests/test_property_*.py
gate: uv run pytest -q
```

---

## Phase 12: Tamper-Evident Manifests (Security Extension)

**Goal:** Detect post-hoc modification of run artifacts without requiring key infrastructure.

### Files/Components

| File | Change |
|------|--------|
| `src/reason_assembly/artifacts.py` | `RunStore.seal_manifest()`: compute SHA-256 over sorted artifact hashes; write `manifest.json` field `integrity_sha256`. |
| `src/reason_assembly/artifacts.py` | `RunStore.verify_integrity() -> list[str]`: recompute and compare; return list of mismatched artifact names. |
| `src/reason_assembly/reason_assembly.py` | `show_command`: call `verify_integrity()`; emit warning if mismatches found. |
| New: `src/reason_assembly/signing.py` | Optional Ed25519 signing: `sign_manifest(private_key_path)` / `verify_signature(public_key_path)`. Uses `cryptography` library **only if installed** (optional extra `[project.optional-dependencies] signing = ["cryptography>=42"]`). Without it, integrity hash still works. |
| `src/reason_assembly/identity.py` | Add `SIGNING_KEY_ENV = "REASON_ASSEMBLY_SIGNING_KEY"` (path to key file; never the key itself). |

### Design

Tier 1 (always on, no deps):
- `manifest.json` gains `integrity_sha256` = SHA-256 of canonical JSON of `{artifact_name: sha256}` for all artifacts.
- Computed at run completion; verified on `show`/`apply`/`outcome`.

Tier 2 (optional, requires `cryptography`):
- Ed25519 signature over `integrity_sha256`; stored in `manifest.json` field `signature` (hex).
- Public key path from env or `<state_root>/signing-key.pub`.
- Private key never read by engine; only by explicit `reason-assembly sign <run_id>` command.

### Invariants

- **Backward compat:** Existing manifests lack `integrity_sha256` → `verify_integrity()` returns `["manifest not sealed"]` warning, not error. `apply` still works (warns but doesn't block for unsealed manifests).
- **No key material in state:** Only public key and signatures stored.
- **No schema bump:** Fields are additive to `RunManifest` with `Optional` defaults.

### Tests

- `tests/test_integrity.py`:
  - `test_seal_and_verify`: create run, seal, verify → pass.
  - `test_tamper_detection`: modify artifact after seal → verify returns mismatch.
  - `test_unsealed_manifest_warns`: old manifest → warning, not crash.
  - `test_signing_optional`: without `cryptography` installed, `sign_manifest` raises informative error.
  - `test_key_not_in_state`: assert no private key bytes in any state file.

### Migration/Compatibility

- Additive fields; Pydantic `Optional[str] = None` for `integrity_sha256` and `signature`.
- `apply` command: unsealed → warn; sealed+mismatch → refuse (new behavior, stricter).
- Sealed+match → proceed as before.

### Checkpoint

```
gate: uv run pytest -q tests/test_integrity.py
gate: uv run pytest -q
```

---

## Phase 13: Dead Code Removal & Minor Fixes (L1–L5, §11)

**Goal:** Remove verified dead code; fix low-severity issues.

### Files/Components

| Item | Action |
|------|--------|
| `v4.py:choose_cold_start_operation` | Delete (unreferenced). |
| `v4.py:higher_order_select` | Delete (unreferenced). |
| `contracts.py:CandidateOutcome`, `ReceiptOutcome` | Delete (never instantiated). |
| `routing.py:CALL_CAPS` | Delete alias. |
| `identity.py:RELEASE_TAG` | Delete (unreferenced). |
| `protocols.py:extract_json` (M1) | Harden: try `json.loads` on full text first; fall back to brace-matching with balanced-depth scan; return `None` on ambiguity instead of guessing. |
| `routing.py:shuffled_labels` (L2) | Document that determinism is intentional (fairness, not security); no change needed. |
| `bin/reason-assembly` (L5) | Add comment documenting `uv` requirement for source installs; no runtime change. |

### Invariants

- No behavioral change from dead code removal (verified by grep + test suite).
- `extract_json` hardening is strictly safer (fewer false positives).

### Tests

- Existing suite passes.
- `tests/test_property_extract_json.py` (from Phase 11) validates hardened parser.

### Checkpoint

```
gate: uv run pytest -q
gate: uv run ruff check .
```

---

## Deferred Recommendations

| Recommendation | Justification for Deferral | Safer Incremental Alternative |
|----------------|---------------------------|-------------------------------|
| **Schema v5 / full state restructuring** (§13.3 in review) | Violates hard constraint #7 (no speculative schema bump). Existing v4 state works; a migration runner adds risk without solving a current user problem. | Phase 12's additive `integrity_sha256` field demonstrates the additive-field pattern. If a future v5 is needed, the locked-write infrastructure (Phase 1) makes it safe. |
| **Circuit breaker per model** (§13.2) | Requires persistent cross-run failure state and tuning; risk of false-positive model exclusion breaking quorum. | Phase 2's retry + existing health-check exclusion covers the immediate need. Revisit after observability (Phase 8) provides failure-rate data. |
| **OpenTelemetry integration** (M5 extension) | Adds a runtime dependency tree; most users run single-shot CLI, not long-lived services. | Phase 8's stdlib JSON logging is OTel-compatible (structured JSON to stderr can be collected by any agent). Add OTel SDK only if a service mode is introduced. |
| **`_INSTRUCTION` regex hardening** (L1) | Quarantine is defense-in-depth, not a security boundary; perfect regex is impossible against adversarial unicode. | Document limitation; rely on SecretGuard + taint propagation as the actual safety net. Consider a token-level classifier only if a real bypass is demonstrated. |
| **`stats_command` caching** (M3) | Premature optimization without evidence of scale pain; adds cache-invalidation complexity. | Defer until a user reports >5s stats latency. The fix (mtime-based index file) is straightforward and can be added in isolation. |
| **`gather_with_quorum` grace-period fix** (M4) | Changing timing semantics risks altering deliberation outcomes (different routes completing in different order). | Add a global wall-clock deadline independent of family quorum (configurable, default 120s) in a future release with A/B testing. |

---

## Gate Matrix

| # | Gate | Command / Check | Phases | Pass Criteria |
|---|------|----------------|--------|---------------|
| G1 | Ruff lint | `uv run ruff check .` | All | Zero errors |
| G2 | pytest 3.11 | `uv run --python 3.11 pytest -q` | All | 0 failures |
| G3 | pytest 3.12 | `uv run --python 3.12 pytest -q` | All | 0 failures |
| G4 | pytest 3.13 | `uv run --python 3.13 pytest -q` | All | 0 failures |
| G5 | compileall | `uv run python -m compileall -q src tests` | 6+ | Exit 0 |
| G6 | Wheel build | `uv build --wheel --out-dir dist` | 6+ | Exactly one `.whl` produced |
| G7 | Isolated wheel install | `uv venv .gate-venv && uv pip install --python .gate-venv/bin/python dist/*.whl` | 6+ | Exit 0 |
| G8 | CLI alias: reason-assembly | `.gate-venv/bin/reason-assembly --version` | All | `reason-assembly 0.5.1` |
| G9 | CLI alias: ccycouncil | `.gate-venv/bin/ccycouncil --version 2>err.txt` | All | Same version + deprecation warning on stderr |
| G10 | CLI help | `.gate-venv/bin/reason-assembly --help` | All | Exit 0, lists all subcommands |
| G11 | Skills data files | `test -f .gate-venv/share/reason-assembly/SKILL.md && test -f .gate-venv/share/reason-assembly/compat/model-council/SKILL.md` | 6+ | Files exist |
| G12 | State migration | `uv run pytest -q tests/test_reason_assembly_migration.py tests/test_cli_state.py` | All | 0 failures; no mutation of fixture state |
| G13 | Identity/env compat | `CCYCOUNCIL_STATE=/tmp/x ccycouncil --version` | All | Respects legacy env |
| G14 | Shell syntax | `sh -n bin/reason-assembly bin/ccycouncil` | All | Exit 0 |
| G15 | Hygiene (no secrets) | `grep -rE '(sk-|ghp_|AKIA|BEGIN.*PRIVATE)' src/ tests/ references/ \|\| true` | All | Zero matches |
| G16 | Concurrency | `uv run pytest -q tests/test_concurrency.py` | 1 | 0 failures, no lost updates |
| G17 | Retries | `uv run pytest -q tests/test_transport.py` | 2 | 0 failures; budget-once assertions pass |
| G18 | Observability | `uv run pytest -q tests/test_observability.py` | 8 | JSON progress parseable; no secrets in output |
| G19 | Tamper checks | `uv run pytest -q tests/test_integrity.py` | 12 | Seal/verify/tamper-detect all pass |
| G20 | No top-level pollution | `python -c "import zipimport, pathlib; r=zipimport.zipimporter(next(pathlib.Path('dist').glob('*.whl'))); assert not any(n.endswith('.py') and '/' not in n for n in r._files)"` or inspect RECORD | 6 | No bare `.py` at wheel root |
| G21 | Property tests | `uv run pytest -q tests/test_property_*.py` | 11 | 0 failures within deadline |
| G22 | Demo mode | `uv run pytest -q tests/test_demo.py` | 9 | Completes without network |
| G23 | Worker preflight | `uv run pytest -q tests/test_git_worker.py tests/test_worker_backends.py` | 5, 10 | 0 failures |
| G24 | No live API calls | `uv run pytest -q` with `HTTP_PROXY=http://127.0.0.1:1 HTTPS_PROXY=http://127.0.0.1:1` | All | Suite passes (proves no external calls) |

---

## Execution Order & Dependencies

```
Phase 0 (scaffolding)
  ├── Phase 1 (locking/transactions)
  ├── Phase 2 (retries)
  ├── Phase 3 (shell=True removal)
  ├── Phase 4 (routing policy)
  └── Phase 5 (worker preflight)
        │
        ▼
Phase 6 (package layout) ← depends on 1–5 being stable
  ├── Phase 7 (decomposition) ← depends on 6
  ├── Phase 8 (observability) ← depends on 6
  └── Phase 9 (demo/calibration) ← depends on 6
        │
        ▼
Phase 10 (worker backends) ← depends on 5, 7
Phase 11 (property tests) ← depends on 7 (tests target extracted code)
Phase 12 (tamper-evident) ← depends on 1 (locked writes)
Phase 13 (dead code) ← last, after all behavioral changes settled
```

Each phase is a separate PR-sized unit. Phases 1–5 are independent and can proceed in parallel. Phase 6 is the structural pivot. Phases 7–12 depend on 6 but are mutually independent.

---

## Commit Hygiene Rules

- No private absolute paths in committed code (use `Path.home()` / env vars).
- No credentials, session IDs, or PII in test fixtures (use `example-*` placeholders).
- Commit only the sanitized review and implementation-plan handoff artifacts under `qa/`; keep raw output, prompts, stderr, and session logs untracked.
- Each phase checkpoint is a squash-merge candidate; no force-push after review.
