I've completed my read-only analysis of all source modules, tests, CI, packaging, and documentation. Here is the full report.

---

# Reason Assembly — Deep Architecture & Product Review

## Metadata

| Field | Value |
|-------|-------|
| Model identifier | `Qwen3.8-Max-Preview` (canonical Qoder identifier; verified from CLI session configuration and inference request log) |
| Qoder CLI version | `1.0.45` |
| Internal model key | `qmodel_preview` |
| Qoder session | Recorded outside this sanitized artifact |
| Repository path | `<repository>` |
| Review mode | Read-only (no mutations performed) |
| Review timestamp | 2026-07-29 (per environment context) |
| Branch | `release/v0.5.1` |
| Version | 0.5.1 |

---

## 1. Executive Summary

Reason Assembly is a Python CLI that orchestrates multiple LLM "council members" through a local proxy to produce evidence-backed decisions, code reviews, adversarial analyses, and competing implementations. It is architecturally ambitious—implementing a typed deliberation protocol with claim genealogy, taint propagation, finality certificates, selective judgment with calibration gates, reliability-weighted routing, and Git worktree isolation for implementation candidates.

The system demonstrates strong security hygiene (secret redaction, credential rejection in patches, SSRF protection, atomic file writes, private permissions) and a principled epistemic design (abstention over false confidence, deterministic verification preference, independence requirements). However, it carries significant complexity in a flat module layout with no package structure, hard-coded model preferences that will rot, a single-developer bus factor, and several correctness and reliability gaps identified below.

---

## 2. Architecture Overview

### 2.1 Module Map (all in `scripts/`, installed as top-level py-modules)

| Module | Responsibility |
|--------|---------------|
| `reason_assembly.py` | CLI entry point, argument parsing, command dispatch |
| `protocols.py` | `CouncilEngine` — main deliberation orchestrator (~1400 lines) |
| `git_worker.py` | `ImplementationEngine` — Git worktree workers, codex invocation |
| `routing.py` | Model scoring, candidate pool, quorum gathering |
| `deliberation.py` | Operation selection, ballot aggregation, ranked pairs |
| `transport.py` | HTTP proxy client, budget tracking, catalogue merge |
| `catalogue_sync.py` | Proxy config alias pruning, sync receipts |
| `verification.py` | Source fetching (SSRF-safe), command/calculation/evidence verifiers |
| `contracts.py` | ~60 Pydantic models (schema v4) |
| `artifacts.py` | `RunStore`, `EvidenceInventory`, `SecretGuard` |
| `state_compat.py` | Legacy migration, state locking, run discovery |
| `reliability.py` | Bayesian reliability buckets, time-decay, confidence calibration |
| `v4.py` | Finality, taint, co-failure, approach diversity, bias audit |
| `v4_state.py` | `PrivateJsonStore`, `AnchorStore`, `RouteEpochStore` |
| `identity.py` | Product constants (name, version, env vars, namespaces) |

### 2.2 Execution Flows

**`decide` / `red-team` / `review`:**
```
CLI → CouncilEngine.__init__ → preflight (catalogue sync, health checks, route selection)
  → task_contract (Luna extraction or deterministic fallback)
  → packed_evidence → extract_evidence (optional)
  → hypotheses (independent proposals via quorum)
  → deliberation loop (choose_operation → verify/minority_defense/sample/rebuttal/judge)
  → mirrored judging → aggregation → selective judgment → finality certificate
  → persist_manifest → rollout card → genealogy/taint
```

**`implement`:**
```
CLI → ImplementationEngine → require_clean_base → CouncilEngine(preflight)
  → task_contract → lock_rubric → create worktrees
  → execute_candidate × N (baseline tests → codex test-only → codex implement → final tests)
  → peer_reviews → judge_candidates (mirrored ballots)
  → contribution graph → apply selected patch → integrator (if integrate)
  → final tests → completion_review → semantic_commit → branch preserved
```

