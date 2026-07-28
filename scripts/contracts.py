from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    """Base for every persisted or model-facing schema-v4 contract."""

    model_config = ConfigDict(extra="forbid")


Role = Literal[
    "proposer",
    "evidence_extractor",
    "critic",
    "risk_analyst",
    "minority_advocate",
    "verifier",
    "judge",
    "validator",
    "worker",
    "test_constructor",
    "integrator",
    "utility",
]
TaskKind = Literal[
    "objective_answer",
    "evidence_synthesis",
    "subjective_tradeoff",
    "safety_gate",
    "review",
    "implementation",
]
ObservationStatus = Literal["confirmed", "disconfirmed", "mixed", "unknown"]


class ModelCapability(Contract):
    id: str
    family: str
    provider: str
    listed_only: bool = False
    context_window: int | None = Field(default=None, ge=1)
    efforts: list[str] = Field(default_factory=list)
    api_support: bool = False
    tool_support: bool = False
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    priority: int = Field(default=10_000, ge=0)
    visibility: str | None = None
    roles: list[Role] = Field(default_factory=list)
    eligible: bool = False
    exclusion_reasons: list[str] = Field(default_factory=list)


class AcceptanceCriterion(Contract):
    id: str
    text: str
    verification: Literal[
        "command",
        "source",
        "calculation",
        "invariant",
        "counterexample",
        "evidence_entailment",
    ] = "evidence_entailment"


class EvidenceRef(Contract):
    id: str
    source: str
    sha256: str
    kind: Literal[
        "task", "file", "git", "prior", "command", "note", "source", "pdf"
    ] = "file"
    size_bytes: int = Field(ge=0)
    priority: int = Field(default=50, ge=0, le=100)
    retrieved_at: datetime | None = None
    final_url: str | None = None
    content_type: str | None = None


class TaskContract(Contract):
    original_task_sha256: str = ""
    objective: str
    task_kind: TaskKind = "objective_answer"
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    required_roles: list[Role] = Field(default_factory=lambda: ["proposer", "judge"])
    risk_level: Literal["low", "medium", "high"] = "low"
    estimated_difficulty: float = Field(default=0.5, ge=0, le=1)
    domain_tags: list[str] = Field(default_factory=list)
    risk_categories: list[
        Literal[
            "security_privacy",
            "data_migration",
            "performance_reliability",
            "production_safety",
        ]
    ] = Field(default_factory=list)


class RoleRequirement(Contract):
    role: Role
    task_kind: TaskKind
    capabilities: list[str] = Field(default_factory=list)
    domain_tags: list[str] = Field(default_factory=list)
    count: int = Field(default=1, ge=1, le=4)


class RoleAssignment(Contract):
    label: str
    role: Role
    model: str
    family: str
    effort: str
    score: float = Field(ge=0, le=1)
    role_fit: float = Field(ge=0, le=1)
    reliability: float = Field(ge=0, le=1)
    independence: float = Field(ge=0, le=1)
    health_latency_score: float = Field(ge=0, le=1)
    exploratory: bool = False
    reasons: list[str] = Field(default_factory=list)


class Claim(Contract):
    id: str = ""
    text: str
    acceptance_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    reported_confidence: float = Field(default=0.5, ge=0, le=1)
    position: Literal["support", "oppose", "neutral"] = "support"
    blocker: bool = False
    load_bearing: bool = False
    testable: bool = False
    assumptions: list[str] = Field(default_factory=list)
    falsifiers: list[str] = Field(default_factory=list)


class ApproachSignature(Contract):
    decomposition: list[str]
    operations: list[str]
    constraints: list[str]
    assumptions: list[str]
    tools: list[str]
    evidence_classes: list[str]
    intermediate_commitments: list[str]
    answer_cluster: str


class Hypothesis(Contract):
    label: str = ""
    recommendation: str
    method: str = "independent"
    claims: list[Claim] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    predicted_observations: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    approach_signature: ApproachSignature | None = None


