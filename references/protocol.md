# Reason Assembly protocol — schema v4

`reason-assembly` 0.6.0 accepts schema v4 only. It preserves explicit invocation,
model-ID-only routing, isolated Git workers, and adaptive 12/30/60 call ceilings.

## Evidence status of composition guidance

Composition claims use three different bases:

- **enforced invariants** are explicit code gates covered by tests;
- **adaptive empirical policies** use activated, task-scoped outcome observations; and
- **recommended heuristics** follow the implemented failure-mode order but are not
  proven globally optimal.

The repository contains no comparative benchmark or ablation suite showing that one
provider, model order, route weighting, or council size is universally best. Unit and
property tests establish deterministic mechanics, not real-world accuracy improvement.

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
not a routed role. The fixed utility route currently supports task-contract and claim
normalization work; adaptive operation selection is deterministic policy plus activated
observational effects, not an autonomous planner.

Eligible routes score as 45% role fit, 30% reliability, 20% independence, and 5%
health latency. The weights and model-ID role-fit heuristics are policy constants, not
empirically fitted optima. Independence is currently fixed at `0.5`; `peer_models` is
accepted but unused, so learned pair independence does not affect routing. Family
exclusion remains active. A deterministic 20% of run IDs receives an exploration seat.
Fixed judge/validator, utility, and integrator reservations are excluded from general
candidate pools; explicit role overrides take precedence subject to catalogue, role,
effort, and family validation.

## Task-kind inference

Task kind controls composition and aggregation. Precedence is:

1. implementation mode → `implementation`;
2. review mode → `review`;
3. detected security, privacy, migration, reliability, production, financial, legal, or
   similarly consequential terms → `safety_gate`;
4. compare/recommend/trade-off/preference language → `subjective_tradeoff`;
5. synthesize/research/evidence language → `evidence_synthesis`;
6. otherwise → `objective_answer`.

`review` resolves exactly one Git target and then uses the common council pipeline with
`task_kind=review`; there is no separately wired review-finding/ranking engine.

## Normal council state machine

`decide` and `review` execute this order:

1. sanitize and persist the request snapshot;
2. snapshot sources, synchronize catalogue/metadata, preflight health, and reserve the
   judge and fixed utility route, plus the integrator for worker mode;
3. construct and lock the task contract, rubric, risk, and reporting rules;
4. reroute family-distinct proposers for the inferred task kind;
5. pack evidence, optionally extract structured source evidence, and collect independent
   anonymous hypotheses in parallel;
6. normalize claims when the fixed route is available, then build the claim ledger and
   approach profile;
7. execute user-authorized verification commands when supplied;
8. run bounded adaptive expansion;
9. persist the final ledger and optionally evaluate a route anchor;
10. obtain forward/reversed mirrored ballots from the judge and expand judging with an
    alternate family when close or inconsistent, up to the protocol limit;
11. apply task-specific aggregation and diagnose generation, verification, or
    aggregation failure;
12. for high-risk tasks, obtain two non-judge validator families and permit at most one
    judge revision/revalidation cycle;
13. apply co-failure/confidence penalties, selective judgment, blockers, abstention, and
    typed finality.

Budgets are total call ceilings: `quick=12`, `standard=30`, and `max=60`.
`--max-calls` overrides the normal ceiling. Ordinary adaptive-operation caps are
`quick=1`, `standard=2`, and `max=3`.

## Red-team state machine

Red-team uses a fixed adversarial composition rather than ordinary adaptive expansion:

1. collect independent initial positions;
2. collect attacks informed by those initial positions;
3. collect defenses and updated positions informed by both prior sets;
4. rebuild the ledger and approach profile;
5. continue through common mirrored judgment, aggregation, high-risk validation when
   applicable, selective judgment, and finality.

Its ordinary adaptive-operation cap is zero. This prevents the fixed attack/defense
sequence from silently accumulating the normal one-to-three additional operations.

## Adaptive operation semantics

Cold-start precedence is: verify a testable conflict; defend a load-bearing coherent
minority; sample an alternate route after approach collapse; use task-specific
ranked-pairs/synthesis/safety logic; use higher-order aggregation when route reliability
is low and conflicts remain; rebut remaining non-testable ambiguity; verify
missing load-bearing evidence; otherwise proceed to judgment.

After 20 labeled operation outcomes, an active task-scoped operation-effect score may
replace that order. It is an observational resolution-rate/cost policy, not proof of
causal effectiveness or cross-domain generalization.

