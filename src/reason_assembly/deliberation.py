from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from itertools import combinations

from .contracts import (
    AggregationResult,
    ApproachProfile,
    ClaimLedger,
    DeliberationDecision,
    FailureDiagnosis,
    JudgmentAssessment,
    JudgmentBallot,
    OperationEffectProfile,
    TaskContract,
)
from .v4 import audit_bias, preference_entropy


BUDGET_CAPS = {"quick": 12, "standard": 30, "max": 60}
OPERATION_CAPS = {"quick": 1, "standard": 2, "max": 3}
GRACE_SECONDS = {"quick": 10.0, "standard": 20.0, "max": 30.0}


def canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", value.lower()).strip())


def choose_operation(
    *,
    index: int,
    contract: TaskContract,
    profile: ApproachProfile,
    ledger: ClaimLedger,
    calibrated_confidence: float,
    remaining_calls: int,
    mandatory_calls: int,
    evidence_completeness: float = 0.0,
    route_reliability: float = 0.5,
    verifier_available: bool = True,
    learned_effects: dict[str, OperationEffectProfile] | None = None,
    estimated_difficulty: float | None = None,
    risk_level: str | None = None,
) -> DeliberationDecision:
    reasons: list[str] = []
    operation = "direct_judgment"
    operation_utility = 0.0
    difficulty = (
        contract.estimated_difficulty
        if estimated_difficulty is None
        else estimated_difficulty
    )
    risk = risk_level or contract.risk_level
    aggregation = aggregation_for_task(contract.task_kind)
    testable_conflicts = [
        item.claim.id
        for item in ledger.entries
        if item.unresolved and item.claim.testable
    ]
    load_bearing_minority = [
        item.claim.id for item in ledger.entries
        if item.claim.load_bearing and item.unresolved
        and len(item.supporting_labels) == 1
    ]
    if remaining_calls <= mandatory_calls + 2:
        if (
            ledger.blockers
            or ledger.conflicts
            or ledger.load_bearing_unresolved
            or ledger.missing_evidence
        ):
            operation = "blocked_escalation"
            operation_utility = 0
            reasons.append(
                "unresolved material state remains after reserving mandatory calls"
            )
        else:
            reasons.append("remaining calls are reserved for mandatory judgment")
    elif testable_conflicts and verifier_available:
        operation = "verify"
        reasons.append("testable claim conflict")
    elif load_bearing_minority:
        operation = "minority_defense"
        reasons.append("novel load-bearing minority claim")
    elif profile.representational_collapse:
        operation = "sample"
        reasons.append("approach-level representational collapse")
    elif contract.task_kind == "subjective_tradeoff" and ledger.conflicts:
        operation = "ranked_pairs"
        reasons.append("subjective trade-off with conflicting preferences")
    elif contract.task_kind == "evidence_synthesis" and ledger.conflicts:
        operation = "synthesize"
        reasons.append("evidence synthesis needs claim-level fusion")
    elif contract.task_kind == "safety_gate" and (ledger.blockers or ledger.conflicts):
        operation = "safety_validate"
        reasons.append("safety gate has blockers or conflicting claims")
    elif route_reliability < 0.4 and ledger.conflicts:
        operation = "higher_order_aggregate"
        aggregation = "higher_order"
        reasons.append("external verification unavailable; use higher-order aggregation")
    elif ledger.conflicts and not testable_conflicts:
        operation = "targeted_rebuttal"
        reasons.append("non-testable ambiguity remains after higher-priority operations")
    elif ledger.missing_evidence and verifier_available:
        operation = "verify"
        reasons.append("load-bearing evidence is incomplete")
    elif ledger.conflicts or ledger.missing_evidence:
        operation = "targeted_rebuttal"
        reasons.append("unresolved non-testable conflict or missing evidence")
    else:
        if risk == "high" or difficulty >= 0.8:
            operation = "pairwise_compare"
            reasons.append("consequential close evaluation merits pairwise scoring")
        else:
            reasons.append(
                "acceptance coverage is stable and no valuable operation remains"
            )
    if learned_effects:
        active = [item for item in learned_effects.values() if item.active]
        if active:
            best = max(active, key=lambda item: item.utility)
            if best.utility <= 0:
                operation = "stop"
                reasons = ["no operation has positive learned utility"]
            else:
                operation = best.operation
                operation_utility = max(0, min(1, best.utility))
                reasons = ["learned posterior operation utility"]
    return DeliberationDecision(
        index=index,
        operation=operation,
        reasons=reasons,
        learned_operation_utility=operation_utility,
        mandatory_calls_reserved=mandatory_calls,
        remaining_calls=remaining_calls,
        aggregation=aggregation,
        policy_inputs={
            "task_kind": contract.task_kind,
            "estimated_difficulty": round(difficulty, 6),
            "approach_distance": round(profile.approach_distance, 6),
            "representational_collapse": profile.representational_collapse,
            "unresolved_claims": sum(item.unresolved for item in ledger.entries),
            "evidence_completeness": round(evidence_completeness, 6),
            "route_reliability": round(route_reliability, 6),
            "verifier_available": verifier_available,
            "remaining_calls": remaining_calls,
            "risk_level": risk,
        },
    )


