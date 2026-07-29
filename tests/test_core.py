from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from artifacts import EvidenceInventory, RunStore, SecretGuard
from conftest import CATALOGUE, FakeTransport
from contracts import (
    AcceptanceCriterion,
    ApproachProfile,
    ApproachSignature,
    Claim,
    ClaimLedger,
    EffectiveChannelProfile,
    FailureDiagnosis,
    Hypothesis,
    LedgerEntry,
    JudgmentBallot,
    Outcome,
    OutcomeObservation,
    ReliabilitySnapshot,
    RunManifest,
    TaskContract,
    Contract,
    VerificationPlan,
    VerificationStep,
    dump_schema_bundle,
)
from deliberation import (
    BUDGET_CAPS,
    aggregate_ballots,
    choose_operation,
    diagnose_failure_mode,
    judgment_assessment,
    ranked_pairs,
)
from protocols import (
    CouncilEngine,
    CouncilRequest,
    clean_blockers,
    deterministic_task_contract,
    run_council,
)
from reliability import ReliabilityStore, exploratory_run
from routing import (
    budget_for,
    build_claim_ledger,
    candidate_pool,
    gather_with_quorum,
    load_routing_policy,
    normalize_hypothesis,
    parse_route_override,
    score_routes,
    stable_claim_id,
)
from transport import CallBudget, CallBudgetExceeded
from v4 import approach_profile, selective_judgment


def make_profile(
    *,
    score: float = 0.5,
    low_diversity: bool = False,
    **_: object,
) -> ApproachProfile:
    return ApproachProfile(
        surface_distance=score,
        approach_distance=score,
        views=[],
        metric_disagreement=0,
        effective_channels=EffectiveChannelProfile(estimated_channels=2),
        representational_collapse=low_diversity,
        warnings=[],
    )


def test_all_schema_v4_contracts_are_strict():
    pending = list(Contract.__subclasses__())
    seen = set()
    while pending:
        model = pending.pop()
        seen.add(model.__name__)
        assert model.model_config["extra"] == "forbid"
        pending.extend(model.__subclasses__())
    assert len(seen) >= 35
    assert {
        "ApproachProfile",
        "VerificationPlan",
        "JudgmentBallot",
        "ContributionGraph",
        "ReliabilitySnapshot",
        "RunManifest",
    } <= set(dump_schema_bundle())
    manifest = RunManifest(
        run_id="r",
        mode="decide",
        budget="quick",
        created_at=datetime.now(timezone.utc),
        prompt_sha256="x",
        call_cap=12,
    )
    assert manifest.schema_version == 4
    with pytest.raises(ValidationError):
        RunManifest.model_validate(
            {**manifest.model_dump(mode="json"), "unexpected": True}
        )


def test_budget_caps_and_greenfield_cli_semantics():
    assert BUDGET_CAPS == {"quick": 12, "standard": 30, "max": 60}
    assert budget_for("adaptive", "small question")[0] == "quick"
    assert budget_for("adaptive", "production credential migration")[0] == "max"
    assert parse_route_override("judge=gpt-5.6-sol:medium") == (
        "judge",
        "gpt-5.6-sol",
        "medium",
    )


def test_routing_policy_loads_toml_and_env_without_catalogue_sync(tmp_path):
    policy_path = tmp_path / "routing.toml"
    policy_path.write_text(
        'preferences = ["model-b", "model-a"]\n'
        '[roles]\njudge = "judge-local:high"\nluna = "utility-local:low"\n'
    )
    policy = load_routing_policy(
        tmp_path,
        {
            "REASON_ASSEMBLY_ROUTING_POLICY": str(policy_path),
            "REASON_ASSEMBLY_INTEGRATOR_MODEL": "integrator-env:medium",
        },
    )
    assert policy.preferences == ("model-b", "model-a")
    assert policy.judge_model == "judge-local:high"
    assert policy.luna_model == "utility-local:low"
    assert policy.integrator_model == "integrator-env:medium"