**`apply`:**
```
CLI → locate_run_root → validate finality certificate, taint, repo, HEAD
  → cherry-pick --no-commit (user must commit)
```

---

## 3. Persistent State & Schemas

### 3.1 State Layout
```
~/.local/state/reason-assembly/
├── runs/<run_id>/          # per-run artifacts (0700)
│   ├── manifest.json       # RunManifest (schema_version=4)
│   ├── verdict.json
│   ├── events.jsonl        # append-only audit log
│   ├── evidence/
│   ├── hypotheses/
│   ├── verifications/
│   ├── judging/
│   ├── patches/
│   ├── private/            # identity-map, receipts
│   └── ...
├── v4/
│   ├── reliability.json    # Bayesian model-role buckets
│   ├── cofailure.json      # joint failure profiles
│   ├── calibration.json    # confidence examples
│   ├── operation-effects.json
│   ├── anchors.json        # calibration anchors
│   ├── route-epochs.json   # catalogue fingerprint epochs
│   └── sync-receipts/
└── .legacy-ccycouncil-import-v1.json
```

### 3.2 Schema & Migration
- **Schema v4 only.** Schemas 1–3 are explicitly rejected (`load_v4_run` raises). No migration path exists.
- Legacy state (`~/.local/state/ccycouncil`) is non-destructively copied on first CLI invocation via `prepare_state_root`. Only terminal (completed/blocked/failed) runs are imported; incomplete runs remain discoverable read-only.
- All persistent stores use atomic write (temp file + `os.replace` + `fsync`), 0600 permissions, 0700 directories.
- `PrivateJsonStore.initialize` uses `os.link` for create-if-absent semantics, preventing concurrent startup races.

### 3.3 Compatibility
- `ccycouncil` command forwards to `main()` with a deprecation warning (suppressible via env).
- Legacy env vars (`CCYCOUNCIL_STATE`, `CCYPROXY_CONFIG`, `CCYCOUNCIL_EPHEMERAL_KEY`) are accepted.
- `compat/model-council/SKILL.md` provides a deprecated skill alias.

---

## 4. Protocol Design & Finality

### 4.1 Lifecycle State Machine
```
RunManifest.status: running → completed | blocked | failed
Verdict.finality: semantic_commit | verdict_commit | abort
```

**Finality rules** (`v4.py:finality_certificate`):
- `abort` if not accepted, abstained, or unresolved claims exist.
- `semantic_commit` only for `implementation` task kind with deterministic or independent receipts.
- `verdict_commit` otherwise (decision supported but no code action).

### 4.2 Selective Judgment
- Requires ≥29 (or ≥59 for ≤5% risk) calibration examples with exact binomial upper bound ≤ risk threshold.
- Cold-start (uncalibrated) confidence capped at 0.65.
- High-risk/implementation abstains without deterministic or independent evidence.

### 4.3 Taint Propagation
- `ClaimGenealogy` is a DAG of source→extraction→claim→verification→verdict edges.
- Post-outcome invalidation propagates taint forward; if taint reaches `V-final`, verdict is downgraded to abort.
- `apply` refuses tainted lineage.

### 4.4 Invariants (verified in code)
- Rubric locked before proposals (hash committed to manifest).
- Mirrored judging (forward + reversed order) with tiebreaker on inconsistency.
- Family diversity quorum (≥2 families required at multiple gates).
- Deterministic verification preferred over model judgment.
- Contribution graph must be conflict-free and dependency-complete for select/integrate.

---

## 5. Model/Provider Routing

### 5.1 Routing Policy
- **Catalogue authority:** Raw `/v1/models` is authoritative. Metadata-only IDs excluded; raw-only IDs listed-only.
- **Scoring:** `0.45*role_fit + 0.30*reliability + 0.20*independence + 0.05*latency`.
- **Hard-coded preferences:** `PREFERENCES` list in `routing.py:44-55` (gemini-3.1-pro-low, claude-opus-4-6-thinking, qwen3.8-max-preview, etc.) biases role_fit.
- **Judge:** Hard-coded to `gpt-5.6-sol:medium` unless overridden.
- **Luna:** `gpt-5.6-luna:low` as utility-only (task contract extraction).
- **Integrator:** `gpt-5.6-sol:medium` for implementation integration.
- **Exploration:** 20% of runs (deterministic by run_id hash) promote the lowest-scored route.
- **Reliability:** Bayesian posterior with 90-day half-life; active after 8 effective observations; family fallback at 20.

