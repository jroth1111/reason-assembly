from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Iterable

from .contracts import (
    ApproachProfile,
    ApproachSignature,
    BiasAudit,
    ClaimGenealogy,
    CoFailureProfile,
    EffectiveChannelProfile,
    VerificationReceipt,
    FinalityCertificate,
    GenealogyNode,
    MultiViewDiversity,
    OperationEffectProfile,
    ReportingRules,
    SelectiveJudgmentReceipt,
    TaintState,
    UncertaintyProfile,
)
from .state_compat import flock_exclusive


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def validate_judgment_risk(value: float, *, high_risk: bool = False,
                           implementation: bool = False) -> float:
    if not 0 < value <= 0.25:
        raise ValueError("--judgment-risk must be in (0, 0.25]")
    if (high_risk or implementation) and value > 0.05:
        return 0.05
    return value


def lock_rubric(criteria: list[dict], evidence_requirements: list[str],
                decision_rule: str, task_format: str, version: str = "4.0") -> dict:
    body = {
        "version": version,
        "criteria": criteria,
        "evidence_requirements": evidence_requirements,
        "decision_rule": decision_rule,
        "task_format": task_format,
    }
    return {**body, "sha256": digest(body)}


_WORD = re.compile(r"[a-z0-9][a-z0-9_.:/+-]*")


def _tokens(values: Iterable[str]) -> set[str]:
    return set(_WORD.findall(" ".join(values).lower()))


def signed_hash_vector(values: Iterable[str], dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    for token in sorted(_tokens(values)):
        raw = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(raw[:4], "big") % dimensions
        vector[index] += 1.0 if raw[4] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _cosine_distance(left: list[float], right: list[float]) -> float:
    return max(0.0, min(1.0, (1 - sum(a * b for a, b in zip(left, right))) / 2))


def _mean_pair_distance(rows: list[list[float]]) -> float:
    pairs = list(combinations(rows, 2))
    return sum(_cosine_distance(a, b) for a, b in pairs) / len(pairs) if pairs else 0.0


def _effective_rank(rows: list[list[float]]) -> float:
    # Stable dependency-free participation ratio of the feature energy spectrum.
    if not rows:
        return 0.0
    dimensions = len(rows[0])
    energies = [sum(row[i] * row[i] for row in rows) for i in range(dimensions)]
    total = sum(energies)
    return total * total / sum(value * value for value in energies) if total else 0.0


def approach_profile(signatures: dict[str, ApproachSignature],
                     surface_texts: dict[str, str] | None = None,
                     families: dict[str, str] | None = None) -> ApproachProfile:
    view_fields = {
        "decomposition": lambda s: s.decomposition,
        "operations_tools": lambda s: s.operations + s.tools,
        "evidence_assumptions": lambda s: s.evidence_classes + s.assumptions,
        "commitments": lambda s: s.intermediate_commitments + [s.answer_cluster],
    }
    views: list[MultiViewDiversity] = []
    distances: list[float] = []
    for name, getter in view_fields.items():
        vectors = [signed_hash_vector(getter(item)) for item in signatures.values()]
        distance = _mean_pair_distance(vectors)
        distances.append(distance)
        centroid = [
            sum(row[i] for row in vectors) / max(1, len(vectors))
            for i in range(64)
        ] if vectors else []
        views.append(MultiViewDiversity(
            view=name, signed_hash_vector=centroid,
            effective_rank=_effective_rank(vectors), mean_distance=distance,
        ))
    approach_distance = sum(distances) / len(distances) if distances else 0.0
    surface_vectors = [
        signed_hash_vector([text]) for text in (surface_texts or {}).values()
    ]
    surface_distance = _mean_pair_distance(surface_vectors)
    disagreement = (
        max(distances) - min(distances) if distances else 0.0
    )
    clusters = Counter(item.answer_cluster.strip().lower() for item in signatures.values())
    minority = sorted(
        label for label, item in signatures.items()
        if clusters[item.answer_cluster.strip().lower()] == 1
    )
    collapse = len(signatures) > 1 and approach_distance < 0.15
    warnings: list[str] = []
    if collapse:
        warnings.append("representational collapse: approaches share strategy")
    if surface_distance > approach_distance + 0.20:
        warnings.append("phrasing diversity masks strategy collapse")
    family_values = set((families or {}).values())
    estimated = max(1.0 if signatures else 0.0, sum(v.effective_rank for v in views) / max(1, len(views)))
    return ApproachProfile(
        surface_distance=surface_distance,
        approach_distance=approach_distance,
        views=views,
        metric_disagreement=disagreement,
        effective_channels=EffectiveChannelProfile(
            estimated_channels=min(float(len(signatures)), estimated),
            qualified_families=sorted(family_values),
            independence_deficit=len(family_values) < 2,
        ),
        representational_collapse=collapse,
        novel_minority_approaches=minority,
        warnings=warnings,
    )


def _binomial_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i)
               for i in range(k + 1))


