# Changelog

All notable changes to this project are documented here.

## Unreleased

- Add a practical multi-model composition cookbook covering effective task-specific
  lineups, exact stage ordering, budget/risk recipes, implementation selection versus
  integration, calibration-driven adaptation, and composition anti-patterns.
- Distinguish routed roles, separately executed model stages, and policy/aggregation
  labels that transition directly to common judgment.
- Clarify that routing weights, role-fit heuristics, and observed operation effects are
  protocol policies rather than benchmark-proven global optima, and that code review
  currently uses the common council pipeline.

## 0.6.0 - 2026-07-29

- Remove the retired compatibility command, skill identity, state import path, and
  environment-variable aliases. The release exposes one canonical product and runtime
  identity.
- Rewrite the public documentation around explicit model roles and a task-pattern
  matrix for objective decisions, evidence synthesis, subjective comparison, safety
  analysis, code review, adversarial analysis, and competing implementation.
- Document family-aware routing and distinguish catalogue membership, role eligibility,
  bounded live health, and approach-family diversity.
- Document anonymous proposal collection, proposal/evidence separation, typed claim
  genealogy, transitive taint, and the limits of identity masking.
- Document strategy-level diversity measurements, effective-rank collapse detection,
  coherent-minority handling, and adaptive deliberation under fixed call ceilings.
- Document position-balanced judging, task-specific aggregation, contribution-aware
  implementation competition, and independently checked integration.
- Document guarded reliability, empirical co-failure, operation-utility, and selective-
  judgment calibration, including cold-start precedence and abstention requirements.
- Clarify typed finality, lifecycle and integrity behavior, and explicit limitations:
  the tool does not guarantee independence, truth, safety, correctness, or fitness for
  use and does not perform local model-weight or latent-state ensembling.
- Add release gates that reject retired identity strings in tracked release surfaces.

## 0.5.1 - 2026-07-29

- Update the exact `pypdf` production dependency pin from 6.6.0 to 6.14.2,
  resolving 58 Dependabot alerts, including four high-severity denial-of-service
  advisories for malformed PDF inputs.
- Update the exact `pytest` development dependency pin from 9.0.2 to 9.0.3,
  resolving the temporary-directory handling advisory.
- Preserve the schema-v4 protocol and 0.5.x runtime behavior.

## 0.5.0 - 2026-07-28

- Establish the canonical package, command, skill, and private state layout.
- Add guarded, non-destructive schema-v4 state publication with atomic run-ID
  reservation and create-if-absent shared-state writes.
- Synchronize the raw proxy catalogue and capability metadata while retaining raw model
  IDs as the routing authority and recording credential-free private receipts.
- Build canonical wheels and console scripts through setuptools with locked CI coverage
  on Python 3.11 through 3.13.

## 0.4.1 - 2026-07-28

- Validate raw and capability-metadata catalogues on every catalogue read, with one
  retry for transient mismatches.
- Keep raw `/v1/models` IDs authoritative when a mismatch persists and mark unmatched
  raw models as listed-only.
- Atomically prune absent smart-alias candidates under an exclusive lock while
  preserving unrelated proxy configuration.
- Add catalogue synchronization, private schema-v4 receipts, and synchronization
  diagnostics to the all-model diagnostics workflow.
- Refresh the proxy catalogue before launcher route resolution while retaining raw-ID
  runtime filtering.
- Publish project documentation, safety guidance, a public integration example, and
  continuous integration.