### 5.2 Fallback Behavior
- Health check failure → exclusion record, alternate route selected.
- Judge unhealthy → substituted from healthy pool.
- Luna unhealthy → deterministic task contract fallback.
- Integrator unhealthy → integration unavailable (raises if integrate action selected).
- Schema validation failure → one repair attempt, then RuntimeError.
- Context overflow (400) → one repack attempt with coverage-checked summaries.

---

## 6. Evidence & Verification

### 6.1 Evidence Collection
- `EvidenceInventory` deduplicates by SHA-256, assigns stable `E-<hash12>` IDs.
- HTTPS sources fetched with SSRF protection (DNS validation, non-public IP rejection, redirect re-validation, 5 MiB cap, 10s timeout, no cookies).
- PDF extraction via pypdf; HTML stripped to text; JSON pretty-printed.
- Context packing: priority-ordered, coverage-checked summaries for oversized evidence.

### 6.2 Verification Types
| Kind | Executor | Deterministic |
|------|----------|---------------|
| `command` | `run_command_verifier` (sandboxed env, shell=True) | Yes |
| `calculation` | AST-whitelisted eval (arithmetic only) | Yes |
| `evidence_entailment` | Lexical coverage ≥85% or exact falsifier match | Yes |
| `source`/`counterexample`/`invariant` | Model-based (via `validated_call`) | No |

### 6.3 Provenance & Auditability
- Every run persists: evidence inventory, claim ledger, verification receipts, ballots, genealogy, taint state, rollout card, finality certificate, call budget events, identity map (private).
- `events.jsonl` is append-only with fsync.
- Rollout card aggregates all decision artifacts for external audit.

---

## 7. Git Worker Orchestration

### 7.1 Isolation
- Each candidate gets a detached worktree under `<run_store>/worktrees/<key>`.
- Workers invoke `codex exec --json` with an ephemeral `CODEX_HOME` (temp dir, 0700, deleted after).
- Environment is stripped to PATH/TMPDIR/LANG/TERM/SSL_CERT only.
- Network access disabled in codex sandbox config (`network_access = false`).

### 7.2 Concurrency
- Workers run concurrently via `asyncio.to_thread` + `asyncio.gather`.
- Each worker operates on its own worktree (no shared mutable state).

### 7.3 Merging & Cleanup
- Selected patch applied to a final worktree on a namespaced branch (`reason-assembly/<run_id>/final`).
- Integration (if action=integrate) invokes codex in the final worktree with other patches as file inputs.
- Final commit: exactly one commit on base, verified by `rev-list --count`.
- Cleanup: worktrees removed, branch preserved only on success; `worktree prune` always runs.
- `apply` cherry-picks without committing; user must inspect and commit.

### 7.4 Failure Handling
- Baseline test failure → candidate invalid.
- Test-only phase must produce only test files (regex check).
- New regression test must fail against base (red-green).
- `git diff --check` must pass.
- Credential scan on every patch (`reject_added_credentials`).
- Final engine-owned tests must pass.
- Completion review by cross-family validator(s) required.

---

## 8. Packaging & Installation

### 8.1 Package Structure
- setuptools with `package-dir = {"" = "scripts"}`, 15 `py-modules`.
- Console scripts: `reason-assembly` → `reason_assembly:main`, `ccycouncil` → `reason_assembly:legacy_main`.
- Data files: SKILL.md, agents/openai.yaml, compat skill, references.
- Dependencies: httpx, pydantic, pypdf, PyYAML (all exact-pinned).
- Dev: pytest, ruff.