def exact_binomial_interval(successes: int, trials: int,
                            alpha: float = 0.05) -> tuple[float, float]:
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("invalid binomial counts")
    if trials == 0:
        return 0.0, 1.0
    def bisect(predicate) -> float:
        low, high = 0.0, 1.0
        for _ in range(70):
            middle = (low + high) / 2
            if predicate(middle):
                high = middle
            else:
                low = middle
        return (low + high) / 2
    lower = 0.0 if successes == 0 else bisect(
        lambda p: 1 - _binomial_cdf(successes - 1, trials, p) >= alpha / 2
    )
    upper = 1.0 if successes == trials else bisect(
        lambda p: _binomial_cdf(successes, trials, p) <= alpha / 2
    )
    return lower, upper


class CoFailureStore:
    def __init__(self, root: Path, *, decay: float = 0.995):
        self.root = Path(root)
        self.path = self.root / "cofailure.json"
        self.decay = decay

    @staticmethod
    def key(routes: Iterable[str], families: Iterable[str], task_kind: str,
            domain: str, answer_format: str) -> str:
        return digest({
            "routes": sorted(set(routes)), "families": sorted(set(families)),
            "task_kind": task_kind, "domain": domain,
            "answer_format": answer_format,
        })

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 4, "buckets": {}}
        data = json.loads(self.path.read_text())
        if data.get("version") != 4:
            raise RuntimeError("co-failure store is not schema v4")
        return data

    def _write(self, data: dict) -> None:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        temp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    def record(self, *, routes: Iterable[str], families: Iterable[str],
               task_kind: str, domain: str, answer_format: str,
               route_correct: dict[str, bool], selected_correct: bool,
               verification_correct: bool | None = None) -> CoFailureProfile:
        routes = list(routes)
        families = list(families)
        key = self.key(routes, families, task_kind, domain, answer_format)
        with flock_exclusive(self.path.with_name(f"{self.path.name}.lock")):
            data = self._load()
            row = data["buckets"].setdefault(key, {
                "observations": 0.0, "all_wrong": 0.0, "oracle_correct": 0.0,
                "selected_correct": 0.0, "verification_wrong": 0.0,
                "route_correct": {},
                "high_risk": task_kind in {"safety_gate", "implementation"},
                "routes": sorted(set(routes)), "families": sorted(set(families)),
                "task_kind": task_kind, "domain": domain,
                "answer_format": answer_format,
            })
            for field in ("observations", "all_wrong", "oracle_correct",
                          "selected_correct", "verification_wrong"):
                row[field] *= self.decay
            for route in list(row["route_correct"]):
                row["route_correct"][route] *= self.decay
            row["observations"] += 1
            correct = list(route_correct.values())
            row["all_wrong"] += float(not any(correct))
            row["oracle_correct"] += float(any(correct))
            row["selected_correct"] += float(selected_correct)
            row["verification_wrong"] += float(verification_correct is False)
            for route, status in route_correct.items():
                row["route_correct"][route] = (
                    row["route_correct"].get(route, 0) + float(status)
                )
            self._write(data)
        return self.profile(key)

    def select_profile(self, *, routes: Iterable[str], families: Iterable[str],
                       task_kind: str, domain: str,
                       answer_format: str) -> CoFailureProfile:
        exact = self.key(routes, families, task_kind, domain, answer_format)
        data = self._load()
        if exact in data["buckets"]:
            return self.profile(exact)
        candidates = []
        family_set = sorted(set(families))
        for key, row in data["buckets"].items():
            specificity = 0
            if row.get("task_kind") != task_kind:
                continue
            specificity += 1
            if row.get("families") == family_set:
                specificity += 4
            if row.get("domain") == domain:
                specificity += 2
            if row.get("answer_format") == answer_format:
                specificity += 1
            candidates.append((specificity, float(row.get("observations", 0)), key))
        return self.profile(max(candidates)[2]) if candidates else self.profile(exact)

    def profile(self, key: str) -> CoFailureProfile:
        row = self._load()["buckets"].get(key)
        if not row:
            return CoFailureProfile(bucket_key=key, observations=0)
        n = max(1, round(row["observations"]))
        wrong = min(n, round(row["all_wrong"]))
        low, high = exact_binomial_interval(wrong, n)
        observations = row["observations"]
        oracle = row["oracle_correct"] / observations
        realized = row["selected_correct"] / observations
        strongest = max(row["route_correct"].values(), default=0) / observations
        threshold = 60 if row.get("high_risk") else 30
        active = observations >= threshold
        return CoFailureProfile(
            bucket_key=key, observations=observations,
            all_wrong_probability=row["all_wrong"] / observations,
            interval_low=low, interval_high=high,
            strongest_single_accuracy=strongest, oracle_accuracy=oracle,
            realized_accuracy=realized,
            orchestration_headroom=oracle - strongest,
            selection_loss=oracle - realized,
            verification_loss=row["verification_wrong"] / observations,
            active=active, uncertain=not active,
        )