class ClaimStance(Contract):
    claim_id: str
    stance: Literal["support", "oppose", "uncertain"]
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)


class LedgerEntry(Contract):
    claim: Claim
    stances: list[ClaimStance] = Field(default_factory=list)
    supporting_labels: list[str] = Field(default_factory=list)
    opposing_labels: list[str] = Field(default_factory=list)
    unresolved: bool = False
    verification_status: Literal[
        "supported", "partially_supported", "conflicting", "falsified",
        "inconclusive", "not_checked",
    ] = "not_checked"


class ClaimLedger(Contract):
    entries: list[LedgerEntry] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    acceptance_coverage: dict[str, list[str]] = Field(default_factory=dict)
    load_bearing_unresolved: list[str] = Field(default_factory=list)


class VerificationStep(Contract):
    id: str
    claim_id: str
    kind: Literal[
        "command",
        "source",
        "calculation",
        "invariant",
        "counterexample",
        "evidence_entailment",
    ]
    instruction: str
    expected_observation: str = ""
    falsifying_observation: str = ""
    executor_input: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class VerificationPlan(Contract):
    steps: list[VerificationStep] = Field(default_factory=list)
    generated_by: Literal["engine", "model"] = "engine"


class VerificationReceipt(Contract):
    id: str = ""
    step_id: str
    claim_id: str
    kind: str
    status: Literal[
        "supported", "partially_supported", "conflicting", "falsified",
        "inconclusive", "not_checked",
    ]
    executor: str
    output_sha256: str
    observation: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    command_exit_code: int | None = None
    timed_out: bool = False
    expected_observation: str = ""
    falsifying_observation: str = ""
    resulting_stance: Literal["support", "oppose", "uncertain"] = "uncertain"
    verifier_route: str | None = None
    verifier_family: str | None = None
    proposer_route: str | None = None
    deterministic: bool = False
    independent: bool = False
    repeated: bool = False
    decomposed: bool = False
    ambiguity: str | None = None


class MinorityPosition(Contract):
    claim_id: str
    source_labels: list[str] = Field(default_factory=list)
    novelty_score: float = Field(ge=0, le=1)
    load_bearing: bool = False


class MinorityDefense(Contract):
    claim_id: str
    advocate_label: str
    strongest_case: str
    falsification_attempt: str
    status: Literal["survived", "falsified", "unresolved"]
    evidence_refs: list[str] = Field(default_factory=list)
    majority_disconfirmation_condition: str = ""
    verification_step_id: str | None = None


class MajoritySelfChallenge(Contract):
    claim_id: str
    majority_labels: list[str] = Field(default_factory=list)
    disconfirmation_condition: str
    evidence_refs: list[str] = Field(default_factory=list)


class CriterionScore(Contract):
    candidate_label: str
    criterion_id: str
    score: int = Field(ge=0, le=5)
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)


class JudgmentBallot(Contract):
    order: list[str]
    action: Literal["select", "integrate", "reject", "synthesize", "block"]
    selected_candidate: str | None = None
    accepted_claim_ids: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    scores: list[CriterionScore] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)
    reported_confidence: float = Field(default=0.5, ge=0, le=1)


class JudgmentAssessment(Contract):
    consistent: bool
    changed_winner: bool = False
    critical_score_delta: bool = False
    changed_load_bearing_claims: list[str] = Field(default_factory=list)
    close_pair: list[str] = Field(default_factory=list)
    tiebreaker_required: bool = False
    reasons: list[str] = Field(default_factory=list)
    preference_entropy: float = Field(default=1, ge=0, le=1)
    evaluations: int = Field(default=2, ge=2, le=6)
    bias_audit: BiasAudit | None = None


