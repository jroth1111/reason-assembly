# Reason Assembly protocol — schema v4

`reason-assembly` 0.6.0 accepts schema v4 only. It preserves explicit invocation,
model-ID-only routing, isolated Git workers, and adaptive 12/30/60 call ceilings.

## Routing and role separation

Every catalogue load synchronizes raw `/v1/models` IDs with capability metadata,
retries one transient ID mismatch, and keeps the raw set authoritative if drift
persists. Matching metadata determines role eligibility. Raw-only models remain
catalogued but listed-only; metadata-only IDs cannot be selected. Catalogue
membership, role eligibility, bounded live health, and family diversity are separate
states.

Canonical routed roles are `proposer`, `evidence_extractor`, `critic`,
`risk_analyst`, `minority_advocate`, `verifier`, `judge`, `validator`, `worker`,
`test_constructor`, `integrator`, and `utility`. Aggregation is a protocol operation,
not a routed role. Roles are eligibility-scoped and do not prove model independence.
High-risk and implementation acceptance require qualified-family diversity rather than
merely multiple IDs.

Eligible routes score as 45% role fit, 30% reliability, 20% independence, and 5%
health latency. Independence is currently fixed at `0.5`; `peer_models` is accepted but
unused, so learned pair independence does not affect routing. Family exclusion remains
active. A deterministic 20% of run IDs receives an exploration seat. Fixed judge/
validator, utility, and integrator reservations are excluded from general candidate
pools; explicit role overrides take precedence subject to catalogue, role, effort, and
family validation.

## Proposals, claims, and diversity

Before proposals, the engine locks a hashed rubric containing criteria, evidence
requirements, decision rule, risk class, and task format. Candidate proposals are
collected independently and presented anonymously. Proposal prose is separated from
typed, source-linked claims and verification receipts.

`ApproachSignature` records decomposition, operations, constraints, assumptions,
tools, evidence classes, intermediate commitments, and answer cluster. Deterministic
signed-hash views named `decomposition`, `operations_tools`, `evidence_assumptions`,
and `commitments` produce surface/approach distance, per-view mean distance and
effective rank, metric disagreement, estimated channels, qualified families,
independence deficit, collapse, novel minorities, and warnings. These are operational
heuristics, not measurements of latent reasoning or proof of causal independence.

## Task patterns and adaptive operations

Objective and review tasks privilege mechanical receipts. Evidence synthesis fuses
source-tagged compatible claims while preserving contradictions. Subjective tasks use
ranked pairs and retain dissent. Safety tasks require substantiated vetoes and two
qualified families. Implementations require isolated candidates, a clean contribution
graph, acceptance-level integration, and independent checks of the integrated result.

Cold-start operation precedence is: verify testable conflict, defend load-bearing
novelty, sample under an exclusion contract after collapse, actively compare close
candidates, then target rebuttal at non-testable ambiguity. The operation schema also
includes stop, direct judgment, higher-order aggregation, pairwise comparison, ranked
pairs, synthesis, safety validation, and blocked escalation. After 20 labeled operation
outcomes, guarded posterior resolution utility may replace the cold-start order.
Generic debate and raw-majority correctness are not protocol stages or claims.

## Judging and calibration

Judging begins with mirrored, position-balanced ballots and expands to at most six
evaluations. When external checks are unavailable, guarded route reliability,
empirical joint failure, evidence coherence, predicted peer agreement, and coherent
minority support may inform aggregation but cannot replace missing decisive evidence.

Reliability is keyed by model × family × role × task kind × domain and has a 90-day
half-life. Exact buckets activate at eight effective observations; family fallback
activates at 20; cold reliability is `0.5`. Active exact scores weight correctness,
error detection, order consistency, revision success, calibration, and competitor
robustness at 50/15/10/10/10/5 percent.

Co-failure routing is inactive below 30 ordinary or 60 high-risk observations.
Selective 10%/5% acceptance requires at least 29/59 accepted calibration examples and
a compliant exact error bound. Ordinary cold-start confidence is capped at 0.65.
High-risk and implementation acceptance abstains unless deterministic or independent
evidence resolves load-bearing criteria.

Implementation boundary: proposer–verifier `joint_failure` is not passed into normal
finality; `higher_order_select` and provisional-anchor acceptance helpers are not wired
into normal orchestration. Their presence in helper code or schemas does not imply that
the normal run flow uses them.

## Taint, finality, and lifecycle

Source quarantine treats retrieved instructions as data. Claim genealogy propagates
falsification, conflict, source invalidation, and integrity failures transitively into
dependent judgments and finality.

Verification is `supported`, `partially_supported`, `conflicting`, `falsified`,
`inconclusive`, or `not_checked`. Finality is `semantic_commit`, `verdict_commit`, or
`abort`; `apply` accepts only an implementation `semantic_commit`. Replay continues an
evidence process without rewriting its history. Revisit records correction, outcome
records realized evidence for guarded calibration, and regrade creates a deterministic
call-free reporting child. Schemas 1–3 are rejected without migration.

The protocol does not guarantee truth, safety, model independence, calibration under
distribution shift, or implementation correctness beyond the stated acceptance
contract. It introduces no local model weights, parameter merging, dense mixture,
learned router, or latent-state fusion.