def uncertainty_profile(*, cross_approach: float, within_route: float,
                        judge_variance: float, representation: float,
                        context_loss: float) -> UncertaintyProfile:
    aleatoric = min(1.0, (within_route + judge_variance + representation + context_loss) / 4)
    return UncertaintyProfile(
        epistemic=cross_approach, within_route_instability=within_route,
        judge_variance=judge_variance, representation_disagreement=representation,
        context_loss=context_loss, aleatoric_cost=aleatoric,
    )


def choose_cold_start_operation(*, testable_conflict: bool,
                                load_bearing_novelty: bool,
                                approach_collapse: bool, close_candidates: bool,
                                non_testable_ambiguity: bool) -> str:
    if testable_conflict:
        return "verify"
    if load_bearing_novelty:
        return "minority_defense"
    if approach_collapse:
        return "sample"
    if close_candidates:
        return "pairwise_compare"
    if non_testable_ambiguity:
        return "targeted_rebuttal"
    return "stop"


def operation_utility(operation: str, task_kind: str, observations: int,
                      resolved: int, load_bearing_epistemic: float,
                      aleatoric_cost: float, context_cost: float,
                      call_cost: float) -> OperationEffectProfile:
    probability = (resolved + 1) / (observations + 2)
    utility = probability * load_bearing_epistemic - aleatoric_cost - context_cost - call_cost
    return OperationEffectProfile(
        operation=operation, task_kind=task_kind, observations=observations,
        resolution_probability=probability, utility=utility,
        active=observations >= 20,
    )


def qualified_routes(routes: list[dict]) -> list[dict]:
    """Keep the strongest route and only additional quality-near routes."""
    if not routes:
        return []
    eligible = [row for row in routes if row.get("eligible", True)]
    if not eligible:
        return []
    active = [row for row in eligible if row.get("reliability_active")]
    score_key = "reliability" if active else "capability_score"
    anchor = max(eligible, key=lambda row: float(row.get(score_key, 0)))
    band = 0.10 if active else 0.15
    floor = float(anchor.get(score_key, 0)) - band
    return [
        row for row in eligible
        if row is anchor or float(row.get(score_key, 0)) >= floor
    ]