def test_candidate_pool_is_family_diverse_and_luna_excluded(tmp_path):
    selected = candidate_pool(CATALOGUE, [], budget="max", role="proposer")
    assert len({route.family for route in selected}) == len(selected)
    assert all(route.model not in {"gpt-5.6-sol", "gpt-5.6-luna"} for route in selected)
    explicit = candidate_pool(
        CATALOGUE,
        [
            "proposer=gemini-3.1-pro-low:low",
            "proposer=qwen3.8-max-preview:high",
        ],
        budget="quick",
        role="proposer",
    )
    scored = score_routes(
        explicit,
        role="proposer",
        task_kind="objective_answer",
        snapshot=ReliabilitySnapshot(generated_at=datetime.now(timezone.utc)),
        reliability_store=ReliabilityStore(tmp_path / "reliability"),
    )
    assert {route.effort for route, _ in scored} == {"low", "high"}


def test_budget_counts_and_caps_all_direct_calls():
    budget = CallBudget(2)
    budget.consume("health", "a")
    budget.consume("judging", "b")
    with pytest.raises(CallBudgetExceeded):
        budget.consume("verification", "c")


def test_evidence_inventory_hash_pack_redaction_and_permissions(tmp_path):
    guard = SecretGuard(["test-secret"])
    store = RunStore(tmp_path, "run", guard)
    inventory = EvidenceInventory()
    first = inventory.add("task", "must preserve constraint", kind="task", priority=100)
    second = inventory.add("context", "other evidence", priority=50)
    packed, included = inventory.packed(1000)
    assert included == [first.id, second.id]
    assert set(inventory.coverage(packed)) == {first.id, second.id}
    store.write_text("secret.txt", "Bearer test-secret")
    assert "test-secret" not in (store.path / "secret.txt").read_text()
    assert oct((store.path / "secret.txt").stat().st_mode & 0o777) == "0o600"


def test_run_store_transaction_writes_manifest_last_and_guards_outcome(tmp_path, monkeypatch):
    store = RunStore(tmp_path, "transaction", SecretGuard())
    order = []
    original = store.write_json

    def tracked(relative, value):
        order.append(relative)
        return original(relative, value)

    monkeypatch.setattr(store, "write_json", tracked)
    store.write_transaction(
        [("manifest.json", {"schema_version": 4}), ("outcome.json", {"status": "confirmed"})],
        require_absent="outcome.json",
    )
    assert order == ["outcome.json", "manifest.json"]
    assert ".run.lock" not in store.artifact_names()
    with pytest.raises(RuntimeError, match="already recorded"):
        store.write_transaction(
            [("outcome.json", {"status": "mixed"})],
            require_absent="outcome.json",
        )


def _hypothesis(label: str, recommendation: str, claim_text: str, evidence: str):
    return normalize_hypothesis(
        Hypothesis(
            recommendation=recommendation,
            method=label,
            claims=[
                Claim(
                    text=claim_text,
                    evidence_refs=[evidence],
                    reported_confidence=0.8,
                    load_bearing=True,
                )
            ],
        ),
        label,
    )


def test_diversity_profile_detects_collapse_and_novel_minority():
    same = ApproachSignature(
        decomposition=["same"],
        operations=["inspect"],
        constraints=[],
        assumptions=[],
        tools=[],
        evidence_classes=[],
        intermediate_commitments=[],
        answer_cluster="same",
    )
    low = approach_profile({"A": same, "B": same})
    assert low.representational_collapse
    distinct = ApproachSignature(
        decomposition=["different"],
        operations=["calculate"],
        constraints=[],
        assumptions=[],
        tools=[],
        evidence_classes=["primary"],
        intermediate_commitments=[],
        answer_cluster="reject",
    )
    high = approach_profile({"A": same, "B": same, "C": distinct})
    assert high.approach_distance > low.approach_distance
    assert "C" in high.novel_minority_approaches


def test_policy_prefers_verification_then_minority_then_novelty():
    contract = TaskContract(objective="x")
    conflict = Claim(id="C-1", text="test", testable=True, load_bearing=True)
    ledger = ClaimLedger(
        entries=[],
        conflicts=["C-1"],
    )
    from contracts import LedgerEntry

    ledger.entries = [LedgerEntry(claim=conflict, unresolved=True)]
    profile = make_profile(score=0.6)
    decision = choose_operation(
        index=1,
        contract=contract,
        profile=profile,
        ledger=ledger,
        calibrated_confidence=0.5,
        remaining_calls=10,
        mandatory_calls=2,
    )
    assert decision.operation == "verify"