| Decision | Runtime behavior |
| --- | --- |
| `verify` | build a verification plan and collect verifier receipts |
| `minority_defense` | defend one load-bearing minority claim and verify it when possible |
| `sample` | add one alternate-route proposal under an exclusion contract |
| `safety_validate` | add a risk-analyst hypothesis, then transition to conservative judgment |
| `targeted_rebuttal` | call up to two family-distinct critic routes |
| `synthesize` | call critic routes with synthesis instructions |
| `blocked_escalation` | stop expansion and preserve unresolved blockers |
| `stop`, `direct_judgment` | transition to common mirrored judgment |
| `higher_order_aggregate`, `pairwise_compare`, `ranked_pairs` | record aggregation intent and transition to common mirrored judgment; no distinct model stage is wired today |

Generic debate and raw-majority correctness are not protocol stages or claims.

## Aggregation and judgment composition

Candidate author identities are hidden from comparison and judgment. Judging begins
with the same judge seeing forward and reversed candidate orders. This detects position
bias before spending a call on another family. Close or inconsistent results may add an
alternate-family judge, with a maximum of six evaluations in the normal council path.

Aggregation is task-specific:

- objective and review: verifier-weighted;
- subjective trade-off: ranked pairs with dissent retained;
- evidence synthesis: claim fusion with contradiction preservation;
- safety gate: conservative veto and qualified-family requirements;
- implementation: criterion integration through a contribution graph.

A policy/aggregation label does not imply a separately executed model stage. External
receipts, evidence coverage, blockers, and finality rules dominate judge confidence.

## Implementation state machine

Implementation runs use a separate Git composition:

1. lock the repository, base commit, task, acceptance contract, verification mode, and
   authorized test commands;
2. route family-distinct workers and distribute acceptance criteria round-robin;
3. create one disposable worktree per worker; each route also fills the
   `test_constructor` identity for its own candidate;
4. execute candidates in parallel and retain only valid receipts;
5. enable cyclic anonymous peer review only when candidates emphasize complementary
   criteria, overlap changed files, or have weak verification;
6. ask the reserved judge for forward/reversed ballots;
7. if action/selection does not converge, ask one different-family tiebreak judge when
   the call reserve permits; otherwise reject;
8. choose `select`, `integrate`, or `reject`:
   - `select` uses the complete selected candidate and all its contributions;
   - `integrate` uses one complete base candidate plus selected verified contributions;
   - `reject` stops without an accepted final patch;
9. block selection/integration on contribution conflicts, missing dependencies, or
   incomplete acceptance-criterion coverage;
10. apply the base patch, optionally run the integrator, reject added credentials, run
    final authorized tests and `git diff --check`;
11. non-documentation semantic finality requires an explicit deterministic test command;
12. require one independent completion review for ordinary work or two family-distinct
    completion reviews for high-risk work, excluding the judge family;
13. create exactly one commit on the declared base and issue `semantic_commit`.

Peer review is conditional, not automatic. Integration is appropriate for complementary
verified contributions; direct selection is preferable when one complete candidate
already covers the contract because it avoids composition risk. Source-candidate tests
never substitute for final tests on the integrated result.

## Calibration and correlated failure

Reliability is keyed by model × family × role × task kind × domain and has a 90-day
half-life. Exact buckets activate at eight effective observations; family fallback
activates at 20; cold reliability is `0.5`. Active exact scores weight correctness,
error detection, order consistency, revision success, calibration, and competitor
robustness at 50/15/10/10/10/5 percent.

Calibration can change composition only through guarded paths:

- activated reliability affects the 30% routing component;
- deterministic exploration may seat a lower-ranked eligible route;
- activated operation effects may change the next adaptive operation;
- council co-failure activates after 30 ordinary or 60 high-risk observations and
  affects confidence, not route membership;
- selective 10%/5% acceptance requires at least 29/59 accepted examples and a compliant
  exact error bound; and
- route epochs/anchors limit reuse after catalogue or policy drift.

Ordinary cold-start confidence is capped at 0.65. High-risk and implementation
acceptance abstains unless deterministic or independent evidence resolves load-bearing
criteria.

Implementation boundaries: learned pair independence does not change routing;
proposer–verifier `joint_failure` is not passed into normal finality;
`higher_order_select` and provisional-anchor acceptance helpers are not wired into
normal orchestration. Their presence in helper code or schemas does not imply normal
run-flow use.

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