### 8.2 Launcher
- `bin/reason-assembly`: resolves symlinks, execs `uv run --quiet --project ... python scripts/reason_assembly.py`.
- Requires `uv` on PATH for development use; wheel installation provides console scripts directly.

### 8.3 CI
- GitHub Actions: Python 3.11/3.12/3.13 matrix.
- Steps: ruff check, pytest, compileall, shell syntax check, wheel build + smoke test (version, data files, deprecated command).

---

## 9. Security Controls

| Control | Location | Assessment |
|---------|----------|------------|
| Secret redaction (exact + regex) | `artifacts.py:SecretGuard` | Strong; covers bearer, API keys, AWS, GitHub, private keys |
| Credential rejection in patches | `SecretGuard.reject_added_credentials` | Good; scans added lines only |
| SSRF protection | `verification.py:validate_source_url` + `_validate_connected_peer` | Thorough; DNS rebinding check |
| Sandboxed command execution | `verification.py:run_command_verifier` | Restricted env, no proxy vars |
| Atomic writes, 0600/0700 | Throughout | Consistent |
| Proxy config locking | `catalogue_sync.py:prune_smart_aliases` | flock + atomic replace + verification |
| Path traversal protection | `RunStore._target` | Resolves and checks parentage |
| Instruction injection quarantine | `v4.py:quarantine_source` | Regex-based; limited but present |
| Calculation sandbox | `verification.py:calculate` | AST whitelist, no builtins |

---

## 10. Findings (Ranked by Severity × Confidence)

### Critical

| # | Finding | Evidence | Confidence |
|---|---------|----------|------------|
| C1 | **Hard-coded model IDs will rot.** `PREFERENCES`, judge default `gpt-5.6-sol:medium`, luna `gpt-5.6-luna:low`, and doctor targets reference specific model IDs that exist only in one operator's proxy. Any proxy catalogue change breaks routing silently (falls through to preference score 0) or raises RuntimeError. | `routing.py:44-55`, `protocols.py:793-798` | Verified, High |
| C2 | **`shell=True` in verification commands.** User-supplied `--verify-command` strings are passed to `shell=True`. While documented as "user-authorized," this is a command injection vector if any upstream tooling passes untrusted input. | `verification.py:298-300` | Verified, High |
| C3 | **No concurrency protection on reliability/calibration stores.** `ReliabilityStore.write` and `PrivateJsonStore.write` use atomic replace but no locking. Concurrent `outcome` commands can lose updates (last-writer-wins). | `reliability.py:115-133`, `v4_state.py:54-60` | Verified, High |

### High

| # | Finding | Evidence | Confidence |
|---|---------|----------|------------|
| H1 | **Flat py-modules packaging pollutes namespace.** Installing this package puts `artifacts`, `contracts`, `routing`, `transport`, `v4`, etc. as top-level importable modules, risking conflicts with any other package using those names. | `pyproject.toml:40-56` | Verified, High |
| H2 | **`protocols.py` is a 1400+ line god module.** `CouncilEngine` handles routing, evidence, hypotheses, deliberation, verification, judging, validation, manifest persistence, and genealogy in one class. | `protocols.py` | Verified, High |
| H3 | **No retry/backoff on proxy 5xx or transient network errors.** A single 500 or timeout during any stage fails the entire run (budget consumed, partial artifacts). | `transport.py:499-504` | Verified, High |
| H4 | **`outcome_command` is ~250 lines of imperative attribution logic with no unit-testable interface.** It reads 10+ artifact files, mutates genealogy/verdict/certificate, and updates 4 stores in a non-transactional sequence. A crash mid-way leaves inconsistent state. | `reason_assembly.py:747-1102` | Verified, High |
| H5 | **`invoke_codex` depends on an external `codex` binary** that is not declared as a dependency, version-checked, or path-configurable. Failure mode is opaque (`FileNotFoundError` caught as generic RuntimeError). | `git_worker.py:515-528` | Verified, High |

### Medium