class DeliberationDecision(Contract):
    index: int = Field(ge=1)
    operation: Literal[
        "stop",
        "direct_judgment",
        "higher_order_aggregate",
        "pairwise_compare",
        "verify",
        "sample",
        "targeted_rebuttal",
        "minority_defense",
        "ranked_pairs",
        "synthesize",
        "safety_validate",
        "blocked_escalation",
    ]
    reasons: list[str] = Field(default_factory=list)
    learned_operation_utility: float = Field(default=0, ge=0, le=1)
    mandatory_calls_reserved: int = Field(default=0, ge=0)
    remaining_calls: int = Field(ge=0)
    aggregation: Literal[
        "verifier_weighted",
        "higher_order",
        "ranked_pairs",
        "criterion_integration",
        "conservative_veto",
        "claim_fusion",
        "mirrored_pairwise",
    ] = "verifier_weighted"
    policy_inputs: dict[str, float | bool | str | int] = Field(default_factory=dict)


class AggregationResult(Contract):
    method: Literal[
        "verifier_weighted",
        "higher_order",
        "ranked_pairs",
        "criterion_integration",
        "conservative_veto",
        "claim_fusion",
        "mirrored_pairwise",
    ]
    selected_candidate: str | None = None
    accepted_claim_ids: list[str] = Field(default_factory=list)
    ranking: list[str] = Field(default_factory=list)
    pareto_frontier: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class EvidenceExtraction(Contract):
    extractor_label: str = ""
    claims: list[Claim] = Field(default_factory=list)
    source_coverage: dict[str, list[str]] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)


class FailureDiagnosis(Contract):
    state: Literal[
        "none",
        "likely_generation_failure",
        "aggregation_discarded_supported_hypothesis",
        "selected_hypothesis_unverified",
        "indeterminate",
    ] = "indeterminate"
    generated_supported_claim_ids: list[str] = Field(default_factory=list)
    aggregation_discarded_claim_ids: list[str] = Field(default_factory=list)
    selected_unverified_claim_ids: list[str] = Field(default_factory=list)
    response: Literal[
        "none",
        "sample_new_hypothesis",
        "reaggregate_with_verified_claims",
        "verify_selected_claims",
        "block",
    ] = "none"
    reasons: list[str] = Field(default_factory=list)


class PeerReviewDecision(Contract):
    enabled: bool
    complementary_criteria: list[str] = Field(default_factory=list)
    potential_conflicting_files: list[str] = Field(default_factory=list)
    weakly_verified_candidates: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class Contribution(Contract):
    id: str
    candidate_label: str
    description: str
    acceptance_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    verification_receipt_ids: list[str] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    patch_sha256: str
    verified: bool = False


class ContributionGraph(Contract):
    contributions: list[Contribution] = Field(default_factory=list)
    selected_ids: list[str] = Field(default_factory=list)
    rejected_ids: list[str] = Field(default_factory=list)
    unresolved_conflicts: list[str] = Field(default_factory=list)
    acceptance_coverage: dict[str, list[str]] = Field(default_factory=dict)


class Verdict(Contract):
    decision: str
    rationale: list[str] = Field(default_factory=list)
    dissent: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    blockers: list[str] = Field(default_factory=list)
    action: Literal["select", "integrate", "reject", "synthesize", "block"] | None = (
        None
    )
    selected_candidate: str | None = None
    integration_plan: list[str] = Field(default_factory=list)
    selected_contribution_ids: list[str] = Field(default_factory=list)
    acceptance_reasons: dict[str, str] = Field(default_factory=dict)
    majority: list[str] = Field(default_factory=list)
    minority: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    failure_diagnosis: FailureDiagnosis | None = None
    finality: Literal["semantic_commit", "verdict_commit", "abort"] = "verdict_commit"
    calibrated: bool = False
    judgment_risk: float = Field(default=0.10, gt=0, le=0.25)
    cofailure: dict[str, Any] | None = None
    abstained: bool = False