def test_policy_uses_explicit_blocked_escalation_when_only_mandatory_calls_remain():
    decision = choose_operation(
        index=2,
        contract=TaskContract(objective="x"),
        profile=make_profile(score=0.4),
        ledger=ClaimLedger(
            entries=[
                LedgerEntry(
                    claim=Claim(
                        id="C-1",
                        text="unsupported",
                        load_bearing=True,
                    ),
                    unresolved=True,
                )
            ],
            missing_evidence=["C-1"],
            load_bearing_unresolved=["C-1"],
        ),
        calibrated_confidence=0.5,
        remaining_calls=4,
        mandatory_calls=2,
    )
    assert decision.operation == "blocked_escalation"
    assert decision.learned_operation_utility == 0


def test_policy_does_not_redefend_a_resolved_minority_claim():
    claim = Claim(
        id="C-1",
        text="resolved minority",
        evidence_refs=["E-1"],
        load_bearing=True,
    )
    decision = choose_operation(
        index=2,
        contract=TaskContract(objective="x"),
        profile=make_profile(score=0.5).model_copy(
            update={"novel_minority_approaches": ["C-1"]}
        ),
        ledger=ClaimLedger(
            entries=[
                LedgerEntry(
                    claim=claim,
                    unresolved=False,
                    verification_status="supported",
                )
            ],
        ),
        calibrated_confidence=0.7,
        remaining_calls=10,
        mandatory_calls=2,
        evidence_completeness=1,
    )
    assert decision.operation == "direct_judgment"


def test_failure_diagnosis_separates_generation_aggregation_and_verification():
    claim = Claim(id="C-1", text="load bearing", load_bearing=True)
    ledger = ClaimLedger(
        entries=[
            LedgerEntry(
                claim=claim,
                supporting_labels=["A"],
            )
        ],
        acceptance_coverage={"AC-1": ["C-1"]},
    )
    aggregation = diagnose_failure_mode(
        ledger=ledger,
        selected_candidate="A",
        accepted_claim_ids=set(),
        verified_claim_ids={"C-1"},
        structurally_supported_claim_ids=set(),
    )
    assert aggregation.state == "aggregation_discarded_supported_hypothesis"
    verification = diagnose_failure_mode(
        ledger=ledger,
        selected_candidate="A",
        accepted_claim_ids={"C-1"},
        verified_claim_ids=set(),
        structurally_supported_claim_ids=set(),
    )
    assert verification.state == "selected_hypothesis_unverified"
    generation = diagnose_failure_mode(
        ledger=ledger.model_copy(update={"acceptance_coverage": {"AC-1": []}}),
        selected_candidate=None,
        accepted_claim_ids=set(),
        verified_claim_ids=set(),
        structurally_supported_claim_ids=set(),
    )
    assert generation.state == "likely_generation_failure"
    assert isinstance(generation, FailureDiagnosis)


def test_claim_ledger_has_no_model_controlled_stop_flag():
    a = _hypothesis("A", "Proceed", "Use evidence", "E-1")
    b = _hypothesis("B", "Proceed", "Use evidence", "E-1")
    contract = TaskContract(
        objective="Use evidence",
        acceptance_criteria=[AcceptanceCriterion(id="AC-001", text="Use evidence")],
    )
    ledger = build_claim_ledger([a, b], [], contract)
    assert not hasattr(ledger, "safe_to_stop")
    assert ledger.acceptance_coverage["AC-001"]


def test_claim_ledger_covers_explicit_evidence_id_citation_criteria():
    hypothesis = _hypothesis(
        "A",
        "Proceed",
        "GET is safe",
        "E-21c1cdce6ab0",
    )
    ledger = build_claim_ledger(
        [hypothesis],
        [],
        TaskContract(
            objective="Cite the source",
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="AC-001",
                    text="Cite supplied evidence ID E-21c1cdce6ab0.",
                )
            ],
        ),
    )
    assert ledger.acceptance_coverage["AC-001"] == [stable_claim_id("GET is safe")]
    qualitative = hypothesis.model_copy(deep=True)
    qualitative.claims[0].acceptance_ids = ["AC-QUAL"]
    ledger = build_claim_ledger(
        [qualitative],
        [],
        TaskContract(
            objective="Preserve qualifications",
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="AC-QUAL",
                    text="Preserve the material minority qualification.",
                )
            ],
        ),
    )
    assert ledger.acceptance_coverage["AC-QUAL"]