def diagnose_failure_mode(
    *,
    ledger: ClaimLedger,
    selected_candidate: str | None,
    accepted_claim_ids: set[str],
    verified_claim_ids: set[str],
    structurally_supported_claim_ids: set[str],
) -> FailureDiagnosis:
    """Distinguish generation, aggregation, and verification failures.

    This is deliberately evidence-relative: without an external outcome the engine
    cannot know that a missing hypothesis is objectively correct.
    """

    load_bearing = {item.claim.id for item in ledger.entries if item.claim.load_bearing}
    generated_supported = load_bearing & (
        verified_claim_ids | structurally_supported_claim_ids
    )
    discarded = verified_claim_ids - accepted_claim_ids
    selected_claims = set(accepted_claim_ids)
    if selected_candidate and not selected_claims:
        selected_claims = {
            item.claim.id
            for item in ledger.entries
            if selected_candidate in item.supporting_labels
        }
    unverified = (selected_claims & load_bearing) - (
        verified_claim_ids | structurally_supported_claim_ids
    )
    if discarded:
        return FailureDiagnosis(
            state="aggregation_discarded_supported_hypothesis",
            generated_supported_claim_ids=sorted(generated_supported),
            aggregation_discarded_claim_ids=sorted(discarded),
            selected_unverified_claim_ids=sorted(unverified),
            response="reaggregate_with_verified_claims",
            reasons=["externally verified claims were omitted by aggregation"],
        )
    if unverified:
        return FailureDiagnosis(
            state="selected_hypothesis_unverified",
            generated_supported_claim_ids=sorted(generated_supported),
            selected_unverified_claim_ids=sorted(unverified),
            response="verify_selected_claims",
            reasons=["selected load-bearing claims lack admissible verification"],
        )
    coverage_missing = any(not claims for claims in ledger.acceptance_coverage.values())
    if not generated_supported and coverage_missing:
        return FailureDiagnosis(
            state="likely_generation_failure",
            response="sample_new_hypothesis",
            reasons=[
                "no generated load-bearing claim is independently supported",
                *(
                    ["acceptance criteria lack claim coverage"]
                    if coverage_missing
                    else []
                ),
            ],
        )
    if ledger.blockers or ledger.load_bearing_unresolved:
        return FailureDiagnosis(
            state="indeterminate",
            generated_supported_claim_ids=sorted(generated_supported),
            response="block",
            reasons=["material blockers prevent a more specific diagnosis"],
        )
    return FailureDiagnosis(
        state="none",
        generated_supported_claim_ids=sorted(generated_supported),
        response="none",
        reasons=["generated, aggregated, and verified support are aligned"],
    )