def preference_entropy(forward_left_wins: int, forward_total: int,
                       reverse_left_wins: int, reverse_total: int) -> float:
    total = forward_total + reverse_total
    if total == 0:
        return 1.0
    # Reversal maps the same semantic candidate back to the left identity.
    probability = (forward_left_wins + reverse_left_wins) / total
    if probability in {0, 1}:
        return 0.0
    return -(probability * math.log2(probability)
             + (1 - probability) * math.log2(1 - probability))


def next_active_comparison(candidates: list[str],
                           pair_uncertainty: dict[tuple[str, str], float],
                           acceptance_uncertainty: dict[str, float]) -> tuple[str, str] | None:
    pairs = list(combinations(sorted(candidates), 2))
    if not pairs:
        return None
    return max(
        pairs,
        key=lambda pair: pair_uncertainty.get(pair, 1.0)
        * max(acceptance_uncertainty.get(pair[0], 0),
              acceptance_uncertainty.get(pair[1], 0)),
    )


def higher_order_select(candidates: list[dict]) -> str | None:
    """Reliability/evidence/minority coherence aggregation; never vote counts."""
    if not candidates:
        return None
    def score(row: dict) -> float:
        reliability = float(row.get("route_reliability", 0.5))
        joint_failure = float(row.get("joint_failure_upper", 1.0))
        evidence = float(row.get("evidence_coherence", 0))
        predicted = float(row.get("predicted_peer_consensus", 0.5))
        minority = float(row.get("minority_internal_consistency", 0))
        unresolved = float(row.get("unresolved_lineage", 0))
        return (
            0.25 * reliability + 0.25 * (1 - joint_failure)
            + 0.25 * evidence + 0.10 * predicted + 0.15 * minority
            - 0.25 * unresolved
        )
    return max(candidates, key=score).get("label")


def audit_bias(ballots: list[dict]) -> BiasAudit:
    findings: list[str] = []
    winners_by_order: dict[str, set[str]] = {}
    for ballot in ballots:
        order = ballot.get("order", [])
        winner = ballot.get("selected_candidate")
        if order and winner:
            winners_by_order.setdefault("|".join(order), set()).add(winner)
        if ballot.get("reported_confidence", 0) > 0.9 and not ballot.get("evidence_refs"):
            findings.append("unsupported-confidence")
        if ballot.get("model") == ballot.get("candidate_model") and winner:
            findings.append("self-preference")
        if ballot.get("bandwagon_context"):
            findings.append("bandwagon")
        if ballot.get("evidence_omitted"):
            findings.append("evidence-omission")
    winners = set().union(*winners_by_order.values()) if winners_by_order else set()
    position = len(winners) > 1
    if position:
        findings.append("position")
    return BiasAudit(
        position=position,
        bandwagon="bandwagon" in findings,
        self_preference="self-preference" in findings,
        evidence_omission="evidence-omission" in findings,
        unsupported_confidence="unsupported-confidence" in findings,
        findings=sorted(set(findings)),
    )


def selective_judgment(*, scores_and_correct: list[tuple[float, bool]],
                       score: float, judgment_risk: float, high_risk: bool,
                       implementation: bool, deterministic: bool,
                       independently_verified: bool,
                       anchor_validated: bool = False) -> SelectiveJudgmentReceipt:
    risk = validate_judgment_risk(
        judgment_risk, high_risk=high_risk, implementation=implementation
    )
    minimum = 59 if risk <= 0.05 else 29
    threshold = None
    for candidate in sorted({value for value, _ in scores_and_correct}, reverse=True):
        accepted = [ok for value, ok in scores_and_correct if value >= candidate]
        if len(accepted) < minimum:
            continue
        errors = sum(not ok for ok in accepted)
        upper = exact_binomial_interval(errors, len(accepted), alpha=0.10)[1]
        if upper <= risk:
            threshold = candidate
            break
    calibrated = threshold is not None
    evidence_resolved = deterministic or independently_verified
    provisional = bool(
        not calibrated and anchor_validated and not high_risk and not implementation
    )
    abstained = (high_risk or implementation) and not calibrated and not evidence_resolved
    accepted = not abstained and (score >= threshold if calibrated else True)
    cap = 0.50 if provisional else (1.0 if calibrated or evidence_resolved else 0.65)
    lower = max(0.0, score - (risk if calibrated else 0.35))
    reasons = []
    if not calibrated:
        reasons.append(f"calibration inactive: requires {minimum} accepted examples")
    if abstained:
        reasons.append("cold-start high-risk acceptance requires deterministic or independent evidence")
    return SelectiveJudgmentReceipt(
        accepted=accepted, abstained=abstained, calibrated=calibrated,
        provisional=provisional, judgment_risk=risk, threshold=threshold,
        confidence_low=min(cap, lower), confidence_high=min(cap, score),
        calibration_examples=len(scores_and_correct), reasons=reasons,
    )