def test_mirrored_judge_detects_position_instability():
    first = JudgmentBallot(
        order=["A", "B"],
        action="select",
        selected_candidate="A",
    )
    second = JudgmentBallot(
        order=["B", "A"],
        action="select",
        selected_candidate="B",
    )
    result = judgment_assessment(first, second, set())
    assert not result.consistent
    assert result.tiebreaker_required


def test_confidence_ignores_uncalibrated_overconfidence_and_caps():
    value = selective_judgment(
        scores_and_correct=[],
        score=0.99,
        judgment_risk=0.1,
        high_risk=False,
        implementation=False,
        deterministic=False,
        independently_verified=False,
    )
    assert not value.calibrated
    assert value.confidence_high == 0.65
    assert clean_blockers(["None identified.", " no blockers ", "real risk"]) == [
        "real risk"
    ]
    assert clean_blockers(
        [
            "No blockers identified - the source is clear.",
            "No blockers except missing evidence",
        ]
    ) == ["No blockers except missing evidence"]


def test_diversity_profile_measures_operational_error_diversity():
    profile = approach_profile(
        {
            "A": ApproachSignature(
                decomposition=["cache"],
                operations=["lock analysis"],
                constraints=[],
                assumptions=["same lock"],
                tools=[],
                evidence_classes=["source"],
                intermediate_commitments=[],
                answer_cluster="one",
            ),
            "B": ApproachSignature(
                decomposition=["database"],
                operations=["transaction proof"],
                constraints=[],
                assumptions=["serializable"],
                tools=[],
                evidence_classes=["test"],
                intermediate_commitments=[],
                answer_cluster="two",
            ),
        }
    )
    assert profile.approach_distance > 0
    assert profile.surface_distance >= 0
    assert profile.effective_channels.estimated_channels >= 1
    assert profile.novel_minority_approaches


def test_deliberation_policy_distinguishes_collapse_verification_and_ambiguity():
    contract = TaskContract(
        objective="x",
        estimated_difficulty=0.8,
        acceptance_criteria=[AcceptanceCriterion(id="AC-1", text="x")],
    )
    claim = Claim(id="C-1", text="x", testable=True, load_bearing=True)
    ledger = ClaimLedger(
        entries=[LedgerEntry(claim=claim, unresolved=True)],
        conflicts=["C-1"],
        acceptance_coverage={"AC-1": ["C-1"]},
    )
    high = make_profile(score=0.8)
    decision = choose_operation(
        index=1,
        contract=contract,
        profile=high,
        ledger=ledger,
        calibrated_confidence=0.5,
        remaining_calls=20,
        mandatory_calls=2,
        evidence_completeness=0.5,
        verifier_available=True,
    )
    assert decision.operation == "verify"
    assert decision.policy_inputs["estimated_difficulty"] == 0.8
    claim.testable = False
    decision = choose_operation(
        index=1,
        contract=contract,
        profile=high,
        ledger=ledger,
        calibrated_confidence=0.5,
        remaining_calls=20,
        mandatory_calls=2,
        evidence_completeness=0.5,
        verifier_available=True,
    )
    assert decision.operation == "targeted_rebuttal"


@pytest.mark.parametrize(
    ("task_kind", "profile_score", "confidence", "conflicts", "expected"),
    [
        ("subjective_tradeoff", 0.6, 0.7, True, "ranked_pairs"),
        ("evidence_synthesis", 0.6, 0.7, True, "synthesize"),
        ("safety_gate", 0.6, 0.7, True, "safety_validate"),
        ("objective_answer", 0.2, 0.5, False, "sample"),
    ],
)
def test_policy_task_specific_branches(
    task_kind, profile_score, confidence, conflicts, expected
):
    profile = make_profile(
        score=profile_score, low_diversity=profile_score <= 0.25
    )
    ledger = ClaimLedger(
        conflicts=["C-1"] if conflicts else [],
        entries=[
            LedgerEntry(
                claim=Claim(id="C-1", text="claim", testable=False),
                unresolved=conflicts,
            )
        ],
    )
    result = choose_operation(
        index=1,
        contract=TaskContract(objective="x", task_kind=task_kind),
        profile=profile,
        ledger=ledger,
        calibrated_confidence=confidence,
        remaining_calls=20,
        mandatory_calls=2,
        evidence_completeness=1,
    )
    assert result.operation == expected


