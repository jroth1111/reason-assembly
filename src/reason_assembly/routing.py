from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Mapping

from .contracts import (
    ClaimLedger,
    ClaimStance,
    Hypothesis,
    LedgerEntry,
    ModelCapability,
    Role,
    RoleAssignment,
    TaskContract,
    TaskKind,
)
from .deliberation import BUDGET_CAPS, canonical
from .identity import LEGACY_ROUTING_POLICY_ENV, ROUTING_POLICY_ENV
from .reliability import ReliabilitySnapshot, ReliabilityStore, exploratory_run


CALL_CAPS = BUDGET_CAPS
RISK_PATTERNS = {
    "security_privacy": re.compile(
        r"(?i)\b(security|privacy|credential|secret|auth(?:entication|orization)?|"
        r"permission|encryption|pii|vulnerability)\b"
    ),
    "data_migration": re.compile(
        r"(?i)\b(migration|database|schema|backfill|data[- ]?loss|delete|"
        r"destructive|retention)\b"
    ),
    "performance_reliability": re.compile(
        r"(?i)\b(performance|latency|reliability|availability|outage|scale|"
        r"concurrency|race|timeout)\b"
    ),
    "production_safety": re.compile(
        r"(?i)\b(production|deploy|release|payment|financial|legal|"
        r"customer|incident|rollback)\b"
    ),
}
PREFERENCES = [
    "gemini-3.1-pro-low",
    "claude-opus-4-6-thinking",
    "qwen3.8-max-preview",
    "nemotron-3-nano-30b-ollama-pro",
    "minimax-m2.5-ollama-pro",
    "glm-5.2-nvidia",
    "gpt-5.6-terra",
    "gemini-pro-agent",
    "claude-sonnet-4-6",
    "kimi-k2.5-nvidia",
]


@dataclass(frozen=True)
class RoutingPolicy:
    preferences: tuple[str, ...] = field(default_factory=lambda: tuple(PREFERENCES))
    judge_model: str | None = "gpt-5.6-sol:medium"
    luna_model: str | None = "gpt-5.6-luna:low"
    integrator_model: str | None = "gpt-5.6-sol:medium"


def load_routing_policy(
    state_root: Path,
    environ: Mapping[str, str] | None = None,
) -> RoutingPolicy:
    """Load local role identities without synchronizing or touching the proxy."""
    values = os.environ if environ is None else environ
    configured = values.get(ROUTING_POLICY_ENV) or values.get(LEGACY_ROUTING_POLICY_ENV)
    path = Path(configured).expanduser() if configured else Path(state_root) / "routing-policy.toml"
    raw: dict[str, Any] = {}
    if path.is_file():
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    roles = raw.get("roles") if isinstance(raw.get("roles"), dict) else {}
    preferences_raw = raw.get("preferences", PREFERENCES)
    preferences = (
        tuple(str(item) for item in preferences_raw)
        if isinstance(preferences_raw, list)
        else tuple(PREFERENCES)
    )

    defaults = RoutingPolicy()

    def role(name: str) -> str | None:
        env_name = f"REASON_ASSEMBLY_{name.upper()}_MODEL"
        legacy_name = f"CCYCOUNCIL_{name.upper()}_MODEL"
        fallback = getattr(defaults, f"{name}_model")
        value = values.get(env_name) or values.get(legacy_name) or roles.get(name) or fallback
        return str(value) if value else None

    return RoutingPolicy(
        preferences=preferences,
        judge_model=role("judge"),
        luna_model=role("luna"),
        integrator_model=role("integrator"),
    )


@dataclass(frozen=True)
class Route:
    capability: ModelCapability
    effort: str
    role: Role

    @property
    def model(self) -> str:
        return self.capability.id

    @property
    def family(self) -> str:
        return self.capability.family


def risk_categories(text: str) -> list[str]:
    return [name for name, pattern in RISK_PATTERNS.items() if pattern.search(text)]


def budget_for(
    requested: str, prompt: str, mode: str = "decide"
) -> tuple[str, list[str]]:
    if requested != "adaptive":
        return requested, ["explicit budget"]
    categories = risk_categories(prompt)
    if categories:
        return "max", ["high-risk categories: " + ", ".join(categories)]
    if mode == "red-team":
        return "standard", ["explicit bounded red-team protocol"]
    if mode == "implement" or len(prompt) > 12_000:
        return "standard", ["implementation or large evidence set"]
    if len(prompt) < 1_200:
        return "quick", ["bounded low-complexity input"]
    return "standard", ["multi-step input"]


def infer_task_kind(mode: str, text: str) -> TaskKind:
    if mode == "implement":
        return "implementation"
    if mode == "review":
        return "review"
    if risk_categories(text):
        return "safety_gate"
    lowered = text.lower()
    if any(word in lowered for word in ("compare", "recommend", "trade-off", "prefer")):
        return "subjective_tradeoff"
    if any(
        word in lowered for word in ("synthesize", "research", "evidence", "papers")
    ):
        return "evidence_synthesis"
    return "objective_answer"


