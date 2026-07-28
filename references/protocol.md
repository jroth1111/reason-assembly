# Reason Assembly protocol — schema v4

`reason-assembly` 0.5.0 accepts schema v4 only. It preserves API-only routing,
explicit invocation, isolated Git workers, and adaptive 12/30/60 call ceilings.

Every catalogue load performs schema-v4 synchronization. It fetches raw
`/v1/models` and capability metadata, retries one transient ID mismatch, and
uses the raw set as the model-ID authority if drift persists. Only matching
metadata is attached; raw-only models remain catalogued but listed-only, while
metadata-only IDs cannot be selected. Smart aliases are proxy-only and never
become council identities.

The same synchronization prunes candidates absent from the raw catalogue from
`smart-aliases` with an exclusive lock and atomic, permission-preserving textual
replacement. Advertised-but-unhealthy candidates remain. Pruning or metadata
failure produces a redacted warning and retains raw-ID runtime filtering.
Credential-free private receipts record set hashes, counts, removed IDs,
timestamp, and outcome. `sync --json` exposes the audit; `doctor --all-models
--json` includes sync and alias-resolution diagnostics.

Catalogue membership, role eligibility, and bounded live health are separate
states. Only membership controls pruning; eligibility controls council routing;
health reports the current terminal probe classification.

Before proposals, the engine locks a hashed rubric containing criteria, evidence
requirements, decision rule, and task format. Typed approach signatures feed
deterministic signed-hash views of surface and strategy distance, effective rank,
effective channels, minority coherence, and representational collapse.

Cold-start precedence is: verify testable conflict, defend load-bearing novelty,
sample with an exclusion contract after collapse, actively compare close
candidates, then target rebuttal at non-testable ambiguity. After 20 labeled
task-operation observations, posterior resolution utility may replace this order.
Generic debate and raw majority voting do not exist.

Objective and review tasks privilege mechanical receipts. Subjective tasks use
ranked pairs and preserve dissent. Safety tasks require substantiated vetoes and
two qualified families. Evidence synthesis fuses source-tagged claims.
Implementations require a clean contribution graph and acceptance-level
integration. When external checks are unavailable, guarded route reliability,
joint failure, evidence coherence, predicted peer consensus, and coherent
minority support replace raw voting.

Judging begins with mirrored ballots and expands to at most six evaluations.
Selective 10%/5% acceptance requires at least 29/59 accepted calibration
examples and a compliant exact error bound. Ordinary cold-start confidence is
capped at 0.65. High-risk and implementation acceptance abstains unless
deterministic or independent evidence resolves load-bearing criteria.

Finality is `semantic_commit`, `verdict_commit`, or `abort`; `apply` accepts only
an implementation `semantic_commit`. Verification is `supported`,
`partially_supported`, `conflicting`, `falsified`, `inconclusive`, or
`not_checked`. Runs preserve reproducible rollout and finality records. Schemas
1–3 are rejected without migration.