| # | Finding | Evidence | Confidence |
|---|---------|----------|------------|
| M1 | **`extract_json` fallback parsing is fragile.** It finds the first `{` or `[` and the last matching closer, which can extract invalid substrings from model output containing multiple JSON objects or prose with braces. | `protocols.py:152-171` | Verified, Medium |
| M2 | **Test coverage is integration-heavy with a single FakeTransport.** All protocol tests use one fake that returns canned responses. No property-based tests, no fuzzing of `extract_json`, no concurrency tests, no failure-injection for partial writes. | `tests/conftest.py` | Verified, Medium |
| M3 | **`stats_command` iterates all runs on every invocation** with no caching or pagination. At scale (hundreds of runs), this becomes O(n × artifacts_per_run) filesystem I/O. | `reason_assembly.py:1132-1316` | Verified, Medium |
| M4 | **`gather_with_quorum` grace period starts only after family quorum is met.** If one family is slow, the grace timer never starts and the call blocks until the global timeout (120s default). | `routing.py:377-418` | Verified, Medium |
| M5 | **No structured logging or observability.** All diagnostics go to stderr as plain text. No OpenTelemetry, no log levels, no machine-readable progress for long-running operations. | Throughout | Verified, Medium |
| M6 | **`run_command_verifier` uses `shell=True` with a hardcoded PATH** that assumes Homebrew on macOS (`/opt/homebrew/bin`). Linux CI or non-Homebrew installs may not find user tools. | `verification.py:289` | Verified, Medium |
| M7 | **Calibration requires 29-59 examples before any acceptance.** For a new deployment, every high-risk/implementation run will abstain indefinitely until sufficient outcome data is recorded manually. This is a UX trap. | `v4.py:443-447` | Verified, Medium |

### Low

| # | Finding | Evidence | Confidence |
|---|---------|----------|------------|
| L1 | **`_INSTRUCTION` regex for quarantine is trivially bypassable** (unicode, word splitting, base64). | `v4.py:498-501` | Verified, Low |
| L2 | **`shuffled_labels` uses SHA-256 of run_id as seed** — deterministic but not cryptographically unpredictable to an attacker who knows the run_id format. | `routing.py:421-425` | Verified, Low |
| L3 | **`CHANGELOG.md` dates are 2026-07-28/29** — suggests either a future-dated repo or synthetic history. | `CHANGELOG.md` | Verified, Informational |
| L4 | **No `py.typed` marker or type stubs** — downstream type checkers cannot validate against contracts. | Absence | Verified, Low |
| L5 | **`bin/reason-assembly` requires `uv` at runtime** for source installs, adding an implicit dependency not declared in pyproject.toml. | `bin/reason-assembly:16` | Verified, Low |

---

## 11. Dead Code & Drift

- `v4.py:choose_cold_start_operation` is defined but never called (the engine uses `deliberation.choose_operation` instead).
- `v4.py:higher_order_select` is defined but unreferenced in any execution path.
- `contracts.py:CandidateOutcome` and `ReceiptOutcome` are defined but never instantiated.
- `routing.py:CALL_CAPS = BUDGET_CAPS` is an unused alias.
- `identity.py:RELEASE_TAG` is defined but never referenced.
- `qa/qoder-qwen-3.8-max-preview-review.md` is an untracked review artifact (in `??` git status).

---

## 12. Product Positioning Critique

**Strengths:**
- Genuinely novel epistemic protocol (typed claims, taint, finality gates, abstention).
- Strong security posture for a CLI tool handling LLM outputs.
- Honest about uncertainty (cold-start caps, calibration requirements).
- Replay/revisit/regrade provide real auditability.

**Weaknesses:**
- Positioning as "built for empirical evaluation" is undermined by the cold-start trap (M7): new users can never get a `semantic_commit` without manually recording dozens of outcomes first.
- Hard dependency on a specific proxy (`CLIProxyAPI`) and specific model IDs makes this effectively single-operator software, not a general product.
- The skill/agent integration story (SKILL.md, agents/openai.yaml) targets a Codex-style ecosystem that may not match actual deployment.
- Documentation is thorough but assumes deep protocol knowledge; no quickstart produces a visible result without a running proxy.