def finality_certificate(*, task_kind: str, accepted: bool, selective: SelectiveJudgmentReceipt,
                         rubric_sha256: str, reporting_rules_sha256: str,
                         deterministic_receipts: list[str],
                         independent_receipts: list[str],
                         qualified_families: list[str],
                         unresolved_claims: list[str]) -> FinalityCertificate:
    resolved = bool(deterministic_receipts or independent_receipts)
    if not accepted or selective.abstained or unresolved_claims:
        finality = "abort"
    elif task_kind == "implementation" and resolved:
        finality = "semantic_commit"
    else:
        finality = "verdict_commit"
    return FinalityCertificate(
        finality=finality, task_kind=task_kind, accepted=accepted and finality != "abort",
        calibrated=selective.calibrated, judgment_risk=selective.judgment_risk,
        deterministic_receipt_ids=deterministic_receipts,
        independent_receipt_ids=independent_receipts,
        qualified_families=qualified_families,
        unresolved_claim_ids=unresolved_claims,
        rubric_sha256=rubric_sha256,
        reporting_rules_sha256=reporting_rules_sha256,
    )


_INSTRUCTION = re.compile(
    r"(?i)\b(ignore (?:all|any|the) (?:previous|prior) instructions|system prompt|"
    r"developer message|execute this|run this command|you are chatgpt|do not follow)\b"
)


def quarantine_source(source_id: str, text: str) -> tuple[list[str], GenealogyNode]:
    suspicious = bool(_INSTRUCTION.search(text))
    claims = [
        line.strip() for line in text.splitlines()
        if line.strip() and not _INSTRUCTION.search(line)
    ]
    return claims, GenealogyNode(
        id=source_id, kind="source", tainted=suspicious,
        quarantine_reason="instruction-like retrieved content" if suspicious else None,
    )


def propagate_taint(graph: ClaimGenealogy, initially_tainted: Iterable[str]) -> TaintState:
    tainted = set(initially_tainted)
    transitions: list[dict[str, str]] = []
    changed = True
    while changed:
        changed = False
        for edge in graph.edges:
            if edge.source in tainted and edge.target not in tainted:
                tainted.add(edge.target)
                transitions.append({
                    "source": edge.source, "target": edge.target,
                    "reason": edge.relation,
                })
                changed = True
    for node in graph.nodes:
        node.tainted = node.id in tainted
    return TaintState(tainted_ids=sorted(tainted), transitions=transitions)


def proposer_verifier_independent(receipt: VerificationReceipt,
                                  joint_failure_rate: float | None,
                                  observations: int) -> bool:
    if receipt.deterministic:
        return True
    if receipt.proposer_route == receipt.verifier_route:
        return False
    if receipt.verifier_family and receipt.proposer_route and (
        receipt.proposer_route.split("/", 1)[0] == receipt.verifier_family
    ):
        return False
    if joint_failure_rate is not None and observations >= 30 and joint_failure_rate > 0.20:
        return False
    return receipt.independent


def default_reporting_rules() -> ReportingRules:
    return ReportingRules(version="4.0")