def test_task_specific_aggregation_preserves_frontier_and_safety_veto():
    ballots = [
        JudgmentBallot(
            order=["A", "B"],
            action="select",
            selected_candidate="A",
            scores=[
                {
                    "candidate_label": "A",
                    "criterion_id": "cost",
                    "score": 5,
                    "reason": "cheap",
                },
                {
                    "candidate_label": "B",
                    "criterion_id": "quality",
                    "score": 5,
                    "reason": "strong",
                },
            ],
        )
    ]
    ranked = ranked_pairs(ballots)
    assert set(ranked.pareto_frontier) == {"A", "B"}
    safety = aggregate_ballots(
        "safety_gate",
        [
            JudgmentBallot(
                order=["A"],
                action="block",
                blockers=["credential exposure"],
            )
        ],
        ClaimLedger(),
        set(),
    )
    assert safety.method == "conservative_veto"
    assert safety.blockers == ["credential exposure"]
    separated = aggregate_ballots(
        "objective_answer",
        [
            JudgmentBallot(
                order=["A", "B"],
                action="select",
                selected_candidate="A",
                scores=[
                    {
                        "candidate_label": "A",
                        "criterion_id": "correct",
                        "score": 1,
                        "reason": "weak",
                    },
                    {
                        "candidate_label": "B",
                        "criterion_id": "correct",
                        "score": 5,
                        "reason": "verified",
                    },
                ],
            )
        ],
        ClaimLedger(),
        set(),
    )
    assert separated.selected_candidate == "B"


def test_reliability_cold_start_activation_decay_and_exploration(tmp_path):
    store = ReliabilityStore(tmp_path)
    snapshot = ReliabilitySnapshot(generated_at=datetime.now(timezone.utc))
    updated = snapshot
    for index in range(8):
        outcome = Outcome(
            run_id=f"r-{index}",
            status="confirmed",
            observations=[
                OutcomeObservation(
                    subject_type="claim",
                    subject_id="C-1",
                    status="confirmed",
                    weight=1,
                )
            ],
            recorded_at=datetime.now(timezone.utc),
        )
        updated = store.update(
            updated,
            outcome,
            [("m", "f", "proposer", "objective_answer", 1.0, 0.9)],
        )
    score, n, active = store.score(
        updated,
        model="m",
        family="f",
        role="proposer",
        task_kind="objective_answer",
    )
    assert active and n >= 8 and score > 0.5
    confidence_score, confidence_n, confidence_active = store.confidence_score(
        updated,
        model="m",
        family="f",
        role="proposer",
        task_kind="objective_answer",
        reported=0.9,
    )
    assert confidence_active and confidence_n >= 8 and confidence_score > 0.5
    decayed = store.load(now=datetime.now(timezone.utc) + timedelta(days=90))
    assert decayed.buckets[0].effective_observations == pytest.approx(4, rel=0.05)
    assert any(exploratory_run(f"run-{index}") for index in range(20))


def test_reliability_tracks_confusion_detection_order_and_pair_failure(tmp_path):
    store = ReliabilityStore(tmp_path)
    snapshot = ReliabilitySnapshot(generated_at=datetime.now(timezone.utc))
    for index in range(8):
        outcome = Outcome(
            run_id=f"r-{index}",
            status="disconfirmed",
            observations=[
                OutcomeObservation(
                    subject_type="claim",
                    subject_id="C-1",
                    status="disconfirmed",
                    weight=1,
                )
            ],
            recorded_at=datetime.now(timezone.utc),
        )
        snapshot = store.update(
            snapshot,
            outcome,
            [
                (
                    "a",
                    "fa",
                    "judge",
                    "objective_answer",
                    1,
                    0.9,
                    ["C-1"],
                    {
                        "prediction": "support",
                        "detected_error": False,
                        "order_consistent": False,
                    },
                ),
                (
                    "b",
                    "fb",
                    "critic",
                    "objective_answer",
                    1,
                    0.8,
                    ["C-1"],
                    {
                        "prediction": "support",
                        "detected_error": True,
                        "order_consistent": True,
                    },
                ),
            ],
        )
    judge = next(item for item in snapshot.buckets if item.model == "a")
    critic = next(item for item in snapshot.buckets if item.model == "b")
    assert judge.false_positive == 8
    assert critic.error_detection_success == 8
    assert not hasattr(snapshot, "pair_buckets")
    assert not hasattr(store, "pair_independence")