---

## 13. Proposed Target Architecture

### 13.1 Component Decomposition

```
reason_assembly/              # proper package (not flat py-modules)
├── __init__.py               # version, public API
├── cli/                      # argument parsing, command handlers
│   ├── main.py
│   ├── decide.py
│   ├── implement.py
│   ├── inspect.py            # show, stats, outcome
│   └── admin.py              # sync, models, doctor, anchors
├── protocol/                 # deliberation engine (pure logic, no I/O)
│   ├── engine.py             # orchestration state machine
│   ├── operations.py         # choose_operation, aggregation
│   ├── judgment.py           # mirrored judging, selective judgment
│   ├── finality.py           # certificates, taint, genealogy
│   └── contracts.py          # Pydantic schemas
├── routing/                  # model selection (pluggable policy)
│   ├── policy.py             # scoring interface
│   ├── reliability.py        # Bayesian store
│   ├── catalogue.py          # sync, merge, epochs
│   └── preferences.py        # externalized, versioned config
├── evidence/                 # collection, verification, provenance
│   ├── inventory.py
│   ├── fetchers.py           # HTTPS, PDF, HTML
│   ├── verifiers.py          # command, calculation, entailment
│   └── genealogy.py
├── workers/                  # git orchestration
│   ├── worktree.py
│   ├── codex.py              # adapter interface (not hardcoded binary)
│   └── integration.py
├── storage/                  # all persistence
│   ├── run_store.py
│   ├── state_root.py
│   ├── migration.py
│   └── locking.py            # advisory locks for all mutable stores
├── transport/                # proxy HTTP client
│   ├── client.py
│   ├── budget.py
│   └── retry.py              # exponential backoff, circuit breaker
├── security/                 # cross-cutting
│   ├── secrets.py
│   ├── ssrf.py
│   └── sandbox.py
└── observability/            # structured logging, progress, metrics
    ├── logger.py
    └── progress.py
```

### 13.2 Key Design Decisions

| Concern | Current | Proposed |
|---------|---------|----------|
| Package structure | Flat py-modules | Proper package with subpackages |
| Model preferences | Hard-coded list | Versioned YAML/TOML config in state root, overridable |
| Judge/integrator | Hard-coded model IDs | Role-based selection with configurable pinning |
| Concurrency | No locking on shared stores | `fcntl.flock` on all mutable JSON stores |
| Transport retry | None | Exponential backoff (3 attempts), circuit breaker per model |
| Worker backend | Hard-coded `codex` binary | `WorkerBackend` protocol (codex, subprocess, mock) |
| Observability | stderr prints | Structured JSON logging + optional progress bar |
| Calibration bootstrap | Requires 29-59 manual outcomes | Provisional acceptance with elevated risk flag + automatic anchor-based calibration |
| God module | 1400-line CouncilEngine | Separated into protocol engine (pure), I/O adapters, persistence |

### 13.3 State & Migration Strategy
- Introduce `schema_version: 5` with a forward-only migration runner.
- v4→v5: restructure run artifacts into the new package layout; provide `reason-assembly migrate` command.
- Maintain read-only v4 discovery for one major version.

### 13.4 Protocol/Finality Invariants (preserved)
- Rubric locked before proposals.
- Mirrored judging with tiebreaker.
- Family diversity quorum ≥2.
- Selective judgment with binomial calibration.
- Taint propagation to verdict.
- `apply` requires clean semantic_commit + untainted lineage.

### 13.5 Routing Policy
- Externalize preferences to `~/.local/state/reason-assembly/routing-policy.toml`.
- Ship a default policy that uses role-capability matching only (no hard-coded model IDs).
- Allow operator pinning (`judge = "model:effort"`) with validation against live catalogue.
- Reliability scoring unchanged (Bayesian, time-decayed).