class ValidationReceipt(Contract):
    label: str
    family: str
    status: Literal["blocker_free", "blocked", "insufficient"]
    verdict_sha256: str
    blockers: list[str] = Field(default_factory=list)
    checked_claim_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CommandReceipt(Contract):
    command: str
    exit_code: int | None = None
    output: str = ""
    phase: str = "worker"
    timed_out: bool = False


class WorkerReceipt(Contract):
    label: str
    model: str
    family: str
    base_commit: str
    exit_code: int
    design: str = ""
    changed_files: list[str] = Field(default_factory=list)
    acceptance_results: dict[str, str] = Field(default_factory=dict)
    commands: list[CommandReceipt] = Field(default_factory=list)
    tests: list[CommandReceipt] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    patch_sha256: str | None = None
    patch_artifact: str | None = None
    baseline_proven: bool = False
    final_proven: bool = False
    valid: bool = False
    failure_reason: str | None = None
    focus_acceptance_ids: list[str] = Field(default_factory=list)


class HealthResult(Contract):
    model: str
    family: str
    status: Literal[
        "healthy", "quota", "unavailable", "invalid", "timeout", "cancelled"
    ]
    latency_ms: int | None = Field(default=None, ge=0)
    detail: str | None = None


class OutcomeObservation(Contract):
    subject_type: Literal["run", "claim", "criterion", "component", "candidate", "receipt"]
    subject_id: str
    status: ObservationStatus
    evidence: list[str] = Field(default_factory=list)
    notes: str = ""
    weight: float = Field(default=0.25, ge=0, le=1)


class Outcome(Contract):
    run_id: str
    status: ObservationStatus
    notes: str = ""
    evidence: list[str] = Field(default_factory=list)
    observations: list[OutcomeObservation] = Field(default_factory=list)
    recorded_at: datetime


class ReliabilityBucket(Contract):
    model: str
    family: str
    role: Role
    task_kind: TaskKind
    domain: str = "general"
    alpha: float = Field(default=2, ge=0)
    beta: float = Field(default=2, ge=0)
    effective_observations: float = Field(default=0, ge=0)
    posterior_mean: float = Field(default=0.5, ge=0, le=1)
    conservative_lower_bound: float = Field(default=0, ge=0, le=1)
    active: bool = False
    true_positive: float = Field(default=0, ge=0)
    false_positive: float = Field(default=0, ge=0)
    true_negative: float = Field(default=0, ge=0)
    false_negative: float = Field(default=0, ge=0)
    error_detection_success: float = Field(default=0, ge=0)
    error_detection_total: float = Field(default=0, ge=0)
    order_consistent: float = Field(default=0, ge=0)
    order_observations: float = Field(default=0, ge=0)
    revision_success: float = Field(default=0, ge=0)
    revision_observations: float = Field(default=0, ge=0)
    calibration_absolute_error: float = Field(default=0, ge=0)
    calibration_observations: float = Field(default=0, ge=0)
    competitor_failures: dict[str, float] = Field(default_factory=dict)
    competitor_observations: dict[str, float] = Field(default_factory=dict)
    last_updated: datetime


class ConfidenceBucket(Contract):
    model: str
    family: str
    role: Role
    task_kind: TaskKind
    domain: str = "general"
    decile: int = Field(ge=0, le=9)
    alpha: float = Field(default=2, ge=0)
    beta: float = Field(default=2, ge=0)
    effective_observations: float = Field(default=0, ge=0)
    posterior_mean: float = Field(default=0.5, ge=0, le=1)
    conservative_lower_bound: float = Field(default=0, ge=0, le=1)
    active: bool = False
    last_updated: datetime


class ReliabilitySnapshot(Contract):
    policy_version: Literal["v4"] = "v4"
    generated_at: datetime
    buckets: list[ReliabilityBucket] = Field(default_factory=list)
    confidence_buckets: list[ConfidenceBucket] = Field(default_factory=list)