def parse_spec(spec: str, default_effort: str = "medium") -> tuple[str, str]:
    if ":" not in spec:
        return spec, default_effort
    model, effort = spec.rsplit(":", 1)
    if not model or not effort:
        raise RuntimeError(f"invalid model specification: {spec}")
    return model, effort


def parse_route_override(spec: str) -> tuple[Role, str, str]:
    if "=" not in spec:
        raise RuntimeError("route override must be ROLE=MODEL[:EFFORT]")
    role_raw, model_spec = spec.split("=", 1)
    valid = {
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
    }
    if role_raw not in valid:
        raise RuntimeError(f"invalid route role: {role_raw}")
    model, effort = parse_spec(model_spec)
    return role_raw, model, effort  # type: ignore[return-value]


def validate_effort(capability: ModelCapability, effort: str) -> None:
    if effort not in capability.efforts:
        supported = ", ".join(capability.efforts) or "none advertised"
        raise RuntimeError(
            f"unsupported effort {effort!r} for {capability.id}; supported: {supported}"
        )


def _preference_score(model_id: str, policy: RoutingPolicy | None = None) -> float:
    preferences = (policy or RoutingPolicy()).preferences
    try:
        index = preferences.index(model_id)
    except ValueError:
        index = len(preferences)
    return max(0.0, 1 - index / max(1, len(preferences)))


def _role_fit(
    capability: ModelCapability,
    role: Role,
    task_kind: TaskKind,
    policy: RoutingPolicy | None = None,
) -> float:
    base = 0.55 + 0.35 * _preference_score(capability.id, policy)
    ident = capability.id.lower()
    if task_kind in {"implementation", "review"} and any(
        key in ident for key in ("codex", "coder", "devstral", "sol")
    ):
        base += 0.1
    preferred_role_model = (
        policy.integrator_model if policy and role == "integrator"
        else policy.judge_model if policy and role in {"judge", "validator"}
        else None
    )
    if preferred_role_model and parse_spec(preferred_role_model)[0] == capability.id:
        base += 0.1
    if role in {"critic", "minority_advocate"} and any(
        key in ident for key in ("claude", "gemini", "qwen")
    ):
        base += 0.05
    if role == "verifier" and capability.tool_support:
        base += 0.08
    return max(0, min(1, base))


def score_routes(
    catalogue: list[ModelCapability | Route],
    *,
    role: Role,
    task_kind: TaskKind,
    domain: str = "general",
    snapshot: ReliabilitySnapshot,
    reliability_store: ReliabilityStore,
    health_latency: dict[str, int] | None = None,
    excluded_families: set[str] | None = None,
    peer_models: list[str] | None = None,
    policy: RoutingPolicy | None = None,
) -> list[tuple[Route, RoleAssignment]]:
    latency = health_latency or {}
    excluded = excluded_families or set()
    rows = []
    for item in catalogue:
        capability = item.capability if isinstance(item, Route) else item
        if role not in capability.roles or capability.family in excluded:
            continue
        effort = (
            item.effort
            if isinstance(item, Route)
            else "medium"
            if "medium" in capability.efforts
            else capability.efforts[0]
        )
        fit = _role_fit(capability, role, task_kind, policy)
        reliability, n, active = reliability_store.score(
            snapshot,
            model=capability.id,
            family=capability.family,
            role=role,
            task_kind=task_kind,
            domain=domain,
        )
        if role == "worker" and "test_constructor" in capability.roles:
            test_score, test_n, test_active = reliability_store.score(
                snapshot,
                model=capability.id,
                family=capability.family,
                role="test_constructor",
                task_kind=task_kind,
                domain=domain,
            )
            reliability = (reliability + test_score) / 2
            n = min(n, test_n) if active and test_active else max(n, test_n)
            active = active or test_active
        independence = 0.5
        latency_score = 1 / (1 + latency.get(capability.id, 1_000) / 5_000)
        score = (
            0.45 * fit + 0.30 * reliability + 0.20 * independence + 0.05 * latency_score
        )
        assignment = RoleAssignment(
            label="",
            role=role,
            model=capability.id,
            family=capability.family,
            effort=effort,
            score=score,
            role_fit=fit,
            reliability=reliability,
            independence=independence,
            health_latency_score=latency_score,
            reasons=[
                "active reliability" if active else f"cold start n={n:.2f}",
                f"task kind={task_kind}",
            ],
        )
        rows.append((Route(capability, effort, role), assignment))
    return sorted(rows, key=lambda row: (-row[1].score, row[0].model))