### 13.6 Security Boundaries (preserved + extended)
- All current SecretGuard, SSRF, sandbox controls retained.
- Add: signed run manifests (Ed25519) for tamper detection.
- Add: `--verify-command` parsed as argv (no shell=True) with explicit shell opt-in.

---

## 14. Phased Implementation Plan

### Phase 1: Existential Risk Reduction (1-2 weeks)

**Goal:** Eliminate data-loss and hard-failure risks without breaking existing behavior.

| Task | Acceptance Criteria | Gate |
|------|-------------------|------|
| Add `fcntl.flock` to ReliabilityStore, PrivateJsonStore, CoFailureStore writes | Concurrent `outcome` commands don't lose data; test with multiprocessing | Stop if lock contention causes >5s stalls |
| Add retry with backoff to `ProxyTransport.ask` for 5xx and timeouts | Single transient 500 doesn't fail a run; budget not consumed on retry | Stop if retry causes budget exhaustion |
| Externalize model preferences to config file with fallback to current hard-coded list | `reason-assembly decide` works unchanged; config override works | Go if tests pass |
| Declare `codex` dependency check at `implement` startup | Clear error message with install instructions if missing | Go |

**Tests:** Unit tests for locking, retry logic, config loading. Integration test with FakeTransport for retry.

### Phase 2: Structural Refactor (2-4 weeks)

**Goal:** Convert to proper package, decompose CouncilEngine.

| Task | Acceptance Criteria | Gate |
|------|-------------------|------|
| Create `reason_assembly/` package, move modules, update imports | All existing tests pass unchanged | Stop if any test breaks |
| Extract `protocol/engine.py` (pure state machine) from I/O | Engine testable without filesystem or network | Go when protocol tests pass with mock I/O |
| Extract `storage/` layer with unified locking | All persistence goes through one interface | Go |
| Replace flat py-modules with package in pyproject.toml | Wheel installs correctly, no namespace pollution | Go if CI green |

**Tests:** Existing suite + new unit tests for extracted components.

### Phase 3: Observability & UX (1-2 weeks)

| Task | Acceptance Criteria |
|------|-------------------|
| Structured JSON logging with levels | `--verbose` shows progress; `--json` emits machine-readable output |
| Progress reporting for long operations | User sees stage transitions during implement |
| Calibration bootstrap: provisional acceptance with `provisional=true` flag | New users get actionable results from day 1 |
| Quickstart documentation that works without a live proxy (mock mode) | `reason-assembly decide --demo` produces a sample run |

### Phase 4: Extensibility & Hardening (ongoing)

| Task | Acceptance Criteria |
|------|-------------------|
| `WorkerBackend` protocol replacing hard-coded codex | Alternative backends (e.g., aider, subprocess) pluggable |
| Property-based tests for `extract_json`, aggregation, taint | No crashes on adversarial model output |
| Signed run manifests | Tamper detection on stored artifacts |
| `--verify-command` as argv (no shell) with `--shell` opt-in | Eliminates C2 |

---

## 15. What Could Not Be Verified

- **Runtime behavior with a live proxy:** No proxy was available; all analysis is static.
- **`codex` binary behavior:** The external binary's actual JSON output format, failure modes, and sandbox enforcement could not be verified.
- **Concurrent production use:** No evidence of multi-user deployment; locking gaps are inferred from code, not observed failures.
- **Calibration data volume:** Whether any deployment has reached the 29-59 example threshold is unknown.
- **Model ID stability:** Whether the hard-coded model IDs (`gpt-5.6-sol`, etc.) are stable in the operator's proxy or change frequently.

---

## 16. Summary Verdict

Reason Assembly is a intellectually serious system with a well-designed epistemic protocol and strong security fundamentals. Its primary risks are operational (hard-coded model dependencies, no retry, no concurrency safety on shared state) and structural (god module, flat packaging, single-developer complexity). The proposed phased plan addresses data-loss risks first (Phase 1), then structural debt (Phase 2), then usability (Phase 3), preserving all useful behavior throughout.