class RouteRecord(Contract):
    label: str
    model: str
    family: str
    effort: str
    role: Role


class ExclusionRecord(Contract):
    model: str
    family: str
    role: Role
    reason: str


class BudgetEvent(Contract):
    index: int = Field(ge=1)
    cap: int = Field(ge=1)
    stage: str
    model: str
    at: datetime


class RunManifest(Contract):
    schema_version: Literal[4] = 4
    policy_version: Literal["v4"] = "v4"
    run_id: str
    mode: Literal["decide", "review", "red-team", "implement", "replay", "revisit", "regrade"]
    budget: Literal["quick", "standard", "max"]
    budget_reasons: list[str] = Field(default_factory=list)
    task_kind: TaskKind | None = None
    created_at: datetime
    completed_at: datetime | None = None
    status: Literal["running", "completed", "blocked", "failed"] = "running"
    prompt_sha256: str
    task_contract_sha256: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    repo: str | None = None
    base_commit: str | None = None
    review_target: str | None = None
    routes: list[RouteRecord] = Field(default_factory=list)
    exclusions: list[ExclusionRecord] = Field(default_factory=list)
    call_cap: int
    calls_used: int = Field(default=0, ge=0)
    operations_used: int = Field(default=0, ge=0)
    stopped_reason: str | None = None
    reliability_snapshot_sha256: str | None = None
    parent_run_id: str | None = None
    ancestry_relation: Literal["replay", "revisit", "regrade"] | None = None
    route_substitutions: list[dict[str, str]] = Field(default_factory=list)
    final_branch: str | None = None
    final_commit: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    judgment_risk: float = Field(default=0.10, gt=0, le=0.25)
    rubric_sha256: str | None = None
    reporting_rules_sha256: str | None = None
    route_epoch: str | None = None
    finality: Literal["semantic_commit", "verdict_commit", "abort"] | None = None
    calibrated: bool = False
    abstained: bool = False


class RubricBundle(Contract):
    version: str
    criteria: list[AcceptanceCriterion]
    evidence_requirements: list[str] = Field(default_factory=list)
    decision_rule: str
    task_format: str
    sha256: str


class MultiViewDiversity(Contract):
    view: str
    signed_hash_vector: list[float]
    effective_rank: float = Field(ge=0)
    mean_distance: float = Field(ge=0, le=1)


class EffectiveChannelProfile(Contract):
    estimated_channels: float = Field(ge=0)
    qualified_families: list[str] = Field(default_factory=list)
    independence_deficit: bool = False


class ApproachProfile(Contract):
    surface_distance: float = Field(ge=0, le=1)
    approach_distance: float = Field(ge=0, le=1)
    views: list[MultiViewDiversity] = Field(default_factory=list)
    metric_disagreement: float = Field(ge=0, le=1)
    effective_channels: EffectiveChannelProfile
    representational_collapse: bool
    novel_minority_approaches: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CoFailureProfile(Contract):
    bucket_key: str
    observations: float = Field(ge=0)
    all_wrong_probability: float | None = Field(default=None, ge=0, le=1)
    interval_low: float | None = Field(default=None, ge=0, le=1)
    interval_high: float | None = Field(default=None, ge=0, le=1)
    strongest_single_accuracy: float | None = Field(default=None, ge=0, le=1)
    oracle_accuracy: float | None = Field(default=None, ge=0, le=1)
    realized_accuracy: float | None = Field(default=None, ge=0, le=1)
    orchestration_headroom: float | None = None
    selection_loss: float | None = None
    verification_loss: float | None = None
    active: bool = False
    uncertain: bool = True


class UncertaintyProfile(Contract):
    epistemic: float = Field(ge=0, le=1)
    within_route_instability: float = Field(ge=0, le=1)
    judge_variance: float = Field(ge=0, le=1)
    representation_disagreement: float = Field(ge=0, le=1)
    context_loss: float = Field(ge=0, le=1)
    aleatoric_cost: float = Field(ge=0, le=1)


