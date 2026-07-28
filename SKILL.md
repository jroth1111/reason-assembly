---
name: reason-assembly
description: Run explicit adaptive multi-model deliberation, verification, red-team, review, and competing implementation workflows through the local CLIProxyAPI catalogue. Use only when the user explicitly invokes $reason-assembly or asks to run reason-assembly; never invoke models implicitly.
disable-model-invocation: true
user-invocable: true
---

# Reason Assembly

Use the explicit `reason-assembly` command. Do not reproduce the protocol in the
parent session or expose private identities and raw opinions.

## Run

- Decision: `reason-assembly decide "question" --budget adaptive`
- Repository review: `reason-assembly review --repo PATH --working-tree`
- Adversarial analysis: `reason-assembly red-team "target"`
- Competing implementation: `reason-assembly implement --repo PATH --base REF --task-file FILE --test-command "COMMAND"`
- Availability: `reason-assembly models` or `reason-assembly doctor --all-models`
- Synchronization audit: `reason-assembly sync [--json]`
- Continue evidence: `reason-assembly replay RUN_ID` or `reason-assembly revisit RUN_ID --correction TEXT`
- Inspect and learn: `reason-assembly show RUN_ID`, `reason-assembly outcome RUN_ID STATUS`, or `reason-assembly stats`
- Calibration: `reason-assembly anchors import|list|validate|retire`
- Reporting-only child: `reason-assembly regrade RUN_ID --rules FILE`
- Apply: `reason-assembly apply RUN_ID`

Pass evidence with repeatable `--context FILE`, HTTPS sources with `--source URL`,
and user-authorized checks with `--verify-command COMMAND`. Route overrides use
`--route ROLE=MODEL[:EFFORT]`. Luna is utility-only.

`--budget quick|standard|max` provides 12/30/60 direct-call ceilings; adaptive is
the default and `--max-calls` is authoritative. `--judgment-risk` must be in
`(0, 0.25]`; high-risk and implementation acceptance is capped at `0.05`.

Every catalogue read validates `/v1/models` against capability metadata and
retries one transient mismatch. Raw `/v1/models` IDs are authoritative: matching
metadata is attached, unmatched raw IDs are listed-only, and metadata-only IDs
cannot enter the council. Proxy smart aliases remain a `ccyproxy` feature and are
never council model IDs.

Synchronization atomically removes alias candidates absent from the raw
catalogue under an exclusive lock. It preserves unrelated configuration text and
does not prune advertised models merely because they are unhealthy. Pruning and
metadata failures warn and continue with raw-ID filtering. Private,
credential-free schema-v4 receipts are written below
`~/.local/state/reason-assembly/v4/`.

The engine locks a rubric, compares typed approaches, selects uncertainty-reducing
operations, verifies load-bearing claims, uses higher-order task-specific
aggregation, and emits `semantic_commit`, `verdict_commit`, or `abort`. Ordinary
cold-start judgments are uncalibrated and capped at 0.65. High-risk and
implementation judgments abstain without deterministic or independent evidence.

## Return

Return the concise verdict, interval-grounded confidence, typed finality,
calibration state, judgment risk, co-failure state, abstention, dissent, blockers,
exclusions, call usage, and run ID. Only schema-v4 runs are accepted under
`~/.local/state/reason-assembly/runs/`.

Treat availability states distinctly: **catalogued** means present in raw
`/v1/models`, **eligible** means matching capabilities permit a council role, and
**healthy** means the current bounded doctor probe reached a terminal live
classification.

Read [protocol.md](references/protocol.md), [provenance.md](references/provenance.md),
and [research.md](references/research.md).