def aggregation_for_task(task_kind: str) -> str:
    return {
        "objective_answer": "verifier_weighted",
        "review": "verifier_weighted",
        "subjective_tradeoff": "ranked_pairs",
        "safety_gate": "conservative_veto",
        "evidence_synthesis": "claim_fusion",
        "implementation": "criterion_integration",
    }[task_kind]


def ranked_pairs(ballots: list[JudgmentBallot]) -> AggregationResult:
    candidates = sorted(
        {score.candidate_label for ballot in ballots for score in ballot.scores}
        | {ballot.selected_candidate for ballot in ballots if ballot.selected_candidate}
    )
    totals = Counter()
    pairwise = Counter()
    criterion_vectors: dict[str, dict[str, int]] = defaultdict(dict)
    for ballot in ballots:
        ballot_totals = Counter()
        for score in ballot.scores:
            totals[score.candidate_label] += score.score
            ballot_totals[score.candidate_label] += score.score
            criterion_vectors[score.candidate_label][score.criterion_id] = (
                criterion_vectors[score.candidate_label].get(score.criterion_id, 0)
                + score.score
            )
        if ballot.selected_candidate:
            totals[ballot.selected_candidate] += 1
            ballot_totals[ballot.selected_candidate] += 1
        for left, right in combinations(candidates, 2):
            if ballot_totals[left] > ballot_totals[right]:
                pairwise[(left, right)] += 1
            elif ballot_totals[right] > ballot_totals[left]:
                pairwise[(right, left)] += 1
    victories = []
    for left, right in combinations(candidates, 2):
        left_votes = pairwise[(left, right)]
        right_votes = pairwise[(right, left)]
        if left_votes != right_votes:
            winner, loser = (left, right) if left_votes > right_votes else (right, left)
            victories.append(
                (
                    abs(left_votes - right_votes),
                    max(left_votes, right_votes),
                    winner,
                    loser,
                )
            )
    locked: dict[str, set[str]] = defaultdict(set)

    def reaches(start: str, target: str) -> bool:
        pending = [start]
        seen = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(locked[current])
        return False

    for _, _, winner, loser in sorted(
        victories, key=lambda row: (-row[0], -row[1], row[2], row[3])
    ):
        if not reaches(loser, winner):
            locked[winner].add(loser)
    incoming = Counter(loser for winners in locked.values() for loser in winners)
    ranking = sorted(
        candidates,
        key=lambda value: (
            incoming[value],
            -len(locked[value]),
            -totals[value],
            value,
        ),
    )
    frontier = []
    for candidate in ranking:
        vector = criterion_vectors[candidate]
        dominated = any(
            other != candidate
            and all(
                criterion_vectors[other].get(key, 0) >= score
                for key, score in vector.items()
            )
            and any(
                criterion_vectors[other].get(key, 0) > score
                for key, score in vector.items()
            )
            for other in ranking
        )
        if not dominated:
            frontier.append(candidate)
    return AggregationResult(
        method="ranked_pairs",
        selected_candidate=ranking[0] if ranking else None,
        ranking=ranking,
        pareto_frontier=frontier,
        reasons=[
            "Tideman ranked-pairs locks strongest pairwise victories without cycles"
        ],
    )