class OperationEffectProfile(Contract):
    operation: str
    task_kind: TaskKind
    observations: float = Field(ge=0)
    resolution_probability: float = Field(ge=0, le=1)
    utility: float
    active: bool = False


class ExclusionContract(Contract):
    different_decomposition: str | None = None
    different_evidence_class: str | None = None
    different_tool: str | None = None
    different_failure_assumption: str | None = None


class ActiveComparison(Contract):
    left: str
    right: str
    expected_uncertainty_reduction: float = Field(ge=0)
    evaluations: int = Field(default=2, ge=2, le=6)


class BiasAudit(Contract):
    position: bool = False
    verbosity: bool = False
    bandwagon: bool = False
    self_preference: bool = False
    evidence_omission: bool = False
    unsupported_confidence: bool = False
    findings: list[str] = Field(default_factory=list)


class SelectiveJudgmentReceipt(Contract):
    accepted: bool
    abstained: bool
    calibrated: bool
    judgment_risk: float = Field(gt=0, le=0.25)
    threshold: float | None = Field(default=None, ge=0, le=1)
    confidence_low: float = Field(ge=0, le=1)
    confidence_high: float = Field(ge=0, le=1)
    calibration_examples: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)


class FinalityCertificate(Contract):
    finality: Literal["semantic_commit", "verdict_commit", "abort"]
    task_kind: TaskKind
    accepted: bool
    calibrated: bool
    judgment_risk: float = Field(gt=0, le=0.25)
    deterministic_receipt_ids: list[str] = Field(default_factory=list)
    independent_receipt_ids: list[str] = Field(default_factory=list)
    qualified_families: list[str] = Field(default_factory=list)
    unresolved_claim_ids: list[str] = Field(default_factory=list)
    rubric_sha256: str
    reporting_rules_sha256: str
    reproducible: bool = True


VerificationStatus = Literal[
    "supported", "partially_supported", "conflicting", "falsified",
    "inconclusive", "not_checked",
]


class GenealogyNode(Contract):
    id: str
    kind: Literal["source", "extraction", "claim", "derivation", "verification", "contribution", "verdict"]
    tainted: bool = False
    quarantine_reason: str | None = None


class GenealogyEdge(Contract):
    source: str
    target: str
    relation: str


class ClaimGenealogy(Contract):
    nodes: list[GenealogyNode] = Field(default_factory=list)
    edges: list[GenealogyEdge] = Field(default_factory=list)


class TaintState(Contract):
    tainted_ids: list[str] = Field(default_factory=list)
    transitions: list[dict[str, str]] = Field(default_factory=list)


class CalibrationAnchor(Contract):
    id: str
    task: str
    expected: str
    task_kind: TaskKind
    domain: str = "general"
    answer_format: str = "text"
    active: bool = True
    sha256: str


class RouteEpoch(Contract):
    id: str
    catalogue_fingerprint: str
    created_at: datetime
    anchor_results: dict[str, bool] = Field(default_factory=dict)
    validated: bool = False


class ReportingRules(Contract):
    version: str
    confidence_display: Literal["interval", "point_and_interval"] = "interval"
    include_dissent: bool = True
    include_taint: bool = True


class DropManifest(Contract):
    stage: str
    route: str
    reason: str
    counted_call: bool