def test_reliability_isolated_by_task_domain(tmp_path):
    store = ReliabilityStore(tmp_path)
    snapshot = ReliabilitySnapshot(generated_at=datetime.now(timezone.utc))
    for index in range(8):
        snapshot = store.update(
            snapshot,
            Outcome(
                run_id=f"security-{index}",
                status="confirmed",
                observations=[
                    OutcomeObservation(
                        subject_type="claim",
                        subject_id="C-1",
                        status="confirmed",
                        weight=1,
                    )
                ],
                recorded_at=datetime.now(timezone.utc),
            ),
            [
                (
                    "m",
                    "f",
                    "verifier",
                    "objective_answer",
                    1,
                    None,
                    ["C-1"],
                    {"domain": "security_privacy"},
                )
            ],
        )
    security = store.score(
        snapshot,
        model="m",
        family="f",
        role="verifier",
        task_kind="objective_answer",
        domain="security_privacy",
    )
    performance = store.score(
        snapshot,
        model="m",
        family="f",
        role="verifier",
        task_kind="objective_answer",
        domain="performance_reliability",
    )
    assert security[2] and security[0] > 0.5
    assert performance == (0.5, 0, False)


@pytest.mark.asyncio
async def test_quorum_grace_cancels_straggler():
    async def result(value, delay):
        await asyncio.sleep(delay)
        return value

    values, failures, cancelled = await gather_with_quorum(
        {
            "a": ("google", result("a", 0.001)),
            "b": ("anthropic", result("b", 0.001)),
            "c": ("qwen", result("c", 1)),
        },
        grace_seconds=0.01,
    )
    assert set(values) == {"a", "b"}
    assert not failures
    assert cancelled == ["c"]


@pytest.mark.asyncio
async def test_v4_protocol_artifacts_anonymity_and_mirrored_judging(
    tmp_path, fake_settings
):
    result = await run_council(
        CouncilRequest(
            mode="decide",
            prompt="Choose the supported option.",
            budget_requested="quick",
            quorum_grace=0,
            verify_commands=["true"],
        ),
        state=tmp_path,
        settings=fake_settings,
        transport_factory=FakeTransport,
    )
    assert result.manifest.schema_version == 4
    assert result.manifest.calls_used <= 12
    assert result.verdict.evidence_refs
    store = RunStore.open_existing(
        tmp_path, result.run_id, SecretGuard(fake_settings.exact_secrets)
    )
    required = {
        "manifest.json",
        "snapshot.json",
        "role-assignments.json",
        "approach-profile.json",
        "claim-ledger.json",
        "judging/ballot-first.json",
        "judging/ballot-reversed.json",
        "judging/consistency.json",
        "selective-judgment.json",
        "failure-diagnosis.json",
        "verification-plan-user.json",
        "verdict.json",
        "private/identity-map.json",
    }
    assert required <= set(store.artifact_names())
    deterministic = [
        name
        for name in store.artifact_names()
        if name.startswith("verifications/VS-user-authorized-")
    ]
    assert deterministic
    assert store.read_json(deterministic[0])["status"] == "supported"
    assert all(
        "model" not in item and "family" not in item
        for item in store.read_json("role-assignments.json")
    )
    public_manifest = store.read_json("manifest.json")
    assert all(
        not route["label"].startswith("Candidate ")
        for route in public_manifest["routes"]
    )
    judge_prompts = [
        prompt for stage, _, prompt in FakeTransport.prompts if stage == "judging"
    ]
    assert len(judge_prompts) == 2
    assert all("gemini-3.1-pro-low" not in prompt for prompt in judge_prompts)