def aggregate_ballots(
    task_kind: str,
    ballots: list[JudgmentBallot],
    ledger: ClaimLedger,
    verified_claim_ids: set[str],
    participant_reliability: dict[str, float] | None = None,
) -> AggregationResult:
    if task_kind == "subjective_tradeoff":
        return ranked_pairs(ballots)
    blockers = sorted(
        {blocker for ballot in ballots for blocker in ballot.blockers}
        | set(ledger.blockers)
    )
    if task_kind == "safety_gate":
        return AggregationResult(
            method="conservative_veto",
            blockers=blockers,
            accepted_claim_ids=sorted(verified_claim_ids),
            reasons=["any substantiated safety blocker vetoes approval"],
        )
    if task_kind == "evidence_synthesis":
        accepted = sorted(
            {claim_id for ballot in ballots for claim_id in ballot.accepted_claim_ids}
            | verified_claim_ids
        )
        return AggregationResult(
            method="claim_fusion",
            accepted_claim_ids=accepted,
            blockers=blockers,
            reasons=["claim-level union preserves provenance and verified dissent"],
        )
    criterion_totals = Counter()
    for ballot in ballots:
        if ballot.blockers:
            continue
        for score in ballot.scores:
            criterion_totals[score.candidate_label] += score.score
    for claim_id in verified_claim_ids:
        entry = next(
            (item for item in ledger.entries if item.claim.id == claim_id),
            None,
        )
        if entry:
            for label in entry.supporting_labels:
                criterion_totals[label] += 5
    votes = criterion_totals
    if participant_reliability:
        weighted = Counter()
        for index, ballot in enumerate(ballots):
            if ballot.blockers:
                continue
            reliability = participant_reliability.get(str(index), 0.5)
            for score in ballot.scores:
                weighted[score.candidate_label] += score.score * reliability
        votes = weighted
        method = "higher_order"
    else:
        method = "verifier_weighted"
    selected = (
        sorted(votes, key=lambda value: (-votes[value], value))[0] if votes else None
    )
    return AggregationResult(
        method=method,
        selected_candidate=selected,
        accepted_claim_ids=sorted(verified_claim_ids),
        blockers=blockers,
        reasons=[
            "mechanical support and criterion evidence outrank ungrounded preference"
            if selected
            else "abstained because no mechanically or criterion-supported candidate exists"
        ],
    )


def judgment_assessment(
    first: JudgmentBallot,
    second: JudgmentBallot,
    load_bearing_ids: set[str],
) -> JudgmentAssessment:
    changed_winner = (
        first.action != second.action
        or first.selected_candidate != second.selected_candidate
    )
    first_scores = {
        (row.candidate_label, row.criterion_id): row.score for row in first.scores
    }
    second_scores = {
        (row.candidate_label, row.criterion_id): row.score for row in second.scores
    }
    critical_delta = any(
        abs(first_scores[key] - second_scores[key]) > 1
        for key in set(first_scores) & set(second_scores)
    )
    changed_claims = sorted(
        (set(first.accepted_claim_ids) ^ set(second.accepted_claim_ids))
        & load_bearing_ids
    )
    totals: dict[str, int] = defaultdict(int)
    for ballot in (first, second):
        for row in ballot.scores:
            totals[row.candidate_label] += row.score
    ranked = sorted(totals, key=lambda value: (-totals[value], value))
    close_pair = (
        ranked[:2]
        if len(ranked) >= 2 and abs(totals[ranked[0]] - totals[ranked[1]]) <= 2
        else []
    )
    consistent = not (changed_winner or critical_delta or changed_claims)
    reasons = []
    if changed_winner:
        reasons.append("winner or action changed under reversed order")
    if critical_delta:
        reasons.append("criterion score changed by more than one")
    if changed_claims:
        reasons.append("accepted load-bearing claims changed")
    audit = audit_bias([
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ])
    semantic_left = int(first.selected_candidate == (first.order[0] if first.order else None))
    semantic_left += int(second.selected_candidate == (second.order[-1] if second.order else None))
    entropy = preference_entropy(semantic_left, 2, 0, 0)
    return JudgmentAssessment(
        consistent=consistent,
        changed_winner=changed_winner,
        critical_score_delta=critical_delta,
        changed_load_bearing_claims=changed_claims,
        close_pair=close_pair,
        tiebreaker_required=not consistent,
        reasons=reasons,
        preference_entropy=entropy,
        evaluations=2,
        bias_audit=audit,
    )


def deterministic_order(run_id: str, labels: list[str], suffix: str) -> list[str]:
    return sorted(
        labels,
        key=lambda label: hashlib.sha256(
            f"{run_id}:{suffix}:{label}".encode()
        ).hexdigest(),
    )