def select_role_routes(
    catalogue: list[ModelCapability | Route],
    *,
    role: Role,
    task_kind: TaskKind,
    domain: str = "general",
    count: int,
    snapshot: ReliabilitySnapshot,
    reliability_store: ReliabilityStore,
    health_latency: dict[str, int] | None = None,
    excluded_families: set[str] | None = None,
    policy: RoutingPolicy | None = None,
) -> list[tuple[Route, RoleAssignment]]:
    """Greedy role routing that re-scores historical independence per seat."""

    selected: list[tuple[Route, RoleAssignment]] = []
    used_models: set[str] = set()
    used_families = set(excluded_families or set())
    for _ in range(count):
        rows = score_routes(
            catalogue,
            role=role,
            task_kind=task_kind,
            domain=domain,
            snapshot=snapshot,
            reliability_store=reliability_store,
            health_latency=health_latency,
            excluded_families=used_families,
            peer_models=[item[0].model for item in selected],
            policy=policy,
        )
        choice = next((row for row in rows if row[0].model not in used_models), None)
        if not choice:
            break
        selected.append(choice)
        used_models.add(choice[0].model)
        used_families.add(choice[0].family)
    return selected


def candidate_pool(
    catalogue: list[ModelCapability],
    overrides: list[str],
    *,
    budget: str,
    role: Role,
    prior_models: list[str] | None = None,
    policy: RoutingPolicy | None = None,
) -> list[Route]:
    target = 2 if budget == "quick" else 3
    by_id = {item.id: item for item in catalogue}
    explicit = [
        (override_role, model, effort)
        for spec in overrides
        for override_role, model, effort in [parse_route_override(spec)]
        if override_role == role
    ]
    if explicit:
        routes = []
        families: set[str] = set()
        for _, model, effort in explicit:
            capability = by_id.get(model)
            if not capability:
                raise RuntimeError(f"model is not in the proxy catalogue: {model}")
            if role not in capability.roles:
                raise RuntimeError(f"{model} cannot serve as {role}")
            validate_effort(capability, effort)
            if capability.family in families:
                continue
            routes.append(Route(capability, effort, role))
            families.add(capability.family)
        return routes
    preferred = list(prior_models or [])
    ordered = sorted(
        catalogue,
        key=lambda item: (
            0 if item.id in preferred else 1,
            preferred.index(item.id) if item.id in preferred else 99_999,
            -_preference_score(item.id, policy),
            item.priority,
            item.id,
        ),
    )
    routes: list[Route] = []
    families: set[str] = set()
    active_policy = policy or RoutingPolicy()
    reserved_models = {
        parse_spec(spec)[0]
        for spec in (
            active_policy.judge_model,
            active_policy.luna_model,
            active_policy.integrator_model,
        )
        if spec
    }
    for capability in ordered:
        if role not in capability.roles or capability.id in reserved_models:
            continue
        if capability.family in families:
            continue
        effort = "medium" if "medium" in capability.efforts else capability.efforts[0]
        routes.append(Route(capability, effort, role))
        families.add(capability.family)
        if len(routes) >= target + 2:
            break
    return routes


def fixed_route(catalogue: list[ModelCapability], spec: str, role: Role) -> Route:
    model, effort = parse_spec(spec)
    capability = next((item for item in catalogue if item.id == model), None)
    if not capability:
        raise RuntimeError(f"model is not in the proxy catalogue: {model}")
    if role not in capability.roles:
        raise RuntimeError(f"{model} cannot serve as {role}")
    validate_effort(capability, effort)
    return Route(capability, effort, role)


def select_healthy(
    pool: list[Route], healthy_models: set[str], budget: str
) -> list[Route]:
    target = 2 if budget == "quick" else 3
    selected = []
    families: set[str] = set()
    for route in pool:
        if route.model not in healthy_models or route.family in families:
            continue
        selected.append(route)
        families.add(route.family)
        if len(selected) >= target:
            break
    return selected