@pytest.mark.asyncio
async def test_evidence_extraction_is_independently_routed_and_source_grounded(
    tmp_path, fake_settings
):
    engine = CouncilEngine(
        CouncilRequest(
            mode="decide",
            prompt="Synthesize the supplied evidence.",
            budget_requested="standard",
            sources=["https://example.com/source"],
            quorum_grace=0,
        ),
        state=tmp_path,
        settings=fake_settings,
        transport_factory=FakeTransport,
    )
    try:
        await engine.preflight()
        source = engine.inventory.add(
            "https://example.com/source",
            "Primary source text",
            kind="source",
            priority=90,
        )
        contract = TaskContract(
            objective="Synthesize evidence",
            task_kind="evidence_synthesis",
            evidence_refs=[source.id],
            domain_tags=["http_semantics"],
        )
        extraction = await engine.extract_evidence(
            contract, engine.inventory.contents[source.id]
        )
        assert extraction is not None
        assert extraction.extractor_label == "Evidence extractor 1"
        assert extraction.claims[0].evidence_refs == [source.id]
        assert engine.identity_map["Evidence extractor 1"]["role"] == (
            "evidence_extractor"
        )
        assert engine.store._target("evidence-extraction.json").exists()
        assignment = next(
            item for item in engine.assignments if item.role == "evidence_extractor"
        )
        assert assignment.reasons
        hypotheses = await engine.hypotheses(
            contract,
            engine.inventory.contents[source.id],
        )
        assert hypotheses
        assert all(item.label.startswith("Candidate ") for item in hypotheses)
        assert "Evidence extractor 1" not in {item.label for item in hypotheses}
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_protocol_permits_exactly_one_schema_repair(tmp_path, fake_settings):
    FakeTransport.malformed_once = {"judging"}
    result = await run_council(
        CouncilRequest(
            mode="decide",
            prompt="Choose the supported option.",
            budget_requested="standard",
            quorum_grace=0,
        ),
        state=tmp_path,
        settings=fake_settings,
        transport_factory=FakeTransport,
    )
    assert result.manifest.status in {"completed", "blocked"}
    assert sum(":schema-repair" in stage for stage, _, _ in FakeTransport.prompts) == 1


@pytest.mark.asyncio
async def test_model_verification_requires_cross_family_consensus(
    tmp_path, fake_settings
):
    FakeTransport.verification_sequence = ["supported", "falsified"]
    engine = CouncilEngine(
        CouncilRequest(
            mode="decide",
            prompt="Check a disputed claim.",
            budget_requested="standard",
            quorum_grace=0,
        ),
        state=tmp_path,
        settings=fake_settings,
        transport_factory=FakeTransport,
    )
    try:
        await engine.preflight()
        claim = Claim(
            id="C-disputed",
            text="The evidence decides this claim",
            evidence_refs=[engine.inventory.refs[0].id],
            testable=True,
            load_bearing=True,
        )
        ledger = ClaimLedger(entries=[LedgerEntry(claim=claim, unresolved=True)])
        receipts = await engine.model_verifications(
            VerificationPlan(
                steps=[
                    VerificationStep(
                        id="VS-disputed-01",
                        claim_id=claim.id,
                        kind="evidence_entailment",
                        instruction="check",
                        executor_input=claim.text,
                        evidence_refs=claim.evidence_refs,
                    )
                ]
            ),
            TaskContract(objective="check"),
            ledger,
            engine.inventory.contents[engine.inventory.refs[0].id],
        )
        assert receipts[-1].status == "conflicting"
        assert receipts[-1].executor == "cross-family-verifier-consensus"
        assert (
            len(list(engine.store._target("verifications").glob("*-vote-*.json"))) == 2
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_high_risk_validation_revision_restarts_two_family_sequence(
    tmp_path, fake_settings
):
    FakeTransport.validation_sequence = [
        "blocked",
        "blocker_free",
        "blocker_free",
    ]
    result = await run_council(
        CouncilRequest(
            mode="decide",
            prompt="Approve this production migration safely.",
            budget_requested="max",
            quorum_grace=0,
        ),
        state=tmp_path,
        settings=fake_settings,
        transport_factory=FakeTransport,
    )
    store = RunStore.open_existing(
        tmp_path, result.run_id, SecretGuard(fake_settings.exact_secrets)
    )
    assert store._target("judging/revised-verdict.json").exists()
    revised = sorted(store._target("validations").glob("*-revised.json"))
    assert len(revised) == 2
    assert result.manifest.calls_used <= 60
    assert "independent high-risk validation incomplete" not in result.verdict.blockers


def test_deterministic_task_contract_fallback_has_task_kind():
    contract = deterministic_task_contract(
        "Review production migration safety", ["E-1"], "review"
    )
    assert contract.task_kind == "review"
    assert contract.risk_level == "high"