class RolloutCard(Contract):
    schema_version: Literal[4] = 4
    run_id: str
    rubric_sha256: str
    reporting_rules_sha256: str
    call_manifest: list[dict[str, Any]] = Field(default_factory=list)
    drops: list[DropManifest] = Field(default_factory=list)
    route_versions: dict[str, str] = Field(default_factory=dict)
    context_packing: list[dict[str, Any]] = Field(default_factory=list)
    sanitized_prompt_sha256s: list[str] = Field(default_factory=list)
    ballots: list[dict[str, Any]] = Field(default_factory=list)
    verification_traces: list[VerificationReceipt] = Field(default_factory=list)
    genealogy: ClaimGenealogy = Field(default_factory=ClaimGenealogy)
    taint_transitions: list[dict[str, str]] = Field(default_factory=list)
    policy_decisions: list[dict[str, Any]] = Field(default_factory=list)
    operation_utilities: list[OperationEffectProfile] = Field(default_factory=list)
    cofailure: list[CoFailureProfile] = Field(default_factory=list)
    finality: FinalityCertificate | None = None
    patches: list[dict[str, str]] = Field(default_factory=list)
    receipts: list[dict[str, Any]] = Field(default_factory=list)
    views: list[dict[str, Any]] = Field(default_factory=list)


class CandidateOutcome(Contract):
    label: str
    status: ObservationStatus


class ReceiptOutcome(Contract):
    id: str
    status: ObservationStatus


class SanitizedSnapshot(Contract):
    mode: Literal["decide", "review", "red-team", "implement", "revisit"]
    budget_requested: Literal["adaptive", "quick", "standard", "max"]
    prompt: str
    contexts: list[dict[str, str]] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)
    repo: str | None = None
    base_commit: str | None = None
    review_target: str | None = None
    verification_mode: Literal["auto", "regression", "invariant", "docs"] | None = None
    test_commands: list[str] = Field(default_factory=list)


class CandidateSummary(Contract):
    label: str
    design: str
    changed_files: list[str] = Field(default_factory=list)
    acceptance_results: dict[str, str] = Field(default_factory=dict)
    tests: list[CommandReceipt] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    patch_sha256: str
    patch_excerpt: str
    contributions: list[Contribution] = Field(default_factory=list)
    focus_acceptance_ids: list[str] = Field(default_factory=list)


class ClaimAlias(Contract):
    source_claim_id: str
    canonical_text: str


class ClaimNormalization(Contract):
    aliases: list[ClaimAlias] = Field(default_factory=list)


class RevisitClaim(Contract):
    claim_id: str
    status: Literal["changed", "stayed"]
    reason: str


class RevisitReport(Contract):
    parent_run_id: str
    claims: list[RevisitClaim] = Field(default_factory=list)


def dump_schema_bundle() -> dict[str, dict[str, Any]]:
    """Expose every public schema-v4 contract."""

    models = [
        ModelCapability,
        TaskContract,
        AcceptanceCriterion,
        EvidenceRef,
        RoleRequirement,
        RoleAssignment,
        Claim,
        Hypothesis,
        ClaimStance,
        ClaimLedger,
        VerificationPlan,
        VerificationReceipt,
        MinorityDefense,
        MajoritySelfChallenge,
        CriterionScore,
        JudgmentBallot,
        JudgmentAssessment,
        DeliberationDecision,
        AggregationResult,
        EvidenceExtraction,
        FailureDiagnosis,
        PeerReviewDecision,
        ContributionGraph,
        Verdict,
        ValidationReceipt,
        WorkerReceipt,
        HealthResult,
        Outcome,
        ReliabilityBucket,
        ConfidenceBucket,
        ReliabilitySnapshot,
        RunManifest,
        RubricBundle,
        ApproachSignature,
        MultiViewDiversity,
        EffectiveChannelProfile,
        ApproachProfile,
        CoFailureProfile,
        UncertaintyProfile,
        OperationEffectProfile,
        ExclusionContract,
        ActiveComparison,
        BiasAudit,
        SelectiveJudgmentReceipt,
        FinalityCertificate,
        GenealogyNode,
        GenealogyEdge,
        ClaimGenealogy,
        TaintState,
        CalibrationAnchor,
        RouteEpoch,
        ReportingRules,
        DropManifest,
        RolloutCard,
        CandidateOutcome,
        ReceiptOutcome,
    ]
    return {model.__name__: model.model_json_schema() for model in models}
