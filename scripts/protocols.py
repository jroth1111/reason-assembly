from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from artifacts import EvidenceInventory, RunStore, SecretGuard, sha256_text
from contracts import (
    AcceptanceCriterion,
    ApproachSignature,
    Claim,
    ClaimLedger,
    ClaimNormalization,
    CoFailureProfile,
    ContributionGraph,
    EvidenceExtraction,
    ExclusionRecord,
    HealthResult,
    Hypothesis,
    ClaimGenealogy,
    GenealogyEdge,
    GenealogyNode,
    JudgmentAssessment,
    JudgmentBallot,
    MajoritySelfChallenge,
    MinorityDefense,
    RevisitClaim,
    RevisitReport,
    RoleAssignment,
    RolloutCard,
    SelectiveJudgmentReceipt,
    RouteRecord,
    RunManifest,
    SanitizedSnapshot,
    TaskContract,
    ValidationReceipt,
    Verdict,
    VerificationPlan,
    VerificationReceipt,
)
from deliberation import (
    BUDGET_CAPS,
    GRACE_SECONDS,
    OPERATION_CAPS,
    aggregate_ballots,
    canonical,
    choose_operation,
    diagnose_failure_mode,
    deterministic_order,
    judgment_assessment,
)
from reliability import ReliabilityStore
from routing import (
    Route,
    budget_for,
    build_claim_ledger,
    candidate_pool,
    exploration_adjust,
    fixed_route,
    gather_with_quorum,
    infer_task_kind,
    normalize_hypothesis,
    parse_route_override,
    risk_categories,
    select_role_routes,
    shuffled_labels,
    stable_claim_id,
)
from transport import (
    CallBudget,
    CallBudgetExceeded,
    ProxySettings,
    ProxyTransport,
    QuotaError,
)
from verification import (
    build_verification_plan,
    run_calculation_verifier,
    run_command_verifier,
    run_evidence_verifier,
    snapshot_sources,
)
from v4 import (
    CoFailureStore,
    approach_profile,
    audit_bias,
    default_reporting_rules,
    digest,
    finality_certificate,
    lock_rubric,
    next_active_comparison,
    operation_utility,
    propagate_taint,
    proposer_verifier_independent,
    qualified_routes,
    quarantine_source,
    selective_judgment,
    uncertainty_profile,
    validate_judgment_risk,
)
from v4_state import AnchorStore, RouteEpochStore, initialize_v4_state


STATE = Path("~/.local/state/ccycouncil").expanduser()
T = TypeVar("T", bound=BaseModel)


@dataclass
class CouncilRequest:
    mode: str
    prompt: str
    budget_requested: str = "adaptive"
    contexts: list[tuple[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    verify_commands: list[str] = field(default_factory=list)
    route_overrides: list[str] = field(default_factory=list)
    max_calls: int | None = None
    quorum_grace: float | None = None
    parent_run_id: str | None = None
    ancestry_relation: str | None = None
    prior_models: list[str] = field(default_factory=list)
    manifest_mode: str | None = None
    judgment_risk: float = 0.10
    repo: str | None = None
    base_commit: str | None = None
    review_target: str | None = None


@dataclass
class ProtocolResult:
    run_id: str
    verdict: Verdict
    exclusions: list[ExclusionRecord]
    manifest: RunManifest


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{os.urandom(4).hex()}"


def extract_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = min(
            [index for index in (stripped.find("{"), stripped.find("[")) if index >= 0],
            default=-1,
        )
        if start < 0:
            raise
        opener = stripped[start]
        closer = "}" if opener == "{" else "]"
        end = stripped.rfind(closer)
        if end < start:
            raise
        return json.loads(stripped[start : end + 1])


def schema_prompt(role: str, instructions: str, payload: Any, schema: dict) -> str:
    return (
        f"ROLE\n{role}\n\nINSTRUCTIONS\n{instructions}\n\n"
        "Return only one JSON object matching the schema exactly. "
        "Do not add fields and do not reveal model identity.\n\n"
        f"SCHEMA\n{json.dumps(schema, sort_keys=True)}\n\n"
        f"INPUT\n{json.dumps(payload, sort_keys=True, ensure_ascii=False)}"
    )


def deterministic_task_contract(
    task: str, evidence_ids: list[str], mode: str
) -> TaskContract:
    categories = risk_categories(task)
    kind = infer_task_kind(mode, task)
    verification = (
        "command"
        if kind in {"implementation", "review"}
        else "source"
        if kind == "evidence_synthesis"
        else "invariant"
        if kind == "safety_gate"
        else "evidence_entailment"
    )
    return TaskContract(
        original_task_sha256=sha256_text(task),
        objective=re.sub(r"\s+", " ", task.strip())[:1500] or "Resolve the task",
        task_kind=kind,
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-001",
                text="Address the stated objective using the available evidence.",
                verification=verification,
            )
        ],
        evidence_refs=evidence_ids,
        required_roles=["proposer", "judge"] + (["validator"] if categories else []),
        risk_level="high" if categories else "low",
        estimated_difficulty=min(
            1,
            0.25
            + len(task) / 12_000
            + 0.15 * len(categories)
            + (0.15 if mode in {"review", "implement", "red-team"} else 0),
        ),
        domain_tags=categories or [kind],
        risk_categories=categories,
    )


def health_status(error: BaseException) -> str:
    if isinstance(error, QuotaError):
        return "quota"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(error, CallBudgetExceeded):
        return "cancelled"
    return "timeout" if "timeout" in str(error).lower() else "unavailable"


def clean_blockers(values: list[str]) -> list[str]:
    empty = {
        "",
        "none",
        "none identified",
        "no blockers",
        "no blocker",
        "n/a",
        "not applicable",
    }
    cleaned = []
    for value in values:
        normalized = re.sub(r"[.\s]+$", "", value.strip().lower())
        no_blocker_statement = bool(
            re.match(
                r"^no (?:material )?blockers?(?: (?:identified|found|remain))?"
                r"(?:\s*[-:—]\s*.*)?$",
                normalized,
            )
        ) and not re.search(r"\b(?:except|but|however|unless)\b", normalized)
        if normalized not in empty and not no_blocker_statement:
            cleaned.append(value)
    return cleaned