async def gather_with_quorum(
    calls: dict[str, tuple[str, Awaitable[Any]]],
    *,
    grace_seconds: float,
    required_families: int = 2,
) -> tuple[dict[str, Any], dict[str, BaseException], list[str]]:
    tasks = {
        label: asyncio.create_task(awaitable) for label, (_, awaitable) in calls.items()
    }
    task_to_label = {task: label for label, task in tasks.items()}
    results: dict[str, Any] = {}
    failures: dict[str, BaseException] = {}
    cancelled: list[str] = []
    deadline: float | None = None
    loop = asyncio.get_running_loop()
    pending = set(tasks.values())
    while pending:
        timeout = None if deadline is None else max(0.0, deadline - loop.time())
        if timeout == 0:
            break
        done, pending = await asyncio.wait(
            pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            break
        for task in done:
            label = task_to_label[task]
            try:
                results[label] = task.result()
            except BaseException as exc:
                failures[label] = exc
        if (
            len({calls[label][0] for label in results}) >= required_families
            and deadline is None
        ):
            deadline = loop.time() + max(0, grace_seconds)
    for task in pending:
        task.cancel()
        cancelled.append(task_to_label[task])
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return results, failures, sorted(cancelled)


def shuffled_labels(run_id: str, count: int) -> list[str]:
    labels = [f"Candidate {chr(65 + index)}" for index in range(count)]
    seed = int(hashlib.sha256(run_id.encode()).hexdigest()[:16], 16)
    random.Random(seed).shuffle(labels)
    return labels


def stable_claim_id(text: str) -> str:
    return "C-" + hashlib.sha256(canonical(text).encode()).hexdigest()[:12]


def normalize_hypothesis(
    hypothesis: Hypothesis, label: str, method: str = "independent"
) -> Hypothesis:
    hypothesis.label = label
    hypothesis.method = method
    seen = set()
    claims = []
    for claim in hypothesis.claims:
        claim.id = stable_claim_id(claim.text)
        claim.acceptance_ids = sorted(set(claim.acceptance_ids))
        claim.evidence_refs = sorted(set(claim.evidence_refs))
        if claim.id not in seen:
            claims.append(claim)
            seen.add(claim.id)
    hypothesis.claims = claims
    return hypothesis


def build_claim_ledger(
    hypotheses: list[Hypothesis],
    stances: list[ClaimStance],
    contract: TaskContract,
) -> ClaimLedger:
    entries: dict[str, LedgerEntry] = {}
    blockers: list[str] = []
    for hypothesis in hypotheses:
        blockers.extend(hypothesis.blockers)
        for claim in hypothesis.claims:
            entry = entries.setdefault(
                claim.id, LedgerEntry(claim=claim.model_copy(deep=True))
            )
            stance = ClaimStance(
                claim_id=claim.id,
                stance=(
                    "support"
                    if claim.position == "support"
                    else "oppose"
                    if claim.position == "oppose"
                    else "uncertain"
                ),
                reason=f"initial position from {hypothesis.label}",
                evidence_refs=claim.evidence_refs,
            )
            entry.stances.append(stance)
            if stance.stance == "support":
                entry.supporting_labels.append(hypothesis.label)
            elif stance.stance == "oppose":
                entry.opposing_labels.append(hypothesis.label)
            if claim.blocker:
                blockers.append(claim.text)
    for stance in stances:
        entry = entries.get(stance.claim_id)
        if entry:
            entry.stances.append(stance)
    conflicts, missing, load_bearing = [], [], []
    for entry in entries.values():
        entry.supporting_labels = sorted(set(entry.supporting_labels))
        entry.opposing_labels = sorted(set(entry.opposing_labels))
        entry.unresolved = bool(
            entry.supporting_labels and entry.opposing_labels
        ) or any(row.stance == "uncertain" for row in entry.stances)
        if entry.unresolved and entry.claim.load_bearing:
            load_bearing.append(entry.claim.id)
        if entry.supporting_labels and entry.opposing_labels:
            conflicts.append(entry.claim.id)
        if not entry.claim.evidence_refs:
            missing.append(entry.claim.id)
    coverage = {}
    for criterion in contract.acceptance_criteria:
        words = {word for word in canonical(criterion.text).split() if len(word) > 3}
        referenced_evidence = set(
            re.findall(r"\bE-[0-9a-f]{12}\b", criterion.text, re.IGNORECASE)
        )
        citation_criterion = bool(
            re.search(r"\b(cite|citation|evidence id)\b", criterion.text, re.IGNORECASE)
        )
        coverage[criterion.id] = [
            claim_id
            for claim_id, entry in entries.items()
            if criterion.id in entry.claim.acceptance_ids
            or words & set(canonical(entry.claim.text).split())
            or referenced_evidence & set(entry.claim.evidence_refs)
            or citation_criterion
            and bool(set(entry.claim.evidence_refs) & set(contract.evidence_refs))
        ]
    return ClaimLedger(
        entries=sorted(entries.values(), key=lambda item: item.claim.id),
        conflicts=sorted(set(conflicts)),
        blockers=sorted(set(blockers)),
        missing_evidence=sorted(set(missing)),
        acceptance_coverage=coverage,
        load_bearing_unresolved=sorted(set(load_bearing)),
    )


def exploration_adjust(
    rows: list[tuple[Route, RoleAssignment]], run_id: str
) -> list[tuple[Route, RoleAssignment]]:
    if not exploratory_run(run_id) or len(rows) < 2:
        return rows
    exploratory = rows[-1]
    exploratory[1].exploratory = True
    exploratory[1].reasons.append("deterministic 20% exploration seat")
    return [exploratory, *rows[:-1]]