class CouncilEngine:
    def __init__(
        self,
        request: CouncilRequest,
        *,
        state: Path = STATE,
        settings: ProxySettings | None = None,
        transport_factory: type[ProxyTransport] = ProxyTransport,
    ):
        self.request = request
        self.state = state.expanduser().resolve()
        self.settings = settings or ProxySettings(
            Path(os.environ["CCYPROXY_CONFIG"])
            if os.environ.get("CCYPROXY_CONFIG")
            else None
        )
        self.guard = SecretGuard(self.settings.exact_secrets)
        self.run_id = new_run_id()
        self.store = RunStore(self.state, self.run_id, self.guard)
        combined = (
            request.prompt + "\n" + "\n".join(value for _, value in request.contexts)
        )
        self.budget_name, self.budget_reasons = budget_for(
            request.budget_requested, combined, request.mode
        )
        cap = request.max_calls or BUDGET_CAPS[self.budget_name]
        self.budget = CallBudget(cap, self.store)
        self.transport = transport_factory(self.settings, budget=self.budget)
        self.inventory = EvidenceInventory()
        self.inventory.add("task", request.prompt, kind="task", priority=100)
        for source, content in request.contexts:
            self.inventory.add(source, content, kind="file", priority=70)
        self.reliability_store = ReliabilityStore(self.state / "v4")
        initialize_v4_state(self.state / "v4")
        self.cofailure_store = CoFailureStore(self.state / "v4")
        self.anchor_store = AnchorStore(self.state / "v4")
        self._context_packing: list[dict[str, Any]] = []
        self._context_drops: list[dict[str, Any]] = []
        self._source_genealogy_nodes: dict[str, GenealogyNode] = {}
        self._taint_transitions: list[dict[str, str]] = []
        self._operation_utilities = []
        self._cofailure_profiles: list[CoFailureProfile] = []
        self.reliability = self.reliability_store.load()
        self.catalogue = []
        self.member_routes: list[Route] = []
        self.candidate_routes: list[Route] = []
        self.alternate_routes: list[Route] = []
        self.judge_route: Route | None = None
        self.luna_route: Route | None = None
        self.integrator_route: Route | None = None
        self.assignments: list[RoleAssignment] = []
        self.identity_map: dict[str, dict[str, str]] = {}
        self.exclusions: list[ExclusionRecord] = []
        self.health: list[HealthResult] = []
        self.manifest = RunManifest(
            run_id=self.run_id,
            mode=request.manifest_mode or request.mode,  # type: ignore[arg-type]
            budget=self.budget_name,  # type: ignore[arg-type]
            budget_reasons=self.budget_reasons,
            created_at=datetime.now(timezone.utc),
            prompt_sha256=sha256_text(request.prompt),
            evidence=self.inventory.refs,
            repo=request.repo,
            base_commit=request.base_commit,
            review_target=request.review_target,
            call_cap=cap,
            parent_run_id=request.parent_run_id,
            ancestry_relation=request.ancestry_relation,  # type: ignore[arg-type]
            judgment_risk=validate_judgment_risk(request.judgment_risk),
        )

    @staticmethod
    def task_domain(contract: TaskContract) -> str:
        return (
            sorted(contract.domain_tags)[0]
            if contract.domain_tags
            else contract.task_kind
        )

    def persist_approach_profile(self, hypotheses: list[Hypothesis]):
        signatures = {
            item.label: item.approach_signature or ApproachSignature(
                decomposition=[item.method],
                operations=[item.method],
                constraints=[],
                assumptions=item.assumptions
                + [value for claim in item.claims for value in claim.assumptions],
                tools=sorted({
                    ref.split(":", 1)[0]
                    for claim in item.claims for ref in claim.evidence_refs
                }),
                evidence_classes=sorted({
                    "external" if ref.startswith("https://") else "preserved"
                    for claim in item.claims for ref in claim.evidence_refs
                }),
                intermediate_commitments=[claim.text for claim in item.claims],
                answer_cluster=canonical(item.recommendation),
            )
            for item in hypotheses
        }
        families = {
            item.label: self.identity_map.get(item.label, {}).get("family", "unknown")
            for item in hypotheses
        }
        profile = approach_profile(
            signatures,
            {item.label: item.recommendation for item in hypotheses},
            families,
        )
        self.store.write_json("approach-signatures.json", signatures)
        self.store.write_json("approach-profile.json", profile)
        return profile

    async def close(self) -> None:
        await self.transport.close()

    def persist_manifest(self) -> None:
        if self.manifest.status in {"completed", "blocked"} and self.store._target(
            "verdict.json"
        ).exists():
            verdict = Verdict.model_validate(self.store.read_json("verdict.json"))
            artifact_names = self.store.artifact_names()
            verification_traces = []
            for name in artifact_names:
                if name.startswith("verifications/") and name.endswith(".json"):
                    try:
                        verification_traces.append(
                            VerificationReceipt.model_validate(self.store.read_json(name))
                        )
                    except ValidationError:
                        continue
            deterministic = [
                item.id or item.step_id for item in verification_traces
                if item.deterministic and item.status == "supported"
            ] + [
                name for name in artifact_names
                if name == "receipts/final-tests.json"
                and all(
                    row.get("exit_code") == 0 and not row.get("timed_out")
                    for row in self.store.read_json(name)
                )
            ]
            independent = [
                name for name in self.store.artifact_names()
                if name.startswith("validations/")
            ]
            selective_path = self.store._target("selective-judgment.json")
            selective = (
                SelectiveJudgmentReceipt.model_validate(
                    self.store.read_json("selective-judgment.json")
                )
                if selective_path.exists()
                else SelectiveJudgmentReceipt(
                    accepted=verdict.finality != "abort",
                    abstained=verdict.abstained,
                    calibrated=verdict.calibrated,
                    judgment_risk=verdict.judgment_risk,
                    confidence_low=verdict.confidence,
                    confidence_high=verdict.confidence,
                    calibration_examples=0,
                )
            )
            certificate = finality_certificate(
                task_kind=self.manifest.task_kind or "objective_answer",
                accepted=verdict.finality != "abort",
                selective=selective,
                rubric_sha256=self.manifest.rubric_sha256 or "",
                reporting_rules_sha256=self.manifest.reporting_rules_sha256 or "",
                deterministic_receipts=deterministic,
                independent_receipts=independent,
                qualified_families=sorted({route.family for route in self.manifest.routes}),
                unresolved_claims=verdict.unresolved,
            )
            if verdict.finality == "semantic_commit" and certificate.finality != "semantic_commit":
                verdict.finality = "abort"
                verdict.abstained = True
                self.store.write_json("verdict.json", verdict)
            self.manifest.finality = certificate.finality
            self.store.write_json("finality-certificate.json", certificate)
            ballots = [
                self.store.read_json(name)
                for name in artifact_names
                if name.startswith("judging/ballot-") and name.endswith(".json")
            ]
            policy_decisions = [
                self.store.read_json(name)
                for name in artifact_names
                if name.startswith("policy/decision-") and name.endswith(".json")
            ]
            nodes = [
                self._source_genealogy_nodes.get(
                    item.id, GenealogyNode(id=item.id, kind="source")
                )
                for item in self.inventory.refs
            ]
            edges: list[GenealogyEdge] = []
            for item in verification_traces:
                verification_id = item.id or item.step_id
                nodes.append(GenealogyNode(id=verification_id, kind="verification"))
                edges.append(GenealogyEdge(
                    source=item.claim_id,
                    target=verification_id,
                    relation="verified_by",
                ))
                edges.append(GenealogyEdge(
                    source=verification_id,
                    target="V-final",
                    relation="informs",
                ))
            extraction_path = self.store._target("evidence-extraction.json")
            if extraction_path.exists():
                nodes.append(GenealogyNode(id="X-extraction", kind="extraction"))
                for ref in self.inventory.refs:
                    if ref.kind in {"source", "pdf"}:
                        edges.append(GenealogyEdge(
                            source=ref.id,
                            target="X-extraction",
                            relation="extracted_by",
                        ))
            ledger_path = self.store._target("claim-ledger.json")
            if ledger_path.exists():
                ledger = ClaimLedger.model_validate(
                    self.store.read_json("claim-ledger.json")
                )
                for entry in ledger.entries:
                    nodes.append(GenealogyNode(id=entry.claim.id, kind="claim"))
                    for evidence_ref in entry.claim.evidence_refs:
                        edges.append(GenealogyEdge(
                            source=evidence_ref,
                            target=entry.claim.id,
                            relation="supports",
                        ))
                    edges.append(GenealogyEdge(
                        source=entry.claim.id,
                        target="V-final",
                        relation="considered_by",
                    ))
            graph_path = self.store._target("contribution-graph.json")
            if graph_path.exists():
                graph = ContributionGraph.model_validate(
                    self.store.read_json("contribution-graph.json")
                )
                for contribution in graph.contributions:
                    nodes.append(GenealogyNode(
                        id=contribution.id,
                        kind="contribution",
                        tainted=(
                            contribution.id in set(graph.selected_ids)
                            and (
                                not contribution.verified
                                or bool(contribution.conflicts)
                                or not contribution.verification_receipt_ids
                            )
                        ),
                        quarantine_reason=(
                            "selected contribution lacks clean deterministic provenance"
                            if contribution.id in set(graph.selected_ids)
                            and (
                                not contribution.verified
                                or bool(contribution.conflicts)
                                or not contribution.verification_receipt_ids
                            )
                            else None
                        ),
                    ))
                    for dependency in contribution.dependencies:
                        edges.append(GenealogyEdge(
                            source=dependency,
                            target=contribution.id,
                            relation="prerequisite",
                        ))
                    edges.append(GenealogyEdge(
                        source=contribution.id,
                        target="V-final",
                        relation="integrated_into",
                    ))
            nodes.append(GenealogyNode(id="V-final", kind="verdict"))
            genealogy = ClaimGenealogy(nodes=nodes, edges=edges)
            taint = propagate_taint(
                genealogy, [node.id for node in nodes if node.tainted]
            )
            self._taint_transitions = taint.transitions
            if "V-final" in taint.tainted_ids:
                certificate.finality = "abort"
                certificate.accepted = False
                verdict.finality = "abort"
                verdict.abstained = True
                verdict.blockers.append("tainted claim lineage reaches verdict")
                self.store.write_json("verdict.json", verdict)
            self.store.write_json("taint-state.json", taint)
            self.store.write_json("claim-genealogy.json", genealogy)
            self.store.write_json(
                "rollout-card.json",
                RolloutCard(
                    run_id=self.run_id,
                    rubric_sha256=certificate.rubric_sha256,
                    reporting_rules_sha256=certificate.reporting_rules_sha256,
                    call_manifest=[
                        event.model_dump(mode="json") for event in self.budget.events
                    ],
                    route_versions={
                        route.label: route.model for route in self.manifest.routes
                    },
                    sanitized_prompt_sha256s=[self.manifest.prompt_sha256],
                    context_packing=self._context_packing,
                    ballots=ballots,
                    verification_traces=verification_traces,
                    genealogy=genealogy,
                    taint_transitions=self._taint_transitions,
                    policy_decisions=policy_decisions,
                    operation_utilities=self._operation_utilities,
                    cofailure=self._cofailure_profiles,
                    finality=certificate,
                    drops=[
                        {
                            "stage": "context-packing",
                            "route": item.get("evidence_id", "unknown"),
                            "reason": item["reason"],
                            "counted_call": False,
                        }
                        for item in self._context_drops
                    ],
                    patches=[
                        {"artifact": name, "sha256": sha256_text(
                            self.store._target(name).read_text(errors="replace")
                        )}
                        for name in artifact_names if name.startswith("patches/")
                    ],
                    receipts=[
                        {"artifact": name}
                        for name in deterministic + independent
                    ],
                ),
            )
        self.manifest.calls_used = self.budget.used
        self.manifest.exclusions = self.exclusions
        self.manifest.evidence = self.inventory.refs
        self.manifest.artifacts = self.store.artifact_names()
        self.store.write_json("manifest.json", self.manifest)

    def remaining_calls(self) -> int:
        return max(0, self.budget.cap - self.budget.used)

    def learned_operation_effects(self, contract: TaskContract):
        path = self.state / "v4" / "operation-effects.json"
        rows = json.loads(path.read_text()).get("effects", []) if path.exists() else []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get("task_kind") == contract.task_kind:
                grouped.setdefault(str(row["operation"]), []).append(row)
        effects = {
            operation: operation_utility(
                operation,
                contract.task_kind,
                len(values),
                sum(bool(item.get("resolved")) for item in values),
                1.0,
                sum(float(item.get("aleatoric_cost", 0)) for item in values)
                / max(1, len(values)),
                sum(float(item.get("context_cost", 0)) for item in values)
                / max(1, len(values)),
                sum(float(item.get("calls", 0)) for item in values)
                / max(1, len(values) * self.manifest.call_cap),
            )
            for operation, values in grouped.items()
        }
        self._operation_utilities = list(effects.values())
        return effects

    async def maybe_run_route_anchor(self, reserved_calls: int) -> None:
        active_role_calls = sum(
            event.stage not in {"health", "extraction"}
            and not event.stage.startswith("anchor")
            for event in self.budget.events
        )
        anchors = self.anchor_store.list(active_only=True)
        if (
            active_role_calls < 20
            or not anchors
            or self.remaining_calls() <= reserved_calls
            or not self.judge_route
            or not self.manifest.route_epoch
        ):
            return
        anchor = anchors[
            int(hashlib.sha256(self.run_id.encode()).hexdigest()[:8], 16)
            % len(anchors)
        ]
        text, usage = await self.transport.ask(
            run_id=self.run_id,
            participant=f"anchor-{anchor.id}",
            model=self.judge_route.model,
            effort=self.judge_route.effort,
            prompt=(
                "Return only the answer to this fixed calibration task. "
                "Do not explain.\nTASK\n" + anchor.task
            ),
            stage="anchor",
            max_output_tokens=256,
        )
        passed = canonical(text) == canonical(anchor.expected)
        epoch = RouteEpochStore(self.state / "v4").record_anchor(
            self.manifest.route_epoch, anchor.id, passed
        )
        self.store.write_json("anchors/interleaved.json", {
            "anchor_id": anchor.id,
            "passed": passed,
            "usage": usage,
            "route_epoch": epoch.model_dump(mode="json"),
        })

    def persist_role_assignments(self) -> None:
        self.store.write_json(
            "role-assignments.json",
            [
                {
                    "label": item.label,
                    "role": item.role,
                    "score": item.score,
                    "role_fit": item.role_fit,
                    "reliability": item.reliability,
                    "independence": item.independence,
                    "health_latency_score": item.health_latency_score,
                    "exploratory": item.exploratory,
                    "reasons": item.reasons,
                }
                for item in self.assignments
            ],
        )
        self.store.write_json("private/role-assignments.json", self.assignments)

    async def validated_call(
        self,
        route: Route,
        *,
        participant: str,
        stage: str,
        role: str,
        instructions: str,
        payload: Any,
        contract: type[T],
        max_output_tokens: int = 5000,
    ) -> tuple[T, dict[str, Any]]:
        prompt = schema_prompt(
            role, instructions, payload, contract.model_json_schema()
        )

        def repack() -> str:
            context = route.capability.context_window or 32_000
            packed, included = self.inventory.packed(
                int(context * 0.55),
                reserve_tokens=max(1000, len(prompt) // 4),
            )
            return schema_prompt(
                role,
                instructions
                + " Context was coverage-repacked. Treat omitted structured fields "
                "as unavailable evidence.",
                {"evidence": packed, "evidence_coverage": included},
                contract.model_json_schema(),
            )

        text, usage = await self.transport.ask(
            run_id=self.run_id,
            participant=participant,
            model=route.model,
            effort=route.effort,
            prompt=prompt,
            stage=stage,
            max_output_tokens=max_output_tokens,
            repack=repack,
        )
        try:
            return contract.model_validate(extract_json(text)), usage
        except (ValidationError, ValueError, json.JSONDecodeError) as first:
            repaired, repair_usage = await self.transport.ask(
                run_id=self.run_id,
                participant=participant + "-repair",
                model=route.model,
                effort=route.effort,
                prompt=(
                    "Repair this object to match the schema exactly. Return only JSON.\n"
                    f"SCHEMA\n{json.dumps(contract.model_json_schema(), sort_keys=True)}\n"
                    f"INVALID\n{text}"
                ),
                stage=f"{stage}:schema-repair",
                max_output_tokens=max_output_tokens,
            )
            try:
                value = contract.model_validate(extract_json(repaired))
                usage["repair"] = repair_usage
                return value, usage
            except (ValidationError, ValueError, json.JSONDecodeError) as second:
                raise RuntimeError(
                    f"malformed {contract.__name__} after one repair: {first}; {second}"
                ) from second

    def _override(self, role: str) -> str | None:
        for spec in self.request.route_overrides:
            override_role, model, effort = parse_route_override(spec)
            if override_role == role:
                return f"{model}:{effort}"
        return None

    async def preflight(
        self, role: str = "proposer"
    ) -> tuple[list[Route], Route, Route | None]:
        self.catalogue = await self.transport.catalogue()
        epoch = RouteEpochStore(self.state / "v4").current(
            [item.model_dump(mode="json") for item in self.catalogue]
        )
        self.manifest.route_epoch = epoch.id
        if not epoch.validated:
            for bucket in self.reliability.buckets:
                bucket.active = False
            for bucket in self.reliability.confidence_buckets:
                bucket.active = False
        self.store.write_json("catalogue.json", self.catalogue)
        pool = candidate_pool(
            self.catalogue,
            self.request.route_overrides,
            budget=self.budget_name,
            role=role,  # type: ignore[arg-type]
            prior_models=self.request.prior_models,
        )
        self.candidate_routes = pool
        if len({route.family for route in pool}) < 2:
            raise RuntimeError("candidate pool has fewer than two provider families")
        judge = fixed_route(
            self.catalogue,
            self._override("judge") or "gpt-5.6-sol:medium",
            "judge",
        )
        try:
            luna = fixed_route(self.catalogue, "gpt-5.6-luna:low", "utility")
        except RuntimeError:
            luna = None
        integrator = None
        if role == "worker":
            try:
                integrator = fixed_route(
                    self.catalogue,
                    self._override("integrator") or "gpt-5.6-sol:medium",
                    "integrator",
                )
            except RuntimeError:
                integrator = None
        unique = {route.model: route for route in pool}
        unique[judge.model] = judge
        for spec in self.request.route_overrides:
            override_role, model, effort = parse_route_override(spec)
            if model in unique:
                continue
            try:
                unique[model] = fixed_route(
                    self.catalogue,
                    f"{model}:{effort}",
                    override_role,
                )
            except RuntimeError as error:
                raise RuntimeError(f"invalid explicit route {spec}: {error}") from error
        if luna:
            unique[luna.model] = luna
        if integrator:
            unique[integrator.model] = integrator

        async def check(route: Route) -> HealthResult:
            started = asyncio.get_running_loop().time()
            effort = "low" if "low" in route.capability.efforts else route.effort
            try:
                await asyncio.wait_for(
                    self.transport.ask(
                        run_id=self.run_id,
                        participant="health-"
                        + hashlib.sha1(route.model.encode()).hexdigest()[:8],
                        model=route.model,
                        effort=effort,
                        prompt="Reply with exactly OK.",
                        stage="health",
                        max_output_tokens=16,
                    ),
                    timeout=max(10.0, GRACE_SECONDS[self.budget_name]),
                )
                return HealthResult(
                    model=route.model,
                    family=route.family,
                    status="healthy",
                    latency_ms=int(
                        (asyncio.get_running_loop().time() - started) * 1000
                    ),
                )
            except BaseException as error:
                return HealthResult(
                    model=route.model,
                    family=route.family,
                    status=health_status(error),  # type: ignore[arg-type]
                    latency_ms=int(
                        (asyncio.get_running_loop().time() - started) * 1000
                    ),
                    detail=self.guard.redact_text(str(error))[:500],
                )

        self.health = await asyncio.gather(*(check(route) for route in unique.values()))
        self.store.write_json("health.json", self.health)
        healthy = {item.model for item in self.health if item.status == "healthy"}
        healthy_pool = [route for route in pool if route.model in healthy]
        if len({item.family for item in healthy_pool}) < 2:
            raise RuntimeError("below quorum after preflight")
        inferred_kind = infer_task_kind(self.request.mode, self.request.prompt)
        latency = {item.model: item.latency_ms or 10_000 for item in self.health}
        scored = select_role_routes(
            healthy_pool,
            role=role,  # type: ignore[arg-type]
            task_kind=inferred_kind,
            domain=inferred_kind,
            count=(2 if self.budget_name == "quick" else 3) + 1,
            snapshot=self.reliability,
            reliability_store=self.reliability_store,
            health_latency=latency,
        )
        scored = exploration_adjust(scored, self.run_id)
        qualified_models = {
            row["model"]
            for row in qualified_routes([
                {
                    "model": route.model,
                    "eligible": True,
                    "reliability_active": "active reliability" in assignment.reasons,
                    "reliability": assignment.reliability,
                    "capability_score": assignment.role_fit,
                }
                for route, assignment in scored
            ])
        }
        scored = [row for row in scored if row[0].model in qualified_models]
        target = 2 if self.budget_name == "quick" else 3
        selected = [row[0] for row in scored[:target]]
        labels = shuffled_labels(self.run_id, len(selected))
        self.assignments = []
        for label, (_, assignment) in zip(labels, scored[:target]):
            assignment.label = label
            self.assignments.append(assignment)
        self.alternate_routes = [
            route for route in pool if route.model in healthy and route not in selected
        ]
        for route in pool:
            result = next(item for item in self.health if item.model == route.model)
            if route not in selected:
                reason = (
                    "healthy alternate not selected"
                    if result.status == "healthy"
                    else f"preflight {result.status}: {result.detail or 'no detail'}"
                )
                self.exclusions.append(
                    ExclusionRecord(
                        model=route.model,
                        family=route.family,
                        role=role,  # type: ignore[arg-type]
                        reason=reason,
                    )
                )
        judge_health = next(item for item in self.health if item.model == judge.model)
        if judge_health.status != "healthy":
            replacement = next(
                (
                    Route(route.capability, route.effort, "judge")
                    for route in selected + self.alternate_routes
                    if "judge" in route.capability.roles
                ),
                None,
            )
            if not replacement:
                raise RuntimeError(
                    f"no healthy judge; preferred judge {judge_health.status}"
                )
            self.exclusions.append(
                ExclusionRecord(
                    model=judge.model,
                    family=judge.family,
                    role="judge",
                    reason=f"preflight {judge_health.status}; substituted",
                )
            )
            judge = replacement
        if luna:
            luna_health = next(item for item in self.health if item.model == luna.model)
            if luna_health.status != "healthy":
                self.exclusions.append(
                    ExclusionRecord(
                        model=luna.model,
                        family=luna.family,
                        role="utility",
                        reason=(
                            f"preflight {luna_health.status}; deterministic fallback"
                        ),
                    )
                )
                luna = None
        if (
            integrator
            and next(
                item for item in self.health if item.model == integrator.model
            ).status
            != "healthy"
        ):
            integrator = None
        self.member_routes = selected
        self.judge_route = judge
        self.luna_route = luna
        self.integrator_route = integrator
        self.persist_role_assignments()
        self.manifest.routes = [
            RouteRecord(
                label=f"{assignment.role.title()} route {index}",
                model=assignment.model,
                family=assignment.family,
                effort=assignment.effort,
                role=assignment.role,
            )
            for index, assignment in enumerate(self.assignments, 1)
        ] + [
            RouteRecord(
                label="Judge",
                model=judge.model,
                family=judge.family,
                effort=judge.effort,
                role="judge",
            )
        ]
        return selected, judge, luna

    def reroute_primary_role(self, contract: TaskContract, role: str) -> list[Route]:
        previous = list(self.member_routes)
        healthy = {item.model for item in self.health if item.status == "healthy"}
        candidates = [item for item in self.candidate_routes if item.model in healthy]
        latency = {item.model: item.latency_ms or 10_000 for item in self.health}
        target = 2 if self.budget_name == "quick" else 3
        scored = select_role_routes(
            candidates,
            role=role,  # type: ignore[arg-type]
            task_kind=contract.task_kind,
            domain=self.task_domain(contract),
            count=target + 1,
            snapshot=self.reliability,
            reliability_store=self.reliability_store,
            health_latency=latency,
        )
        scored = exploration_adjust(scored, self.run_id)
        if len({item[0].family for item in scored}) < 2:
            raise RuntimeError("below family quorum after task-specific rerouting")
        scored = scored[:target]
        self.member_routes = [item[0] for item in scored]
        selected_models = {item.model for item in self.member_routes}
        self.exclusions = [
            item
            for item in self.exclusions
            if not (
                item.role == role
                and item.model in selected_models
                and item.reason == "healthy alternate not selected"
            )
        ]
        for item in previous:
            if item.model not in selected_models:
                self.exclusions.append(
                    ExclusionRecord(
                        model=item.model,
                        family=item.family,
                        role=role,  # type: ignore[arg-type]
                        reason="displaced by task-contract role rerouting",
                    )
                )
        labels = shuffled_labels(self.run_id, len(scored))
        self.assignments = []
        for label, (_, assignment) in zip(labels, scored):
            assignment.label = label
            self.assignments.append(assignment)
        self.alternate_routes = [
            item
            for item in candidates
            if item not in self.member_routes and item.model in healthy
        ]
        self.persist_role_assignments()
        self.manifest.routes = [
            RouteRecord(
                label=f"{item.role.title()} route {index}",
                model=item.model,
                family=item.family,
                effort=item.effort,
                role=item.role,
            )
            for index, item in enumerate(self.assignments, 1)
        ] + [
            RouteRecord(
                label="Judge",
                model=self.judge_route.model,
                family=self.judge_route.family,
                effort=self.judge_route.effort,
                role="judge",
            )
        ]
        return self.member_routes

    def route_for_role(
        self,
        role: str,
        contract: TaskContract,
        *,
        exclude_families: set[str] | None = None,
    ) -> Route | None:
        healthy = {item.model for item in self.health if item.status == "healthy"}
        explicit = self._override(role)
        if explicit:
            route = fixed_route(self.catalogue, explicit, role)  # type: ignore[arg-type]
            if route.model not in healthy or route.family in (
                exclude_families or set()
            ):
                return None
            return route
        latency = {item.model: item.latency_ms or 10_000 for item in self.health}
        rows = select_role_routes(
            [item for item in self.catalogue if item.id in healthy],
            role=role,  # type: ignore[arg-type]
            task_kind=contract.task_kind,
            domain=self.task_domain(contract),
            count=1,
            snapshot=self.reliability,
            reliability_store=self.reliability_store,
            health_latency=latency,
            excluded_families=exclude_families,
        )
        if not rows:
            return None
        route, assignment = rows[0]
        assignment.label = f"{role.title()} {len(self.assignments) + 1}"
        self.assignments.append(assignment)
        self.persist_role_assignments()
        return route

    async def task_contract(self) -> TaskContract:
        fallback = deterministic_task_contract(
            self.request.prompt,
            [ref.id for ref in self.inventory.refs],
            self.request.mode,
        )
        if not self.luna_route or self.remaining_calls() < 4:
            return fallback
        try:
            contract, usage = await self.validated_call(
                self.luna_route,
                participant="task-contract",
                stage="extraction",
                role="Task contract extractor",
                instructions=(
                    "Extract the objective, task kind, acceptance criteria, constraints, "
                    "required epistemic roles, evidence IDs, and risk. Do not decide."
                ),
                payload={
                    "task": self.request.prompt,
                    "mode": self.request.mode,
                    "evidence": [
                        ref.model_dump(mode="json") for ref in self.inventory.refs
                    ],
                },
                contract=TaskContract,
                max_output_tokens=4000,
            )
            contract.original_task_sha256 = sha256_text(self.request.prompt)
            contract.evidence_refs = [ref.id for ref in self.inventory.refs]
            for index, criterion in enumerate(contract.acceptance_criteria):
                criterion.id = f"AC-{index + 1:03d}"
            if not contract.acceptance_criteria:
                contract.acceptance_criteria = fallback.acceptance_criteria
            self.store.append_event("task_contract", usage=usage)
            return contract
        except BaseException as error:
            self.store.append_event(
                "task_contract_fallback",
                reason=self.guard.redact_text(str(error))[:500],
            )
            return fallback

    async def packed_evidence(
        self, routes: list[Route], *, include_retrieved_sources: bool = False
    ) -> str:
        minimum = min(route.capability.context_window or 32_000 for route in routes)
        limit = int(minimum * 0.60)
        rows: list[str] = []
        used = 0
        refs = sorted(self.inventory.refs, key=lambda item: (-item.priority, item.id))
        for ref in refs:
            if ref.kind in {"source", "pdf"} and not include_retrieved_sources:
                continue
            content = self.inventory.contents.get(ref.id, "")
            claims, node = quarantine_source(ref.id, content)
            if node.tainted:
                self._source_genealogy_nodes[ref.id] = node
            safe = "\n".join(claims)
            block = f"[EVIDENCE {ref.id} source={ref.source}]\n{safe}\n[/EVIDENCE]"
            if used + len(block) > limit:
                self._context_drops.append({
                    "evidence_id": ref.id,
                    "reason": "context limit",
                    "size": len(block),
                })
                continue
            rows.append(block)
            used += len(block)
        self._context_packing.append({
            "limit": limit,
            "used": used,
            "included": [ref.id for ref in refs if any(ref.id in row for row in rows)],
            "retrieved_sources": include_retrieved_sources,
        })
        return "\n\n".join(rows)

    async def extract_evidence(
        self, contract: TaskContract, evidence: str
    ) -> EvidenceExtraction | None:
        if (
            contract.task_kind != "evidence_synthesis" and not self.request.sources
        ) or self.remaining_calls() < 4:
            return None
        route = self.route_for_role("evidence_extractor", contract)
        if not route:
            return None
        label = "Evidence extractor 1"
        self.identity_map[label] = {
            "model": route.model,
            "family": route.family,
            "effort": route.effort,
            "role": "evidence_extractor",
        }
        try:
            value, usage = await self.validated_call(
                route,
                participant="evidence-extractor-1",
                stage="evidence-extraction",
                role="Independent source-grounded evidence extractor",
                instructions=(
                    "Extract atomic claims only from the immutable evidence. Attach "
                    "source evidence IDs and the acceptance criterion IDs each claim "
                    "covers, identify source conflicts and gaps, and do not recommend "
                    "or synthesize a final answer."
                ),
                payload={
                    "task_contract": contract.model_dump(mode="json"),
                    "evidence_inventory": [
                        item.model_dump(mode="json") for item in self.inventory.refs
                    ],
                    "evidence": evidence,
                },
                contract=EvidenceExtraction,
                max_output_tokens=4500,
            )
            value.extractor_label = label
            valid_refs = {
                item.id
                for item in self.inventory.refs
                if item.kind in {"source", "pdf"}
            }
            valid_acceptance = {item.id for item in contract.acceptance_criteria}
            for claim in value.claims:
                claim.id = stable_claim_id(claim.text)
                claim.acceptance_ids = sorted(
                    set(claim.acceptance_ids) & valid_acceptance
                )
                claim.evidence_refs = sorted(set(claim.evidence_refs) & valid_refs)
            value.source_coverage = {
                ref: sorted(set(claim_ids))
                for ref, claim_ids in value.source_coverage.items()
                if ref in valid_refs
            }
            self.store.write_json("evidence-extraction.json", value)
            self.store.append_event("evidence_extraction", usage=usage)
            return value
        except BaseException as error:
            self.store.append_event(
                "evidence_extraction_failed",
                reason=self.guard.redact_text(str(error))[:500],
            )
            return None

    async def hypotheses(
        self,
        contract: TaskContract,
        evidence: str,
        *,
        routes: list[Route] | None = None,
        method: str = "independent",
        prior: list[Hypothesis] | None = None,
    ) -> list[Hypothesis]:
        active = routes or self.member_routes
        if method == "independent" and active == self.member_routes:
            primary_roles = {route.role for route in active}
            by_model = {
                item.model: item.label
                for item in self.assignments
                if item.role in primary_roles
            }
            labels = [
                by_model.get(route.model, f"Candidate {index + 1}")
                for index, route in enumerate(active)
            ]
        else:
            used = set(self.identity_map)
            candidates = shuffled_labels(self.run_id + method, 26)
            labels = [item for item in candidates if item not in used][: len(active)]
            if len(labels) < len(active):
                labels.extend(
                    f"Candidate X{index + 1}"
                    for index in range(len(active) - len(labels))
                )
        calls = {}
        for route, label in zip(active, labels):
            self.identity_map[label] = {
                "model": route.model,
                "family": route.family,
                "effort": route.effort,
                "role": route.role,
            }
            payload = {
                "task_contract": contract.model_dump(mode="json"),
                "evidence": evidence,
                "prior_structured_hypotheses": [
                    {
                        "label": item.label,
                        "recommendation_cluster": canonical(item.recommendation),
                        "claims": [
                            {
                                "id": claim.id,
                                "text": canonical(claim.text),
                                "evidence_refs": claim.evidence_refs,
                                "position": claim.position,
                                "blocker": claim.blocker,
                                "load_bearing": claim.load_bearing,
                                "testable": claim.testable,
                                "falsifiers": [
                                    canonical(value) for value in claim.falsifiers
                                ],
                            }
                            for claim in item.claims
                            if method != "red-team-attacks" or claim.load_bearing
                        ],
                    }
                    for item in (prior or [])
                ],
            }
            if method == "sample":
                payload["exclusion_contract"] = {
                    "different_decomposition": "required",
                    "different_evidence_class": "required unless a new tool is used",
                    "different_tool": "required unless a new failure assumption is used",
                    "different_failure_assumption": "required unless another dimension differs",
                }
            method_instruction = {
                "red-team-attacks": (
                    "Attack only load-bearing claims using quoted claim IDs, concrete "
                    "counterexamples, falsifiers, and evidence references."
                ),
                "defenses-and-updated-positions": (
                    "Defend attacked claims, concede falsified claims, and return an "
                    "updated position. Preserve unresolved minority claims."
                ),
                "sample": (
                    "Use a materially different decomposition, tool, assumptions, or "
                    "evidence source from every prior hypothesis."
                ),
                "risk-analysis": (
                    "Analyze the locally selected risk category, identify production "
                    "failure scenarios, and make every blocker falsifiable."
                ),
            }.get(
                method,
                "Produce a materially independent hypothesis without imitating peers.",
            )
            calls[label] = (
                route.family,
                self.validated_call(
                    route,
                    participant=f"{method}-{hashlib.sha1(label.encode()).hexdigest()[:6]}",
                    stage="hypotheses",
                    role="Independent anonymous hypothesis proposer",
                    instructions=(
                        "Produce a falsifiable structured hypothesis. State assumptions, "
                        "predicted observations, evidence references, load-bearing claims, "
                        "the acceptance criterion IDs covered by each claim, blockers, "
                        "risks, and a typed approach_signature with decomposition, "
                        "operations, constraints, assumptions, tools, evidence_classes, "
                        "intermediate_commitments, and answer_cluster. "
                        f"{method_instruction}"
                    ),
                    payload=payload,
                    contract=Hypothesis,
                    max_output_tokens=5500,
                ),
            )
        results, failures, cancelled = await gather_with_quorum(
            calls,
            grace_seconds=(
                self.request.quorum_grace
                if self.request.quorum_grace is not None
                else GRACE_SECONDS[self.budget_name]
            ),
            required_families=min(2, len(active)),
        )
        rows = []
        for route, label in zip(active, labels):
            if label in results:
                hypothesis, usage = results[label]
                hypothesis.blockers = clean_blockers(hypothesis.blockers)
                valid_acceptance = {item.id for item in contract.acceptance_criteria}
                for claim in hypothesis.claims:
                    claim.acceptance_ids = sorted(
                        set(claim.acceptance_ids) & valid_acceptance
                    )
                rows.append(normalize_hypothesis(hypothesis, label, method))
                self.store.write_json(f"hypotheses/{method}-{label}.json", rows[-1])
                self.store.append_event("hypothesis", label=label, usage=usage)
            else:
                reason = (
                    "cancelled after quorum grace"
                    if label in cancelled
                    else self.guard.redact_text(str(failures.get(label, "failed")))[
                        :500
                    ]
                )
                self.exclusions.append(
                    ExclusionRecord(
                        model=route.model,
                        family=route.family,
                        role=route.role,
                        reason=reason,
                    )
                )
        if (
            len(active) >= 2
            and len({self.identity_map[row.label]["family"] for row in rows}) < 2
        ):
            raise RuntimeError("below quorum: fewer than two completed families")
        return rows

    async def normalize_claims(
        self, rows: list[Hypothesis], round_name: str
    ) -> list[Hypothesis]:
        raw = {claim.id: claim.text for row in rows for claim in row.claims}
        if not raw or not self.luna_route or self.remaining_calls() < 4:
            return rows
        try:
            value, usage = await self.validated_call(
                self.luna_route,
                participant=f"claim-normalizer-{round_name}",
                stage="claim-normalization",
                role="Semantic claim canonicalizer",
                instructions=(
                    "Map semantically equivalent claims to identical concise canonical "
                    "text. Preserve distinct, opposite, or differently scoped claims."
                ),
                payload={
                    "claims": [
                        {"source_claim_id": key, "text": value}
                        for key, value in sorted(raw.items())
                    ]
                },
                contract=ClaimNormalization,
                max_output_tokens=4000,
            )
            aliases = {
                item.source_claim_id: item.canonical_text for item in value.aliases
            }
            if set(aliases) != set(raw):
                raise RuntimeError("normalizer did not cover every claim")
            for row in rows:
                for claim in row.claims:
                    claim.text = aliases[claim.id]
                    claim.id = stable_claim_id(claim.text)
            self.store.write_json(f"claim-normalization/{round_name}.json", value)
            self.store.append_event("claim_normalization", usage=usage)
        except BaseException as error:
            self.store.append_event(
                "claim_normalization_fallback",
                reason=self.guard.redact_text(str(error))[:500],
            )
        return rows

    def _claim_has_admissible_evidence(
        self, claim: Claim, contract: TaskContract
    ) -> bool:
        by_id = {item.id: item for item in self.inventory.refs}
        rows = [by_id[item] for item in claim.evidence_refs if item in by_id]
        if not rows:
            return False
        if contract.task_kind == "evidence_synthesis":
            return any(item.kind in {"source", "pdf"} for item in rows)
        return True

    async def model_verifications(
        self,
        plan: VerificationPlan,
        contract: TaskContract,
        ledger: ClaimLedger,
        evidence: str,
    ) -> list[VerificationReceipt]:
        receipts = []
        command_steps = [step for step in plan.steps if step.kind == "command"]
        cwd = Path(self.request.repo or ".").expanduser().resolve()
        for step in command_steps:
            receipt = run_command_verifier(step, cwd=cwd)
            receipts.append(receipt)
            self.store.write_json(f"verifications/{step.id}.json", receipt)
        calculation_steps = [step for step in plan.steps if step.kind == "calculation"]
        for step in calculation_steps:
            receipt = run_calculation_verifier(step)
            receipts.append(receipt)
            self.store.write_json(f"verifications/{step.id}.json", receipt)
        model_steps = [
            step for step in plan.steps if step.kind not in {"command", "calculation"}
        ]
        unresolved_steps = []
        for step in model_steps:
            deterministic = run_evidence_verifier(step, self.inventory.contents)
            if deterministic.status in {"supported", "falsified"}:
                receipts.append(deterministic)
                self.store.write_json(f"verifications/{step.id}.json", deterministic)
            else:
                unresolved_steps.append(step)
        if not unresolved_steps or self.remaining_calls() < 4:
            return receipts
        verifier_routes = []
        excluded: set[str] = set()
        for _ in range(2):
            route = self.route_for_role("verifier", contract, exclude_families=excluded)
            if route:
                verifier_routes.append(route)
                excluded.add(route.family)
        if len(verifier_routes) < 2:
            return receipts
        maximum_steps = max(0, (self.remaining_calls() - 2) // 2)
        for step in unresolved_steps[:maximum_steps]:
            candidates = []
            for index, route in enumerate(verifier_routes, 1):
                verifier_label = f"Verifier {step.id}-{index}"
                self.identity_map[verifier_label] = {
                    "model": route.model,
                    "family": route.family,
                    "effort": route.effort,
                    "role": "verifier",
                }
                try:
                    receipt, usage = await self.validated_call(
                        route,
                        participant=f"verifier-{step.id}-{index}",
                        stage="verification",
                        role="Independent claim verifier",
                        instructions=(
                            "Test only the named claim against the supplied immutable "
                            "evidence. Falsifying evidence wins over argument quality. "
                            "Return inconclusive when evidence cannot decide."
                        ),
                        payload={
                            "step": step.model_dump(mode="json"),
                            "task_contract": contract.model_dump(mode="json"),
                            "claim_ledger": ledger.model_dump(mode="json"),
                            "evidence": evidence,
                        },
                        contract=VerificationReceipt,
                        max_output_tokens=3500,
                    )
                    receipt.step_id = step.id
                    receipt.claim_id = step.claim_id
                    receipt.kind = step.kind
                    receipt.executor = f"anonymous-verifier-{index}"
                    receipt.expected_observation = step.expected_observation
                    receipt.falsifying_observation = step.falsifying_observation
                    receipt.resulting_stance = (
                        "support"
                        if receipt.status == "supported"
                        else "oppose"
                        if receipt.status == "falsified"
                        else "uncertain"
                    )
                    receipt.output_sha256 = sha256_text(receipt.observation)
                    candidates.append(receipt)
                    self.store.write_json(
                        f"verifications/{step.id}-vote-{index}.json",
                        receipt,
                    )
                    self.store.append_event(
                        "verification_vote",
                        step=step.id,
                        index=index,
                        usage=usage,
                    )
                except BaseException as error:
                    self.store.append_event(
                        "verification_failed",
                        step=step.id,
                        index=index,
                        error=self.guard.redact_text(str(error))[:500],
                    )
            statuses = {item.status for item in candidates}
            status = (
                candidates[0].status
                if len(candidates) == 2
                and len(statuses) == 1
                and candidates[0].status in {"supported", "falsified"}
                else "conflicting" if len(statuses) > 1 else "inconclusive"
            )
            evidence_kinds = {
                item.kind
                for item in self.inventory.refs
                if item.id in step.evidence_refs
            }
            if step.kind in {
                "counterexample",
                "invariant",
            } and not evidence_kinds.intersection({"command", "source", "pdf", "git"}):
                status = "inconclusive"
            observation = (
                "\n\n".join(
                    f"Verifier {index}: {item.observation}"
                    for index, item in enumerate(candidates, 1)
                )
                or "fewer than two independent verifier receipts"
            )
            consensus = VerificationReceipt(
                id=step.id,
                step_id=step.id,
                claim_id=step.claim_id,
                kind=step.kind,
                status=status,
                executor="cross-family-verifier-consensus",
                output_sha256=sha256_text(observation),
                observation=observation,
                evidence_refs=sorted(
                    {ref for item in candidates for ref in item.evidence_refs}
                ),
                expected_observation=step.expected_observation,
                falsifying_observation=step.falsifying_observation,
                resulting_stance=(
                    "support"
                    if status == "supported"
                    else "oppose"
                    if status == "falsified"
                    else "uncertain"
                ),
                verifier_route="+".join(route.model for route in verifier_routes),
                verifier_family="+".join(route.family for route in verifier_routes),
                independent=len({route.family for route in verifier_routes}) == 2,
                repeated=True,
                decomposed=True,
                ambiguity=(
                    "cross-family verifier disagreement"
                    if status == "conflicting" else None
                ),
            )
            receipts.append(consensus)
            self.store.write_json(f"verifications/{step.id}.json", consensus)
            self.store.append_event(
                "verification_consensus", step=step.id, status=status
            )
        return receipts

    async def minority_defense(
        self,
        claim: Claim,
        contract: TaskContract,
        evidence: str,
        majority_labels: list[str],
    ) -> MinorityDefense | None:
        if self.remaining_calls() < 3:
            return None
        route = self.route_for_role("minority_advocate", contract)
        if not route:
            return None
        value, usage = await self.validated_call(
            route,
            participant="minority-advocate",
            stage="minority-defense",
            role="Independent minority hypothesis advocate",
            instructions=(
                "State the strongest falsifiable case for this minority claim and "
                "attempt to falsify it. Also state the concrete evidence the majority "
                "must accept as disconfirming its own position. Preserve the minority "
                "as unresolved unless evidence actually defeats it."
            ),
            payload={
                "task_contract": contract.model_dump(mode="json"),
                "claim": claim.model_dump(mode="json"),
                "evidence": evidence,
            },
            contract=MinorityDefense,
            max_output_tokens=3500,
        )
        value.claim_id = claim.id
        value.advocate_label = "Minority Advocate"
        self.identity_map[value.advocate_label] = {
            "model": route.model,
            "family": route.family,
            "effort": route.effort,
            "role": "minority_advocate",
        }
        if majority_labels and self.remaining_calls() >= 2:
            identity = self.identity_map.get(majority_labels[0], {})
            capability = next(
                (
                    item
                    for item in self.catalogue
                    if item.id == identity.get("model") and "critic" in item.roles
                ),
                None,
            )
            if capability:
                majority_route = Route(
                    capability,
                    identity.get("effort", "medium"),
                    "critic",
                )
                challenge, challenge_usage = await self.validated_call(
                    majority_route,
                    participant=f"majority-self-challenge-{claim.id}",
                    stage="majority-self-challenge",
                    role="Majority position self-challenger",
                    instructions=(
                        "State the concrete falsifying observation or evidence that "
                        "would make the majority abandon this position."
                    ),
                    payload={
                        "claim": claim.model_dump(mode="json"),
                        "majority_labels": majority_labels,
                        "task_contract": contract.model_dump(mode="json"),
                    },
                    contract=MajoritySelfChallenge,
                    max_output_tokens=2500,
                )
                challenge.claim_id = claim.id
                challenge.majority_labels = majority_labels
                value.majority_disconfirmation_condition = (
                    challenge.disconfirmation_condition
                )
                self.store.write_json(
                    f"minority/{claim.id}-majority-self-challenge.json",
                    challenge,
                )
                self.store.append_event(
                    "majority_self_challenge", claim_id=claim.id, usage=challenge_usage
                )
        self.store.write_json(f"minority/{claim.id}.json", value)
        self.store.append_event("minority_defense", claim_id=claim.id, usage=usage)
        return value

    def apply_receipts(
        self, ledger: ClaimLedger, receipts: list[VerificationReceipt]
    ) -> None:
        by_claim = {item.claim.id: item for item in ledger.entries}
        for receipt in receipts:
            entry = by_claim.get(receipt.claim_id)
            if entry:
                entry.verification_status = receipt.status
                if receipt.status == "supported":
                    entry.unresolved = False
                elif receipt.status == "falsified":
                    entry.unresolved = False
                    if entry.claim.load_bearing:
                        ledger.blockers.append(
                            f"falsified load-bearing claim {entry.claim.id}"
                        )
                else:
                    entry.unresolved = True
        ledger.load_bearing_unresolved = sorted(
            item.claim.id
            for item in ledger.entries
            if item.claim.load_bearing and item.unresolved
        )
        ledger.blockers = sorted(set(ledger.blockers))

    def blind_payload(
        self,
        rows: list[Hypothesis],
        ledger: ClaimLedger,
        order: list[str],
        receipts: list[VerificationReceipt],
        minority: list[MinorityDefense],
    ) -> dict[str, Any]:
        by_label = {item.label: item for item in rows}
        return {
            "hypotheses": [
                {
                    "label": label,
                    "recommendation_cluster": canonical(by_label[label].recommendation),
                    "claims": [
                        {
                            **claim.model_dump(
                                mode="json",
                                exclude={"assumptions", "falsifiers"},
                            ),
                            "assumptions": sorted(
                                canonical(item) for item in claim.assumptions
                            ),
                            "falsifiers": sorted(
                                canonical(item) for item in claim.falsifiers
                            ),
                        }
                        for claim in by_label[label].claims
                    ],
                    "assumptions": sorted(
                        canonical(item) for item in by_label[label].assumptions
                    ),
                    "predicted_observations": sorted(
                        canonical(item)
                        for item in by_label[label].predicted_observations
                    ),
                    "risks": sorted(canonical(item) for item in by_label[label].risks),
                    "blockers": sorted(
                        canonical(item) for item in by_label[label].blockers
                    ),
                }
                for label in order
                if label in by_label
            ],
            "claim_ledger": ledger.model_dump(mode="json"),
            "verification_receipts": [
                item.model_dump(mode="json") for item in receipts
            ],
            "minority_defenses": [item.model_dump(mode="json") for item in minority],
        }

    async def ballot(
        self,
        contract: TaskContract,
        evidence: str,
        rows: list[Hypothesis],
        ledger: ClaimLedger,
        receipts: list[VerificationReceipt],
        minority: list[MinorityDefense],
        order: list[str],
        suffix: str,
        route: Route | None = None,
    ) -> JudgmentBallot:
        active = route or self.judge_route
        assert active is not None
        ballot, usage = await self.validated_call(
            active,
            participant=f"judge-{suffix}",
            stage="judging",
            role="Fresh blind criterion judge",
            instructions=(
                "Evaluate acceptance criteria independently. Verification receipts "
                "outrank rhetoric. Preserve surviving minority claims. For objective "
                "tasks choose verified selection; subjective tasks rank criteria; "
                "safety tasks block on any substantiated blocker."
            ),
            payload={
                "task_contract": contract.model_dump(mode="json"),
                "evidence_inventory": [
                    ref.model_dump(mode="json") for ref in self.inventory.refs
                ],
                "evidence": evidence,
                "anonymous_council": self.blind_payload(
                    rows, ledger, order, receipts, minority
                ),
            },
            contract=JudgmentBallot,
            max_output_tokens=6500,
        )
        ballot.order = order
        ballot.blockers = clean_blockers(ballot.blockers)
        self.store.write_json(f"judging/ballot-{suffix}.json", ballot)
        self.store.append_event("judgment_ballot", suffix=suffix, usage=usage)
        return ballot

    async def mirrored_judgment(
        self,
        contract: TaskContract,
        evidence: str,
        rows: list[Hypothesis],
        ledger: ClaimLedger,
        receipts: list[VerificationReceipt],
        minority: list[MinorityDefense],
    ) -> tuple[JudgmentBallot, JudgmentAssessment]:
        labels = [item.label for item in rows]
        first_order = deterministic_order(self.run_id, labels, "first")
        second_order = list(reversed(first_order))
        first = await self.ballot(
            contract, evidence, rows, ledger, receipts, minority, first_order, "first"
        )
        second = await self.ballot(
            contract,
            evidence,
            rows,
            ledger,
            receipts,
            minority,
            second_order,
            "reversed",
        )
        consistency = judgment_assessment(
            first,
            second,
            {item.claim.id for item in ledger.entries if item.claim.load_bearing},
        )
        final = first
        ballots = [first, second]
        if consistency.tiebreaker_required and self.remaining_calls() >= 2:
            alternate = self.route_for_role(
                "judge", contract, exclude_families={self.judge_route.family}
            )
            if alternate:
                tie = await self.ballot(
                    contract,
                    evidence,
                    rows,
                    ledger,
                    receipts,
                    minority,
                    deterministic_order(self.run_id, labels, "tie"),
                    "tiebreaker",
                    route=alternate,
                )
                ballots.append(tie)
                first_key = (first.action, first.selected_candidate)
                second_key = (second.action, second.selected_candidate)
                tie_key = (tie.action, tie.selected_candidate)
                if tie_key == second_key and tie_key != first_key:
                    final = second
                    consistency.consistent = True
                    consistency.reasons.append(
                        "cross-family tiebreaker converged on reversed ballot"
                    )
                elif tie_key == first_key:
                    final = first
                    consistency.consistent = True
                    consistency.reasons.append(
                        "cross-family tiebreaker converged on first ballot"
                    )
                else:
                    consistency.reasons.append(
                        "cross-family tiebreaker did not converge"
                    )
                    consistency.consistent = False
        while not consistency.consistent and len(ballots) < 6 and self.remaining_calls() >= 2:
            candidates = sorted({
                score.candidate_label for item in ballots for score in item.scores
            })
            pair = next_active_comparison(
                candidates,
                {
                    tuple(sorted((left, right))): consistency.preference_entropy
                    for left, right in combinations(candidates, 2)
                },
                {candidate: consistency.preference_entropy for candidate in candidates},
            )
            if not pair:
                break
            alternate = self.route_for_role(
                "judge", contract, exclude_families={self.judge_route.family}
            )
            if not alternate:
                break
            repeated = await self.ballot(
                contract,
                evidence,
                [item for item in rows if item.label in set(pair)],
                ledger,
                receipts,
                minority,
                list(pair) if len(ballots) % 2 == 0 else list(reversed(pair)),
                f"adaptive-{len(ballots) + 1}",
                route=alternate,
            )
            ballots.append(repeated)
            consistency = judgment_assessment(
                ballots[-2],
                ballots[-1],
                {item.claim.id for item in ledger.entries if item.claim.load_bearing},
            )
            consistency.evaluations = len(ballots)
            consistency.bias_audit = audit_bias(
                [item.model_dump(mode="json") for item in ballots]
            )
        if consistency.close_pair and self.remaining_calls() >= 2 and len(ballots) < 6:
            alternate = self.route_for_role(
                "judge", contract, exclude_families={self.judge_route.family}
            )
            if alternate:
                pair_order = deterministic_order(
                    self.run_id, consistency.close_pair, "close-pair"
                )
                pair = await self.ballot(
                    contract,
                    evidence,
                    [item for item in rows if item.label in set(pair_order)],
                    ledger,
                    receipts,
                    minority,
                    pair_order,
                    "pairwise",
                    route=alternate,
                )
                ballots.append(pair)
                final = pair
        aggregation = aggregate_ballots(
            contract.task_kind,
            ballots,
            ledger,
            {item.claim_id for item in receipts if item.status == "supported"},
            participant_reliability=(
                {
                    str(index): self.reliability_store.score(
                        self.reliability,
                        model=self.judge_route.model,
                        family=self.judge_route.family,
                        role="judge",
                        task_kind=contract.task_kind,
                        domain=self.task_domain(contract),
                    )[0]
                    for index, _ in enumerate(ballots)
                }
                if not any(item.status == "supported" for item in receipts)
                else None
            ),
        )
        self.store.write_json("judging/aggregation.json", aggregation)
        if aggregation.selected_candidate:
            final.selected_candidate = aggregation.selected_candidate
            if final.action not in {"select", "integrate"}:
                final.action = "select"
        if aggregation.accepted_claim_ids:
            final.accepted_claim_ids = aggregation.accepted_claim_ids
        final.blockers = sorted(set(final.blockers + aggregation.blockers))
        self.store.write_json("judging/consistency.json", consistency)
        return final, consistency

    def ballot_verdict(
        self,
        ballot: JudgmentBallot,
        consistency: JudgmentAssessment,
        ledger: ClaimLedger,
        contract: TaskContract,
        confidence: float,
    ) -> Verdict:
        blockers = sorted(set(ballot.blockers + ledger.blockers))
        if not consistency.consistent:
            blockers.append("mirrored judge inconsistency")
        action = ballot.action
        decision = (
            "BLOCKED: judgment did not converge"
            if blockers and contract.task_kind == "safety_gate"
            else "; ".join(ballot.rationale) or action
        )
        accepted = set(ballot.accepted_claim_ids)
        evidence_refs = {
            ref
            for item in ledger.entries
            if not accepted or item.claim.id in accepted
            for ref in item.claim.evidence_refs
        }
        evidence_refs.update(
            ref for score in ballot.scores for ref in score.evidence_refs
        )
        if not evidence_refs and self.inventory.refs:
            evidence_refs.add(self.inventory.refs[0].id)
        return Verdict(
            decision=decision,
            rationale=ballot.rationale,
            dissent=[
                item.claim.text
                for item in ledger.entries
                if item.unresolved or item.claim.id in ledger.load_bearing_unresolved
            ],
            confidence=confidence,
            blockers=sorted(set(blockers)),
            action=action,
            selected_candidate=ballot.selected_candidate,
            acceptance_reasons={
                score.criterion_id: score.reason for score in ballot.scores
            },
            majority=ballot.accepted_claim_ids,
            minority=[
                item.claim.id
                for item in ledger.entries
                if len(item.supporting_labels) == 1
            ],
            unresolved=ledger.load_bearing_unresolved,
            evidence_refs=sorted(evidence_refs),
        )

    async def validate_high_risk(
        self,
        contract: TaskContract,
        evidence: str,
        ledger: ClaimLedger,
        verdict: Verdict,
    ) -> tuple[list[ValidationReceipt], bool, Verdict]:
        validators = []
        excluded = {self.judge_route.family}
        for _ in range(2):
            route = self.route_for_role(
                "validator", contract, exclude_families=excluded
            )
            if route:
                validators.append(route)
                excluded.add(route.family)
        if len(validators) < 2:
            return [], False, verdict
        receipts = []
        revised = False
        index = 0
        while index < 2:
            route = validators[index]
            self.identity_map[f"Validator {index + 1}"] = {
                "model": route.model,
                "family": route.family,
                "effort": route.effort,
                "role": "validator",
            }
            value, usage = await self.validated_call(
                route,
                participant=f"validator-{index + 1}{'-revised' if revised else ''}",
                stage="validation",
                role="Independent non-judge safety validator",
                instructions=(
                    "Return blocker_free only when every load-bearing conclusion is "
                    "supported by evidence and no material safety blocker remains."
                ),
                payload={
                    "task_contract": contract.model_dump(mode="json"),
                    "evidence": evidence,
                    "claim_ledger": ledger.model_dump(mode="json"),
                    "verdict": verdict.model_dump(mode="json"),
                },
                contract=ValidationReceipt,
                max_output_tokens=4000,
            )
            value.label = f"Validator {index + 1}"
            value.family = route.family
            value.verdict_sha256 = sha256_text(
                json.dumps(verdict.model_dump(mode="json"), sort_keys=True)
            )
            receipts.append(value)
            suffix = f"{index + 1}{'-revised' if revised else ''}"
            self.store.write_json(f"validations/validator-{suffix}.json", value)
            self.store.append_event("validation", index=index + 1, usage=usage)
            if value.status != "blocker_free":
                if revised or self.remaining_calls() < 4:
                    return receipts, False, verdict
                revision, revision_usage = await self.validated_call(
                    self.judge_route,
                    participant="judge-revision",
                    stage="judge-revision",
                    role="Safety verdict reviser",
                    instructions=(
                        "Revise the verdict once to address the validator blocker. "
                        "Do not remove a blocker without evidence. Preserve dissent."
                    ),
                    payload={
                        "task_contract": contract.model_dump(mode="json"),
                        "claim_ledger": ledger.model_dump(mode="json"),
                        "prior_verdict": verdict.model_dump(mode="json"),
                        "blocking_validation": value.model_dump(mode="json"),
                    },
                    contract=Verdict,
                    max_output_tokens=5000,
                )
                verdict = revision
                revised = True
                receipts = []
                index = 0
                self.store.write_json("judging/revised-verdict.json", verdict)
                self.store.append_event("judge_revision", usage=revision_usage)
                continue
            index += 1
        return (
            receipts,
            len(receipts) == 2
            and all(item.status == "blocker_free" for item in receipts),
            verdict,
        )

    def make_revisit_report(self, ledger: ClaimLedger) -> None:
        if not self.request.parent_run_id:
            return
        try:
            parent = RunStore.open_existing(
                self.store.root, self.request.parent_run_id, self.guard
            )
            prior = ClaimLedger.model_validate(parent.read_json("claim-ledger.json"))
        except (RuntimeError, OSError, ValidationError):
            return
        before = {item.claim.id: item for item in prior.entries}
        after = {item.claim.id: item for item in ledger.entries}
        rows = []
        for claim_id in sorted(set(before) | set(after)):
            previous = before.get(claim_id)
            current = after.get(claim_id)
            stayed = bool(
                previous
                and current
                and previous.model_dump(mode="json") == current.model_dump(mode="json")
            )
            rows.append(
                RevisitClaim(
                    claim_id=claim_id,
                    status="stayed" if stayed else "changed",
                    reason=(
                        "Claim evidence, stance, and verification state are unchanged."
                        if stayed
                        else "Claim evidence, stance, presence, or verification changed."
                    ),
                )
            )
        self.store.write_json(
            "revisit-report.json",
            RevisitReport(parent_run_id=self.request.parent_run_id, claims=rows),
        )

    async def run(self) -> ProtocolResult:
        snapshot_mode = (
            self.request.mode
            if self.request.mode
            in {"decide", "review", "red-team", "implement", "revisit"}
            else "decide"
        )
        self.store.write_json(
            "snapshot.json",
            SanitizedSnapshot(
                mode=snapshot_mode,  # type: ignore[arg-type]
                budget_requested=self.request.budget_requested,  # type: ignore[arg-type]
                prompt=self.request.prompt,
                contexts=[
                    {"source": source, "content": content}
                    for source, content in self.request.contexts
                ],
                sources=self.request.sources,
                verify_commands=self.request.verify_commands,
                repo=self.request.repo,
                base_commit=self.request.base_commit,
                review_target=self.request.review_target,
            ),
        )
        self.store.append_event(
            "started", budget=self.budget_name, reasons=self.budget_reasons
        )
        self.persist_manifest()
        try:
            if self.request.sources:
                await snapshot_sources(self.request.sources, self.inventory, self.store)
            self.inventory.snapshot(self.store)
            routes, judge, _ = await self.preflight()
            contract = await self.task_contract()
            routes = self.reroute_primary_role(contract, "proposer")
            self.manifest.task_kind = contract.task_kind
            self.store.write_json("task-contract.json", contract)
            rubric = lock_rubric(
                [item.model_dump(mode="json") for item in contract.acceptance_criteria],
                ["load-bearing claims require deterministic or independent evidence"],
                "task-specific higher-order aggregation; never raw majority",
                contract.task_kind,
            )
            reporting_rules = default_reporting_rules()
            self.store.write_json("rubric.json", rubric)
            self.store.write_json("reporting-rules.json", reporting_rules)
            self.manifest.rubric_sha256 = rubric["sha256"]
            self.manifest.reporting_rules_sha256 = digest(
                reporting_rules.model_dump(mode="json")
            )
            self.manifest.task_contract_sha256 = sha256_text(
                json.dumps(contract.model_dump(mode="json"), sort_keys=True)
            )
            reliability_payload = self.reliability.model_dump(mode="json")
            self.store.write_json("reliability-snapshot.json", reliability_payload)
            self.manifest.reliability_snapshot_sha256 = sha256_text(
                json.dumps(reliability_payload, sort_keys=True)
            )
            evidence = await self.packed_evidence(routes + [judge])
            source_evidence = await self.packed_evidence(
                routes + [judge], include_retrieved_sources=True
            )
            extraction = await self.extract_evidence(contract, source_evidence)
            if extraction:
                evidence += "\n\nSTRUCTURED SOURCE EXTRACTION\n" + json.dumps(
                    extraction.model_dump(mode="json"),
                    sort_keys=True,
                    ensure_ascii=False,
                )
            hypotheses = await self.hypotheses(contract, evidence)
            hypotheses = await self.normalize_claims(hypotheses, "initial")
            ledger = build_claim_ledger(hypotheses, [], contract)
            profile = self.persist_approach_profile(hypotheses)
            if self.request.mode == "red-team":
                attacks = await self.hypotheses(
                    contract,
                    evidence,
                    routes=self.member_routes,
                    method="red-team-attacks",
                    prior=hypotheses,
                )
                attacks = await self.normalize_claims(attacks, "red-team-attacks")
                updated = await self.hypotheses(
                    contract,
                    evidence,
                    routes=self.member_routes,
                    method="defenses-and-updated-positions",
                    prior=hypotheses + attacks,
                )
                updated = await self.normalize_claims(
                    updated, "defenses-and-updated-positions"
                )
                hypotheses = hypotheses + attacks + updated
                ledger = build_claim_ledger(hypotheses, [], contract)
                profile = self.persist_approach_profile(hypotheses)
            initial_reported = sum(
                claim.reported_confidence for row in hypotheses for claim in row.claims
            ) / max(1, sum(len(row.claims) for row in hypotheses))
            role_score, role_n, _ = self.reliability_store.score(
                self.reliability,
                model=judge.model,
                family=judge.family,
                role="judge",
                task_kind=contract.task_kind,
                domain=self.task_domain(contract),
            )
            initial_confidence = min(
                initial_reported, role_score if role_n >= 8 else initial_reported
            )
            receipts: list[VerificationReceipt] = []
            if self.request.verify_commands:
                command_plan = build_verification_plan(
                    [item.claim for item in ledger.entries],
                    self.request.verify_commands,
                    id_namespace="user-authorized",
                )
                command_plan.steps = [
                    step for step in command_plan.steps if step.kind == "command"
                ]
                for step in command_plan.steps:
                    if not step.evidence_refs and self.inventory.refs:
                        step.evidence_refs = [self.inventory.refs[0].id]
                self.store.write_json("verification-plan-user.json", command_plan)
                command_receipts = await self.model_verifications(
                    command_plan, contract, ledger, evidence
                )
                receipts.extend(command_receipts)
                self.apply_receipts(ledger, command_receipts)
            minority: list[MinorityDefense] = []
            operation_cap = (
                0
                if self.request.mode == "red-team"
                else OPERATION_CAPS[self.budget_name]
            )
            for index in range(1, operation_cap + 1):
                break_after_operation = False
                evidence_completeness = sum(
                    bool(value) for value in ledger.acceptance_coverage.values()
                ) / max(1, len(ledger.acceptance_coverage))
                evidence_completeness *= sum(
                    self._claim_has_admissible_evidence(item.claim, contract)
                    for item in ledger.entries
                    if item.claim.load_bearing
                ) / max(
                    1,
                    sum(item.claim.load_bearing for item in ledger.entries),
                )
                healthy_models = {
                    item.model for item in self.health if item.status == "healthy"
                }
                verifier_available = any(
                    item.id in healthy_models and "verifier" in item.roles
                    for item in self.catalogue
                )
                decision = choose_operation(
                    index=index,
                    contract=contract,
                    profile=profile,
                    ledger=ledger,
                    calibrated_confidence=initial_confidence,
                    remaining_calls=self.remaining_calls(),
                    mandatory_calls=(4 if contract.risk_level == "high" else 2),
                    evidence_completeness=evidence_completeness,
                    route_reliability=role_score,
                    verifier_available=verifier_available,
                    learned_effects=self.learned_operation_effects(contract),
                )
                self.store.write_json(f"policy/decision-{index}.json", decision)
                self.manifest.operations_used += 1
                if decision.operation == "blocked_escalation":
                    ledger.blockers.append(
                        "deliberation policy escalated unresolved material state"
                    )
                    ledger.blockers = sorted(set(ledger.blockers))
                    self.manifest.stopped_reason = decision.reasons[0]
                    break
                if decision.operation in {
                    "stop",
                    "direct_judgment",
                    "higher_order_aggregate",
                    "pairwise_compare",
                    "ranked_pairs",
                }:
                    self.manifest.stopped_reason = decision.reasons[0]
                    break
                if decision.operation == "verify":
                    plan = build_verification_plan(
                        [item.claim for item in ledger.entries if item.unresolved],
                        [],
                        [item.verification for item in contract.acceptance_criteria],
                        id_namespace=f"operation-{index}",
                    )
                    self.store.write_json("verification-plan.json", plan)
                    new_receipts = await self.model_verifications(
                        plan, contract, ledger, evidence
                    )
                    receipts.extend(new_receipts)
                    self.apply_receipts(ledger, new_receipts)
                elif decision.operation == "minority_defense":
                    claim_id = next(
                        (
                            item.claim.id
                            for item in ledger.entries
                            if item.claim.load_bearing
                            and item.unresolved
                            and len(item.supporting_labels) == 1
                        ),
                        None,
                    )
                    if claim_id:
                        claim = next(
                            item.claim
                            for item in ledger.entries
                            if item.claim.id == claim_id
                        )
                        entry = next(
                            item for item in ledger.entries if item.claim.id == claim_id
                        )
                        defense = await self.minority_defense(
                            claim,
                            contract,
                            evidence,
                            sorted(
                                {item.label for item in hypotheses}
                                - set(entry.supporting_labels)
                            ),
                        )
                        if defense:
                            minority.append(defense)
                            if defense.status == "falsified":
                                next(
                                    item
                                    for item in ledger.entries
                                    if item.claim.id == claim_id
                                ).verification_status = "falsified"
                            else:
                                entry = next(
                                    item
                                    for item in ledger.entries
                                    if item.claim.id == claim_id
                                )
                                entry.unresolved = True
                                if entry.claim.load_bearing:
                                    ledger.load_bearing_unresolved = sorted(
                                        set(ledger.load_bearing_unresolved + [claim_id])
                                    )
                                plan = build_verification_plan(
                                    [claim],
                                    [],
                                    id_namespace=f"minority-{claim.id}",
                                )
                                if plan.steps:
                                    defense.verification_step_id = plan.steps[0].id
                                    self.store.write_json(
                                        "verification-plan-minority.json", plan
                                    )
                                    new_receipts = await self.model_verifications(
                                        plan, contract, ledger, evidence
                                    )
                                    receipts.extend(new_receipts)
                                    self.apply_receipts(ledger, new_receipts)
                                    self.store.write_json(
                                        f"minority/{claim.id}.json", defense
                                    )
                elif decision.operation == "sample":
                    if self.alternate_routes:
                        new_rows = await self.hypotheses(
                            contract,
                            evidence,
                            routes=[self.alternate_routes.pop(0)],
                            method="sample",
                            prior=hypotheses,
                        )
                        hypotheses.extend(
                            await self.normalize_claims(new_rows, "sample")
                        )
                        ledger = build_claim_ledger(hypotheses, [], contract)
                    else:
                        ledger.blockers.append(
                            "hypothesis generation failure: no independent alternate route"
                        )
                        ledger.blockers = sorted(set(ledger.blockers))
                        break
                elif decision.operation == "safety_validate":
                    risk_route = self.route_for_role("risk_analyst", contract)
                    if risk_route:
                        risk_rows = await self.hypotheses(
                            contract,
                            evidence,
                            routes=[risk_route],
                            method="risk-analysis",
                            prior=hypotheses,
                        )
                        hypotheses.extend(
                            await self.normalize_claims(risk_rows, "risk-analysis")
                        )
                        ledger = build_claim_ledger(hypotheses, [], contract)
                    break_after_operation = True
                elif decision.operation in {
                    "targeted_rebuttal",
                    "synthesize",
                }:
                    critic_routes = []
                    excluded: set[str] = set()
                    for _ in range(min(2, len(self.member_routes))):
                        critic = self.route_for_role(
                            "critic", contract, exclude_families=excluded
                        )
                        if critic:
                            critic_routes.append(critic)
                            excluded.add(critic.family)
                    critics = await self.hypotheses(
                        contract,
                        evidence,
                        routes=critic_routes or self.member_routes[:2],
                        method=decision.operation,
                        prior=hypotheses,
                    )
                    hypotheses.extend(
                        await self.normalize_claims(critics, decision.operation)
                    )
                    ledger = build_claim_ledger(hypotheses, [], contract)
                profile = self.persist_approach_profile(hypotheses)
                if break_after_operation:
                    self.manifest.stopped_reason = (
                        "risk analysis completed; proceed to conservative validation"
                    )
                    break
            self.store.write_json("claim-ledger.json", ledger)
            await self.maybe_run_route_anchor(
                4 if contract.risk_level == "high" else 2
            )
            ballot, consistency = await self.mirrored_judgment(
                contract, evidence, hypotheses, ledger, receipts, minority
            )
            high_risk = contract.risk_level == "high"
            evidence_coverage = sum(
                bool(value) for value in ledger.acceptance_coverage.values()
            ) / max(1, len(ledger.acceptance_coverage))
            evidence_coverage *= sum(
                self._claim_has_admissible_evidence(item.claim, contract)
                for item in ledger.entries
                if item.claim.load_bearing
            ) / max(
                1,
                sum(item.claim.load_bearing for item in ledger.entries),
            )
            structurally_supported = {
                item.claim.id
                for item in ledger.entries
                if item.supporting_labels
                and self._claim_has_admissible_evidence(item.claim, contract)
                and not item.opposing_labels
            }
            verified_ids = {
                item.claim_id for item in receipts if item.status == "supported"
            }
            unresolved_load_bearing = [
                item.claim.id
                for item in ledger.entries
                if item.claim.load_bearing
                and item.claim.id not in verified_ids | structurally_supported
            ]
            ledger.load_bearing_unresolved = sorted(
                set(ledger.load_bearing_unresolved) | set(unresolved_load_bearing)
            )
            diagnosis = diagnose_failure_mode(
                ledger=ledger,
                selected_candidate=ballot.selected_candidate,
                accepted_claim_ids=set(ballot.accepted_claim_ids),
                verified_claim_ids=verified_ids,
                structurally_supported_claim_ids=structurally_supported,
            )
            if diagnosis.state == "aggregation_discarded_supported_hypothesis":
                ballot.accepted_claim_ids = sorted(
                    set(ballot.accepted_claim_ids)
                    | set(diagnosis.aggregation_discarded_claim_ids)
                )
            elif diagnosis.state == "selected_hypothesis_unverified":
                ledger.blockers.append(
                    "selected hypothesis contains unverified load-bearing claims"
                )
            elif diagnosis.state == "likely_generation_failure":
                ledger.blockers.append(
                    "no independently supported load-bearing hypothesis was generated"
                )
            self.store.write_json("failure-diagnosis.json", diagnosis)
            if evidence_coverage < 1:
                ledger.blockers.append("acceptance criteria coverage incomplete")
            if unresolved_load_bearing:
                ledger.blockers.append(
                    "unresolved load-bearing claims: "
                    + ", ".join(sorted(unresolved_load_bearing))
                )
            ledger.blockers = sorted(set(ledger.blockers))
            provisional = Verdict(
                decision="; ".join(ballot.rationale) or ballot.action,
                confidence=ballot.reported_confidence,
                blockers=ballot.blockers,
                action=ballot.action,
            )
            validations: list[ValidationReceipt] = []
            validation_complete = not high_risk
            if high_risk:
                (
                    validations,
                    validation_complete,
                    provisional,
                ) = await self.validate_high_risk(
                    contract, evidence, ledger, provisional
                )
                if provisional.action:
                    ballot.action = provisional.action
                if provisional.rationale:
                    ballot.rationale = provisional.rationale
                ballot.blockers = sorted(set(ballot.blockers + provisional.blockers))
            calibration_path = self.state / "v4" / "calibration.json"
            calibration_rows = json.loads(calibration_path.read_text()).get("examples", [])
            score_rows = [
                (float(item["score"]), bool(item["correct"]))
                for item in calibration_rows
                if item.get("task_kind") == contract.task_kind
                and item.get("domain") == self.task_domain(contract)
                and item.get("route_epoch") == self.manifest.route_epoch
            ]
            route_ids = [route.model for route in self.member_routes]
            families = [route.family for route in self.member_routes]
            cofailure = self.cofailure_store.select_profile(
                routes=route_ids,
                families=families,
                task_kind=contract.task_kind,
                domain=self.task_domain(contract),
                answer_format=contract.task_kind,
            )
            self._cofailure_profiles = [cofailure]
            cofailure_penalty = (
                cofailure.interval_high
                if cofailure.active and cofailure.interval_high is not None
                else 0.25
            )
            verifier_reliability = (
                sum(item.status == "supported" for item in receipts) / len(receipts)
                if receipts else 0.5
            )
            lineage_penalty = len(unresolved_load_bearing) / max(
                1, sum(item.claim.load_bearing for item in ledger.entries)
            )
            confidence_score = max(
                0.0,
                min(
                    1.0,
                    ballot.reported_confidence
                    * evidence_coverage
                    * (1 - cofailure_penalty)
                    * verifier_reliability
                    * (1 - lineage_penalty),
                ),
            )
            deterministic_resolved = bool(unresolved_load_bearing) is False and bool(
                receipts
            ) and all(
                item.deterministic and item.status == "supported"
                for item in receipts
                if any(
                    row.claim.id == item.claim_id and row.claim.load_bearing
                    for row in ledger.entries
                )
            )
            independent_resolved = bool(unresolved_load_bearing) is False and bool(
                receipts
            ) and all(
                proposer_verifier_independent(item, None, 0)
                and item.status == "supported"
                for item in receipts
                if any(
                    row.claim.id == item.claim_id and row.claim.load_bearing
                    for row in ledger.entries
                )
            )
            selective = selective_judgment(
                scores_and_correct=score_rows,
                score=confidence_score,
                judgment_risk=self.request.judgment_risk,
                high_risk=high_risk,
                implementation=contract.task_kind == "implementation",
                deterministic=deterministic_resolved,
                independently_verified=independent_resolved,
            )
            self.store.write_json("selective-judgment.json", selective)
            self.store.write_json("uncertainty-profile.json", uncertainty_profile(
                cross_approach=profile.approach_distance,
                within_route=0.0,
                judge_variance=consistency.preference_entropy,
                representation=profile.metric_disagreement,
                context_loss=min(1.0, len(self._context_drops) / max(1, len(self.inventory.refs))),
            ))
            verdict = self.ballot_verdict(
                ballot, consistency, ledger, contract, selective.confidence_high
            )
            verdict.failure_diagnosis = diagnosis
            if high_risk and not validation_complete:
                verdict.blockers.append("independent high-risk validation incomplete")
                verdict.action = "block"
                verdict.decision = "BLOCKED: " + verdict.decision
            verdict.judgment_risk = validate_judgment_risk(
                self.request.judgment_risk, high_risk=high_risk
            )
            verdict.calibrated = selective.calibrated
            verdict.abstained = selective.abstained or bool(
                high_risk and not validation_complete
            )
            verdict.finality = "abort" if verdict.blockers or verdict.abstained else "verdict_commit"
            verdict.cofailure = cofailure.model_dump(mode="json")
            self.manifest.judgment_risk = verdict.judgment_risk
            self.manifest.calibrated = verdict.calibrated
            self.manifest.abstained = verdict.abstained
            self.manifest.finality = verdict.finality
            self.store.write_json("verdict.json", verdict)
            self.store.write_json("private/identity-map.json", self.identity_map)
            if self.request.mode == "revisit":
                self.make_revisit_report(ledger)
            self.manifest.status = "blocked" if verdict.blockers else "completed"
            self.manifest.completed_at = datetime.now(timezone.utc)
            self.store.append_event(
                "completed",
                confidence=verdict.confidence,
                blocked=bool(verdict.blockers),
            )
            self.persist_manifest()
            return ProtocolResult(self.run_id, verdict, self.exclusions, self.manifest)
        except BaseException as error:
            self.manifest.status = "failed"
            self.manifest.completed_at = datetime.now(timezone.utc)
            self.store.append_event(
                "failed", error=self.guard.redact_text(str(error))[:1000]
            )
            self.persist_manifest()
            raise
        finally:
            await self.close()


async def run_council(
    request: CouncilRequest,
    *,
    state: Path = STATE,
    settings: ProxySettings | None = None,
    transport_factory: type[ProxyTransport] = ProxyTransport,
) -> ProtocolResult:
    return await CouncilEngine(
        request,
        state=state,
        settings=settings,
        transport_factory=transport_factory,
    ).run()
    (aggregate_ballots,)
