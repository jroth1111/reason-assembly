#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from .artifacts import RunStore, SecretGuard
from .identity import CANONICAL_CLI, LEGACY_CLI, PRODUCT_DESCRIPTOR, VERSION
from .contracts import (
    ClaimGenealogy,
    FinalityCertificate,
    HealthResult,
    Outcome,
    OutcomeObservation,
    ReportingRules,
    RolloutCard,
    RunManifest,
    SanitizedSnapshot,
    Verdict,
)
from .git_worker import (
    ImplementationRequest,
    apply_run,
    resolve_review_target,
    run_implementation,
)
from .protocols import CouncilRequest, ProtocolResult, STATE, new_run_id, run_council
from .reliability import ReliabilityStore
from .routing import fixed_route
from .state_compat import (
    compatible_state_roots,
    iter_run_roots,
    locate_run_root,
    prepare_state_root,
)
from .v4 import CoFailureStore, digest, propagate_taint
from .v4_state import AnchorStore, PrivateJsonStore
from .transport import (
    CallBudget,
    ProxyCallError,
    ProxySettings,
    ProxyTransport,
    QuotaError,
)


def context_files(paths: list[str]) -> list[tuple[str, str]]:
    rows = []
    for item in paths:
        path = Path(item).expanduser().resolve()
        rows.append((str(path), path.read_text(errors="replace")))
    return rows


def prompt_value(args: argparse.Namespace) -> str:
    if getattr(args, "prompt", None):
        return args.prompt
    if getattr(args, "prompt_file", None):
        return Path(args.prompt_file).expanduser().read_text(errors="replace")
    return sys.stdin.read()


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--budget",
        choices=["adaptive", "quick", "standard", "max"],
        default="adaptive",
    )
    parser.add_argument("--context", action="append", default=[])
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument(
        "--demo", action="store_true", help="run a deterministic offline demonstration"
    )
    parser.add_argument("--verify-command", action="append", default=[])
    parser.add_argument(
        "--verify-shell",
        action="store_true",
        help="allow shell syntax in --verify-command (explicit local execution opt-in)",
    )
    parser.add_argument("--route", action="append", default=[])
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--quorum-grace", type=float)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--judgment-risk", type=float, default=0.10)


def add_prompt(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--prompt-file")
    add_common(parser)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog=CANONICAL_CLI,
        description=PRODUCT_DESCRIPTOR,
        epilog=(
            "Safety: prompts and selected context may be sent to configured providers; "
            "verification and implementation commands execute locally only when supplied."
        ),
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    root.add_argument(
        "--json-progress", action="store_true", help="emit JSON stage progress to stderr"
    )
    root.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="WARNING"
    )
    commands = root.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    models = commands.add_parser("models", help="list catalogued and eligible models")
    models.add_argument("--json", action="store_true")

    sync = commands.add_parser("sync", help="synchronize proxy catalogue metadata")
    sync.add_argument("--json", action="store_true")

    doctor = commands.add_parser("doctor", help="diagnose catalogue and model health")
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--all-models", action="store_true")
    doctor.add_argument("--concurrency", type=int, default=4)
    doctor.add_argument("--health-timeout", type=float, default=15.0)
    doctor.add_argument("--json", action="store_true")

    add_prompt(commands.add_parser("decide", help="run an evidence-backed decision council"))
    add_prompt(commands.add_parser("red-team", help="run adversarial analysis"))

    review = commands.add_parser("review", help="review a selected Git change")
    review.add_argument("--repo", required=True)
    target = review.add_mutually_exclusive_group()
    target.add_argument("--base")
    target.add_argument("--range", dest="range_spec")
    target.add_argument("--commit")
    target.add_argument("--staged", action="store_true")
    target.add_argument("--working-tree", action="store_true")
    add_common(review)

    implement = commands.add_parser("implement", help="compare isolated implementations")
    implement.add_argument("--repo", required=True)
    implement.add_argument("--base", default="HEAD")
    implement.add_argument("--task-file")
    implement.add_argument("--test-command", action="append", default=[])
    implement.add_argument(
        "--verification-mode",
        choices=["auto", "regression", "invariant", "docs"],
        default="auto",
    )
    implement.add_argument("--worker-timeout", type=int, default=900)
    add_common(implement)

    show = commands.add_parser("show", help="inspect a stored run or artifact")
    show.add_argument("run_id")
    show.add_argument("--artifact")
    show.add_argument("--json", action="store_true")

    replay = commands.add_parser("replay", help="replay a stored run")
    replay.add_argument("run_id")
    add_common(replay)

    revisit = commands.add_parser("revisit", help="continue a run with corrected evidence")
    revisit.add_argument("run_id")
    revisit.add_argument("--correction", required=True)
    add_common(revisit)

    outcome = commands.add_parser("outcome", help="record an observed run outcome")
    outcome.add_argument("run_id")
    outcome.add_argument(
        "status", choices=["confirmed", "disconfirmed", "mixed", "unknown"]
    )
    outcome.add_argument("--notes", default="")
    outcome.add_argument("--evidence", action="append", default=[])
    outcome.add_argument("--claim", action="append", default=[])
    outcome.add_argument("--criterion", action="append", default=[])
    outcome.add_argument("--component", action="append", default=[])
    outcome.add_argument("--candidate", action="append", default=[])
    outcome.add_argument("--receipt", action="append", default=[])
    outcome.add_argument("--json", action="store_true")

    anchors = commands.add_parser("anchors", help="manage calibration anchors")
    anchor_commands = anchors.add_subparsers(dest="anchor_cmd", required=True)
    anchor_import = anchor_commands.add_parser("import")
    anchor_import.add_argument("file")
    anchor_import.add_argument("--json", action="store_true")
    anchor_list = anchor_commands.add_parser("list")
    anchor_list.add_argument("--active", action="store_true")
    anchor_list.add_argument("--json", action="store_true")
    anchor_validate = anchor_commands.add_parser("validate")
    anchor_validate.add_argument("--json", action="store_true")
    anchor_retire = anchor_commands.add_parser("retire")
    anchor_retire.add_argument("anchor_id")
    anchor_retire.add_argument("--json", action="store_true")

    regrade = commands.add_parser("regrade", help="regrade a run without altering evidence")
    regrade.add_argument("run_id")
    regrade.add_argument("--rules", required=True)
    regrade.add_argument("--json", action="store_true")

    stats = commands.add_parser("stats", help="summarize observed outcomes")
    stats.add_argument("--json", action="store_true")

    apply_parser = commands.add_parser("apply", help="apply an accepted implementation patch")
    apply_parser.add_argument("run_id")
    apply_parser.add_argument("--repo", default=".")
    return root


def council_request(
    args: argparse.Namespace,
    mode: str,
    prompt: str,
    *,
    contexts: list[tuple[str, str]] | None = None,
    parent_run_id: str | None = None,
    ancestry_relation: str | None = None,
    prior_models: list[str] | None = None,
    manifest_mode: str | None = None,
    repo: str | None = None,
    base_commit: str | None = None,
    review_target: str | None = None,
) -> CouncilRequest:
    return CouncilRequest(
        mode=mode,
        prompt=prompt,
        budget_requested=args.budget,
        contexts=contexts if contexts is not None else context_files(args.context),
        sources=args.source,
        verify_commands=args.verify_command,
        verify_shell=getattr(args, "verify_shell", False),
        route_overrides=args.route,
        max_calls=args.max_calls,
        quorum_grace=args.quorum_grace,
        parent_run_id=parent_run_id,
        ancestry_relation=ancestry_relation,
        prior_models=prior_models or [],
        manifest_mode=manifest_mode,
        repo=repo,
        base_commit=base_commit,
        review_target=review_target,
        judgment_risk=getattr(args, "judgment_risk", 0.10),
    )


def format_result(result: ProtocolResult, as_json: bool) -> str:
    payload = {
        "run_id": result.run_id,
        "verdict": result.verdict.model_dump(mode="json"),
        "exclusions": [item.model_dump(mode="json") for item in result.exclusions],
        "calls": {
            "used": result.manifest.calls_used,
            "cap": result.manifest.call_cap,
        },
    }
    if as_json:
        return json.dumps(payload, indent=2, sort_keys=True)
    lines = [
        result.verdict.decision,
        f"confidence={result.verdict.confidence:.2f}",
        f"finality={result.verdict.finality}",
        f"calibrated={str(result.verdict.calibrated).lower()}",
        f"judgment_risk={result.verdict.judgment_risk:.3f}",
        f"abstained={str(result.verdict.abstained).lower()}",
    ]
    if result.verdict.dissent:
        lines.append("dissent=" + "; ".join(result.verdict.dissent))
    if result.verdict.blockers:
        lines.append("blockers=" + "; ".join(result.verdict.blockers))
    if result.verdict.evidence_refs:
        lines.append("evidence=" + ", ".join(result.verdict.evidence_refs))
    if result.exclusions:
        lines.append(
            "exclusions="
            + "; ".join(f"{item.model}: {item.reason}" for item in result.exclusions)
        )
    lines.append(f"calls={result.manifest.calls_used}/{result.manifest.call_cap}")
    lines.append(f"run_id={result.run_id}")
    return "\n".join(lines)


async def doctor_command(
    args: argparse.Namespace,
) -> tuple[list[Any], list[Any], dict[str, Any] | None]:
    settings = ProxySettings()
    transport = ProxyTransport(settings, sync_state_root=STATE / "v4")
    try:
        catalogue = await transport.catalogue()
        sync = (
            transport.last_sync.to_dict(SecretGuard(settings.exact_secrets))
            if transport.last_sync
            else None
        )
        if not (getattr(args, "live", False) or getattr(args, "all_models", False)):
            return catalogue, [], sync
        targets = (
            catalogue
            if getattr(args, "all_models", False)
            else [
                capability
                for capability in catalogue
                if capability.id
                in {
                    "gpt-5.6-sol",
                    "gemini-3.1-pro-low",
                    "claude-opus-4-6-thinking",
                }
            ]
        )
        eligible = [item for item in targets if item.eligible]
        transport.budget = CallBudget(max(1, len(eligible)))
        semaphore = asyncio.Semaphore(max(1, getattr(args, "concurrency", 4)))

        async def check(capability: Any) -> HealthResult:
            if not capability.eligible:
                return HealthResult(
                    model=capability.id,
                    family=capability.family,
                    status="invalid",
                    detail=", ".join(capability.exclusion_reasons),
                )
            route = fixed_route(
                catalogue,
                capability.id
                + ":"
                + ("low" if "low" in capability.efforts else capability.efforts[0]),
                "proposer" if "proposer" in capability.roles else capability.roles[0],
            )
            async with semaphore:
                started = time.monotonic()
                try:
                    await asyncio.wait_for(
                        transport.ask(
                            run_id="doctor",
                            participant=capability.id,
                            model=route.model,
                            effort=route.effort,
                            prompt="Reply with exactly OK.",
                            stage="health",
                            max_output_tokens=16,
                        ),
                        timeout=max(0.1, args.health_timeout),
                    )
                    return HealthResult(
                        model=capability.id,
                        family=capability.family,
                        status="healthy",
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
                except QuotaError as error:
                    return HealthResult(
                        model=capability.id,
                        family=capability.family,
                        status="quota",
                        detail=str(error)[:300],
                    )
                except ProxyCallError as error:
                    status = (
                        "timeout" if "timeout" in str(error).lower() else "unavailable"
                    )
                    return HealthResult(
                        model=capability.id,
                        family=capability.family,
                        status=status,
                        detail=str(error)[:300],
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    return HealthResult(
                        model=capability.id,
                        family=capability.family,
                        status="timeout",
                        detail=(f"health check exceeded {args.health_timeout:.1f}s"),
                    )

        health = await asyncio.gather(*(check(item) for item in targets))
        return catalogue, health, sync
    finally:
        await transport.close()


async def sync_command(args: argparse.Namespace) -> int:
    settings = ProxySettings()
    transport = ProxyTransport(settings, sync_state_root=STATE / "v4")
    try:
        result = await transport.synchronize()
    finally:
        await transport.close()
    payload = result.report.to_dict(SecretGuard(settings.exact_secrets))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        counts = payload["counts"]
        print(f"status={payload['status']}")
        print(
            "catalogues="
            f"raw:{counts['raw']} metadata:{counts['metadata']} "
            f"council:{counts['council']}"
        )
        print(f"equal={str(payload['equality']['all']).lower()}")
        print(f"aliases={','.join(sorted(payload['aliases'])) or 'none'}")
        print(
            "removed_candidates="
            + (",".join(payload["removed_candidates"]) or "none")
        )
        print(f"warnings={len(payload['warnings'])}")
    return 2 if payload["status"] == "failed" else 0


def print_models(
    catalogue: list[Any],
    health: list[HealthResult],
    as_json: bool,
    *,
    sync: dict[str, Any] | None = None,
    include_sync: bool = False,
) -> None:
    health_by_model = {item.model: item for item in health}
    rows = []
    for item in catalogue:
        row = item.model_dump(mode="json")
        if item.id in health_by_model:
            row["health"] = health_by_model[item.id].model_dump(mode="json")
        rows.append(row)
    if as_json:
        payload: Any = rows
        if include_sync:
            sync_payload = sync or {}
            payload = {
                "models": rows,
                "sync": sync_payload,
                "alias_resolution": {
                    "aliases": sync_payload.get("aliases", {}),
                    "removed_candidates": sync_payload.get(
                        "removed_candidates", []
                    ),
                    "empty_aliases": sync_payload.get("empty_aliases", []),
                    "pruning": sync_payload.get("pruning", {}),
                },
            }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for row in rows:
        status = row.get("health", {}).get("status", "")
        roles = ",".join(row["roles"])
        if not roles:
            roles = "listed-only" if row.get("listed_only") else "ineligible"
        print(
            f"{row['id']:<48} {row['family']:<12} "
            f"{str(row['context_window'] or 'unknown'):<9} {roles:<32} {status}"
        )


def open_store(run_id: str) -> RunStore:
    root = locate_run_root(STATE, run_id)
    return RunStore.open_existing(root, run_id, SecretGuard())


def require_writable_canonical_store(store: RunStore) -> None:
    if store.root != STATE.expanduser().resolve():
        raise RuntimeError(
            "legacy runs are read-only; wait for the run to complete and rerun "
            "Reason Assembly to import it before recording an outcome"
        )


def load_v4_run(run_id: str) -> tuple[RunStore, RunManifest]:
    store = open_store(run_id)
    raw = store.read_json("manifest.json")
    if raw.get("schema_version") != 4:
        raise RuntimeError("only schema-v4 runs are supported; schemas 1-3 cannot be migrated")
    manifest = RunManifest.model_validate(raw)
    return store, manifest


def anchors_command(args: argparse.Namespace) -> Any:
    store = AnchorStore(STATE / "v4")
    if args.anchor_cmd == "import":
        result = store.import_file(Path(args.file).expanduser().resolve())
    elif args.anchor_cmd == "list":
        result = store.list(active_only=args.active)
    elif args.anchor_cmd == "validate":
        result = store.validate()
    elif args.anchor_cmd == "retire":
        result = store.retire(args.anchor_id)
    else:
        raise RuntimeError("unknown anchors command")
    payload = (
        [item.model_dump(mode="json") for item in result]
        if isinstance(result, list)
        else result.model_dump(mode="json")
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif isinstance(result, list):
        for item in result:
            print(f"{item.id}\t{'active' if item.active else 'retired'}\t{item.task_kind}")
    else:
        print(f"{result.id}\t{'active' if result.active else 'retired'}")
    return result


def regrade_command(args: argparse.Namespace) -> RunManifest:
    parent_store, parent = load_v4_run(args.run_id)
    rules_path = Path(args.rules).expanduser().resolve()
    rules = ReportingRules.model_validate(json.loads(rules_path.read_text()))
    child_store = RunStore.create_unique(
        STATE,
        SecretGuard(),
        new_run_id,
        collision_roots=compatible_state_roots(STATE),
    )
    child_id = child_store.run_id
    preserved = {}
    for name in parent_store.artifact_names():
        if name.startswith("private/") or name in {"manifest.json", "events.jsonl"}:
            continue
        if not (
            name in {
                "verdict.json", "rollout-card.json", "claim-genealogy.json",
                "task-contract.json", "rubric.json", "finality-certificate.json",
                "taint-state.json", "approach-profile.json", "approach-signatures.json",
            }
            or name.startswith(("judging/", "verifications/", "policy/", "patches/", "receipts/"))
        ):
            continue
        source = parent_store._target(name)
        if source.is_file():
            data = source.read_bytes()
            preserved[name] = hashlib.sha256(data).hexdigest()
            child_store.write_bytes(f"preserved/{name}", data)
    rules_hash = digest(rules.model_dump(mode="json"))
    child_store.write_json("reporting-rules.json", rules)
    parent_verdict = Verdict.model_validate(parent_store.read_json("verdict.json"))
    if not rules.include_dissent:
        parent_verdict.dissent = []
    child_store.write_json("verdict.json", parent_verdict)
    parent_certificate = FinalityCertificate.model_validate(
        parent_store.read_json("finality-certificate.json")
    )
    parent_certificate.reporting_rules_sha256 = rules_hash
    child_store.write_json("finality-certificate.json", parent_certificate)
    parent_card = RolloutCard.model_validate(
        parent_store.read_json("rollout-card.json")
    )
    parent_card.run_id = child_id
    parent_card.reporting_rules_sha256 = rules_hash
    parent_card.call_manifest = []
    parent_card.finality = parent_certificate
    parent_card.views.append({
        "kind": "regrade",
        "parent_run_id": parent.run_id,
        "preserved_artifact_hashes": preserved,
        "call_free": True,
    })
    child_store.write_json("rollout-card.json", parent_card)
    child_store.write_json("regrade-view.json", {
        "parent_run_id": parent.run_id,
        "preserved_artifact_hashes": preserved,
        "reporting_rules": rules.model_dump(mode="json"),
        "call_free": True,
    })
    child = parent.model_copy(deep=True, update={
        "run_id": child_id,
        "mode": "regrade",
        "created_at": datetime.now(timezone.utc),
        "completed_at": datetime.now(timezone.utc),
        "parent_run_id": parent.run_id,
        "ancestry_relation": "regrade",
        "calls_used": 0,
        "call_cap": 0,
        "reporting_rules_sha256": rules_hash,
        "artifacts": [],
    })
    raw = RunManifest.model_validate(child.model_dump(mode="json")).model_dump(mode="json")
    child_store.write_json("manifest.json", raw)
    raw["artifacts"] = child_store.artifact_names()
    child_store.write_json("manifest.json", raw)
    child_store.seal_manifest()
    raw = child_store.read_json("manifest.json")
    if args.json:
        print(json.dumps(raw, indent=2, sort_keys=True))
    else:
        print(f"regraded {parent.run_id} as {child_id} with 0 calls")
    return child


def show_command(args: argparse.Namespace) -> None:
    store, manifest = load_v4_run(args.run_id)
    integrity_issues = store.verify_integrity()
    if integrity_issues:
        print(
            f"{CANONICAL_CLI}: warning: integrity: {', '.join(integrity_issues)}",
            file=sys.stderr,
        )
    if args.artifact:
        relative = args.artifact
        if relative == "private" or relative.startswith("private/"):
            raise RuntimeError("private identity artifacts are not exposed by show")
        target = store._target(relative)
        if not target.is_file():
            raise RuntimeError(f"artifact not found: {relative}")
        print(target.read_text(errors="replace"), end="")
        return
    payload: dict[str, Any] = {"manifest": manifest.model_dump(mode="json")}
    for name in ("verdict.json", "outcome.json", "claim-ledger.json"):
        target = store._target(name)
        if target.exists():
            payload[name.removesuffix(".json")] = store.read_json(name)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        verdict = payload.get("verdict") or {}
        print(
            "\n".join(
                [
                    str(verdict.get("decision", manifest.status)),
                    f"confidence={verdict.get('confidence', 'unknown')}",
                    f"finality={verdict.get('finality', manifest.finality or 'unknown')}",
                    f"calibrated={verdict.get('calibrated', manifest.calibrated)}",
                    f"judgment_risk={verdict.get('judgment_risk', manifest.judgment_risk)}",
                    f"abstained={verdict.get('abstained', manifest.abstained)}",
                    f"status={manifest.status}",
                    f"mode={manifest.mode}",
                    f"calls={manifest.calls_used}/{manifest.call_cap}",
                    f"run_id={manifest.run_id}",
                ]
            )
        )


def route_substitutions(
    parent: RunManifest, current: RunManifest
) -> list[dict[str, str]]:
    old_by_role: dict[str, list[Any]] = defaultdict(list)
    new_by_role: dict[str, list[Any]] = defaultdict(list)
    for route in parent.routes:
        old_by_role[str(route.role)].append(route)
    for route in current.routes:
        new_by_role[str(route.role)].append(route)
    substitutions = []
    for role in sorted(set(old_by_role) | set(new_by_role)):
        old = [item.model for item in old_by_role[role]]
        new = [item.model for item in new_by_role[role]]
        common = set(old) & set(new)
        old = [item for item in old if item not in common]
        new = [item for item in new if item not in common]
        for index in range(max(len(old), len(new))):
            before = old[index] if index < len(old) else "none"
            after = new[index] if index < len(new) else "none"
            if before != after:
                substitutions.append(
                    {
                        "role": role,
                        "prior_model": before,
                        "replacement_model": after,
                    }
                )
    return substitutions


def record_substitutions(result: ProtocolResult, parent: RunManifest) -> ProtocolResult:
    result.manifest.route_substitutions = route_substitutions(parent, result.manifest)
    store = open_store(result.run_id)
    result.manifest.artifacts = store.artifact_names()
    store.write_json("manifest.json", result.manifest)
    return result


def extraction_subject_ids(
    extraction: dict[str, Any], ledger_entries: list[dict[str, Any]]
) -> set[str]:
    subjects = {claim.get("id", "") for claim in extraction.get("claims", [])}
    for extracted in extraction.get("claims", []):
        extracted_acceptance = set(extracted.get("acceptance_ids", []))
        extracted_evidence = set(extracted.get("evidence_refs", []))
        for entry in ledger_entries:
            claim = entry.get("claim", {})
            if extracted_acceptance & set(
                claim.get("acceptance_ids", [])
            ) and extracted_evidence & set(claim.get("evidence_refs", [])):
                subjects.add(claim.get("id", ""))
    subjects.discard("")
    return subjects


async def replay_command(args: argparse.Namespace) -> ProtocolResult:
    store, parent = load_v4_run(args.run_id)
    snapshot = SanitizedSnapshot.model_validate(store.read_json("snapshot.json"))
    prior_models = [route.model for route in parent.routes]
    contexts = [(item["source"], item["content"]) for item in snapshot.contexts]
    budget = args.budget
    if budget == "adaptive":
        budget = snapshot.budget_requested
    if snapshot.mode == "implement":
        request = ImplementationRequest(
            repo=snapshot.repo or "",
            base=snapshot.base_commit or "HEAD",
            task=snapshot.prompt,
            budget_requested=budget,
            contexts=contexts,
            sources=snapshot.sources,
            verify_commands=snapshot.verify_commands,
            route_overrides=args.route,
            max_calls=args.max_calls,
            quorum_grace=args.quorum_grace,
            test_commands=snapshot.test_commands,
            verification_mode=snapshot.verification_mode or "auto",
            parent_run_id=args.run_id,
            ancestry_relation="replay",
            prior_models=prior_models,
            manifest_mode="replay",
        )
        result = await run_implementation(request)
    else:
        request = council_request(
            args,
            snapshot.mode,
            snapshot.prompt,
            contexts=contexts,
            parent_run_id=args.run_id,
            ancestry_relation="replay",
            prior_models=prior_models,
            manifest_mode="replay",
            repo=snapshot.repo,
            base_commit=snapshot.base_commit,
            review_target=snapshot.review_target,
        )
        request.budget_requested = budget
        request.sources = snapshot.sources
        request.verify_commands = snapshot.verify_commands
        result = await run_council(request)
    return record_substitutions(result, parent)


async def revisit_command(args: argparse.Namespace) -> ProtocolResult:
    store, parent = load_v4_run(args.run_id)
    snapshot = SanitizedSnapshot.model_validate(store.read_json("snapshot.json"))
    contexts = [(item["source"], item["content"]) for item in snapshot.contexts]
    for name in ("claim-ledger.json", "verdict.json", "task-contract.json"):
        target = store._target(name)
        if target.exists():
            contexts.append((f"prior:{name}", target.read_text()))
    contexts.append(("correction", args.correction))
    request = council_request(
        args,
        "revisit",
        snapshot.prompt + "\n\nCORRECTION\n" + args.correction,
        contexts=contexts,
        parent_run_id=args.run_id,
        ancestry_relation="revisit",
        prior_models=[route.model for route in parent.routes],
        manifest_mode="revisit",
        repo=snapshot.repo,
        base_commit=snapshot.base_commit,
        review_target=snapshot.review_target,
    )
    return await run_council(request)


def outcome_command(args: argparse.Namespace) -> Outcome:
    store, manifest = load_v4_run(args.run_id)
    require_writable_canonical_store(store)
    if store._target("outcome.json").exists():
        raise RuntimeError(
            "outcome is already recorded; use revisit for corrected evidence"
        )
    valid_evidence = set(manifest.artifacts)
    valid_evidence.update(item.id for item in manifest.evidence)
    valid_evidence.update(
        value
        for item in manifest.evidence
        for value in (item.source, item.final_url)
        if value and value.startswith("https://")
    )
    for item in args.evidence:
        if item not in valid_evidence:
            raise RuntimeError(
                f"outcome evidence is not a run artifact or evidence ID: {item}"
            )

    def parse_rows(kind: str, values: list[str]) -> list[OutcomeObservation]:
        rows = []
        evidence_ids = {item.id for item in manifest.evidence}
        strong_evidence = any(
            item in evidence_ids
            or item.startswith(("evidence/", "receipts/", "validations/"))
            for item in args.evidence
        )
        for value in values:
            if "=" not in value:
                raise RuntimeError(f"{kind} outcome must be ID=STATUS")
            subject_id, status = value.rsplit("=", 1)
            if status not in {"confirmed", "disconfirmed", "mixed", "unknown"}:
                raise RuntimeError(f"invalid outcome status: {status}")
            rows.append(
                OutcomeObservation(
                    subject_type=kind,
                    subject_id=subject_id,
                    status=status,
                    evidence=args.evidence,
                    notes=args.notes,
                    weight=1.0 if strong_evidence else 0.25,
                )
            )
        return rows

    observations = [
        *parse_rows("claim", args.claim),
        *parse_rows("criterion", args.criterion),
        *parse_rows("component", args.component),
        *parse_rows("candidate", getattr(args, "candidate", [])),
        *parse_rows("receipt", getattr(args, "receipt", [])),
    ]
    if not observations:
        observations = [
            OutcomeObservation(
                subject_type="run",
                subject_id=args.run_id,
                status=args.status,
                evidence=args.evidence,
                notes=args.notes,
                weight=0.25,
            )
        ]
    outcome = Outcome(
        run_id=args.run_id,
        status=args.status,
        notes=args.notes,
        evidence=args.evidence,
        observations=observations,
        recorded_at=datetime.now(timezone.utc),
    )
    mutations: list[tuple[str, Any]] = [("outcome.json", outcome)]
    invalidation_event: dict[str, Any] | None = None
    invalidated = {
        item.subject_id
        for item in observations
        if item.status in {"disconfirmed", "mixed"}
    }
    if invalidated and store._target("claim-genealogy.json").exists():
        genealogy = ClaimGenealogy.model_validate(
            store.read_json("claim-genealogy.json")
        )
        taint = propagate_taint(genealogy, invalidated)
        mutations.extend(
            [("claim-genealogy.json", genealogy), ("taint-state.json", taint)]
        )
        certificate: FinalityCertificate | None = None
        if taint.tainted_ids and store._target("verdict.json").exists():
            current_verdict = Verdict.model_validate(store.read_json("verdict.json"))
            current_verdict.finality = "abort"
            current_verdict.abstained = True
            current_verdict.blockers = sorted(
                set(current_verdict.blockers)
                | {"post-outcome evidence invalidated verdict lineage"}
            )
            mutations.append(("verdict.json", current_verdict))
        if taint.tainted_ids and store._target("finality-certificate.json").exists():
            certificate = FinalityCertificate.model_validate(
                store.read_json("finality-certificate.json")
            )
            certificate.finality = "abort"
            certificate.accepted = False
            certificate.unresolved_claim_ids = sorted(
                set(certificate.unresolved_claim_ids) | invalidated
            )
            mutations.append(("finality-certificate.json", certificate))
        if taint.tainted_ids and store._target("rollout-card.json").exists() and certificate:
            card = RolloutCard.model_validate(store.read_json("rollout-card.json"))
            card.finality = certificate
            card.taint_transitions = taint.transitions
            mutations.append(("rollout-card.json", card))
        invalidation_event = {
            "tainted_ids": taint.tainted_ids,
            "applied_commit_preserved": True,
        }
    manifest.artifacts = sorted(
        set(store.artifact_names()) | {name for name, _ in mutations}
    )
    mutations.append(("manifest.json", manifest))
    store.write_transaction(
        mutations,
        require_absent="outcome.json",
        commit_artifact="manifest.json",
    )
    store.append_event("outcome", status=outcome.status)
    if invalidation_event:
        store.append_event("post_outcome_invalidation", **invalidation_event)
    identity_rows: list[tuple[str, dict[str, str]]] = []
    identity_path = store._target("private/identity-map.json")
    if identity_path.exists():
        identity_rows.extend(
            (label, identity)
            for label, identity in store.read_json("private/identity-map.json").items()
        )
    identity_rows.extend(
        (
            route.label,
            {
                "model": route.model,
                "family": route.family,
                "role": route.role,
            },
        )
        for route in manifest.routes
    )
    subjects_by_label: dict[str, set[str]] = defaultdict(set)
    predictions_by_label: dict[str, dict[str, str]] = defaultdict(dict)
    competitor_label_by_label: dict[str, str] = {}
    ledger_entries: list[dict[str, Any]] = []
    ledger_path = store._target("claim-ledger.json")
    if ledger_path.exists():
        ledger = store.read_json("claim-ledger.json")
        ledger_entries = ledger.get("entries", [])
        for entry in ledger_entries:
            claim_id = entry.get("claim", {}).get("id")
            supporters = entry.get("supporting_labels", [])
            opponents = entry.get("opposing_labels", [])
            for label in supporters:
                if claim_id:
                    subjects_by_label[label].add(claim_id)
                    predictions_by_label[label][claim_id] = "support"
                if opponents:
                    competitor_label_by_label[label] = opponents[0]
            for label in opponents:
                if claim_id:
                    subjects_by_label[label].add(claim_id)
                    predictions_by_label[label][claim_id] = "oppose"
                if supporters:
                    competitor_label_by_label[label] = supporters[0]
        for criterion_id, claim_ids in ledger.get("acceptance_coverage", {}).items():
            for entry in ledger.get("entries", []):
                if entry.get("claim", {}).get("id") in claim_ids:
                    for label in entry.get("supporting_labels", []):
                        subjects_by_label[label].add(criterion_id)
    graph_path = store._target("contribution-graph.json")
    if graph_path.exists():
        for contribution in store.read_json("contribution-graph.json").get(
            "contributions", []
        ):
            label = contribution.get("candidate_label", "")
            subjects_by_label[label].add(contribution.get("id", ""))
            subjects_by_label[label].update(contribution.get("acceptance_ids", []))
    verification_root = store._target("verifications")
    if verification_root.exists():
        for path in verification_root.glob("*.json"):
            receipt = json.loads(path.read_text())
            step_id = receipt.get("step_id")
            claim_id = receipt.get("claim_id")
            if step_id and claim_id:
                subjects_by_label[f"Verifier {step_id}"].add(claim_id)
                match = re.search(r"-vote-(\d+)\.json$", path.name)
                if match:
                    subjects_by_label[f"Verifier {step_id}-{match.group(1)}"].add(
                        claim_id
                    )
    minority_root = store._target("minority")
    if minority_root.exists():
        for path in minority_root.glob("*.json"):
            value = json.loads(path.read_text())
            if value.get("advocate_label") and value.get("claim_id"):
                subjects_by_label[value["advocate_label"]].add(value["claim_id"])
    extraction_path = store._target("evidence-extraction.json")
    if extraction_path.exists():
        extraction = store.read_json("evidence-extraction.json")
        extractor_label = extraction.get("extractor_label", "")
        subjects_by_label[extractor_label].update(
            extraction_subject_ids(extraction, ledger_entries)
        )
    all_subjects = {item.subject_id for item in observations}
    confidence_path = store._target("confidence-estimate.json")
    judge_reported = (
        store.read_json("confidence-estimate.json").get("reported")
        if confidence_path.exists()
        else None
    )
    seen = set()
    attributions = []
    identity_by_label = {label: identity for label, identity in identity_rows}
    consistency_path = store._target("judging/consistency.json")
    order_consistent = (
        bool(store.read_json("judging/consistency.json").get("consistent"))
        if consistency_path.exists()
        else None
    )
    revised = store._target("judging/revised-verdict.json").exists()
    contract_path = store._target("task-contract.json")
    domain = manifest.task_kind or "objective_answer"
    if contract_path.exists():
        domain_tags = store.read_json("task-contract.json").get("domain_tags", [])
        if domain_tags:
            domain = sorted(domain_tags)[0]
    for label, identity in identity_rows:
        key = (
            identity.get("model", "unknown"),
            identity.get("family", "unknown"),
            identity.get("role", "proposer"),
        )
        if key in seen:
            continue
        seen.add(key)
        role = key[2]
        subject_label = label.removesuffix(" test-construction")
        subjects = (
            all_subjects
            if role in {"judge", "validator"}
            or any(item.subject_type == "run" for item in observations)
            else subjects_by_label.get(subject_label, set()) & all_subjects
        )
        if not subjects:
            continue
        attributions.append(
            (
                *key,
                manifest.task_kind or "objective_answer",
                1.0,
                judge_reported if role == "judge" else None,
                sorted(subjects),
                {
                    "prediction": (
                        "support"
                        if role
                        in {
                            "proposer",
                            "judge",
                            "validator",
                            "worker",
                            "integrator",
                        }
                        else None
                    ),
                    "predictions": predictions_by_label.get(subject_label, {}),
                    "detected_error": (
                        any(
                            item.subject_id in subjects
                            and item.status == "disconfirmed"
                            for item in observations
                        )
                        if role in {"critic", "verifier", "minority_advocate"}
                        else None
                    ),
                    "order_consistent": (order_consistent if role == "judge" else None),
                    "revised": revised if role == "judge" else None,
                    "revision_correct": (
                        outcome.status == "confirmed"
                        if role == "judge" and revised
                        else None
                    ),
                    "competitor": identity_by_label.get(
                        competitor_label_by_label.get(subject_label, ""), {}
                    ).get("model"),
                    "domain": domain,
                },
            )
        )
    reliability = ReliabilityStore(STATE / "v4")
    reliability.update(reliability.load(), outcome, attributions)
    verdict = store.read_json("verdict.json")
    calibration_store = PrivateJsonStore(
        STATE / "v4" / "calibration.json",
        {"schema_version": 4, "examples": []},
    )
    def append_calibration(calibration: dict[str, Any]) -> None:
        calibration["examples"].append({
            "run_id": args.run_id,
            "score": float(verdict.get("confidence", 0)),
            "correct": outcome.status == "confirmed",
            "task_kind": manifest.task_kind or "objective_answer",
            "domain": domain,
            "route_epoch": manifest.route_epoch,
            "judgment_risk": manifest.judgment_risk,
        })

    calibration_store.locked_write(append_calibration)
    candidate_rows = {
        item.subject_id: item.status == "confirmed"
        for item in observations if item.subject_type == "candidate"
    }
    if candidate_rows:
        identity_by_label = dict(identity_rows)
        route_correct = {
            identity_by_label[label]["model"]: correct
            for label, correct in candidate_rows.items()
            if label in identity_by_label
        }
        selected = verdict.get("selected_candidate")
        receipt_rows = [
            item for item in observations if item.subject_type == "receipt"
        ]
        CoFailureStore(STATE / "v4").record(
            routes=[route.model for route in manifest.routes if route.role == "proposer"],
            families=[route.family for route in manifest.routes if route.role == "proposer"],
            task_kind=manifest.task_kind or "objective_answer",
            domain=domain,
            answer_format=manifest.task_kind or "text",
            route_correct=route_correct,
            selected_correct=candidate_rows.get(selected, outcome.status == "confirmed"),
            verification_correct=(
                all(item.status == "confirmed" for item in receipt_rows)
                if receipt_rows else None
            ),
        )
    effects_store = PrivateJsonStore(
        STATE / "v4" / "operation-effects.json",
        {"schema_version": 4, "effects": []},
    )
    new_effects = []
    for artifact in manifest.artifacts:
        if artifact.startswith("policy/decision-") and artifact.endswith(".json"):
            decision = store.read_json(artifact)
            new_effects.append({
                "run_id": args.run_id,
                "task_kind": manifest.task_kind or "objective_answer",
                "operation": decision["operation"],
                "resolved": outcome.status == "confirmed",
                "aleatoric_cost": 0.0 if outcome.status == "confirmed" else 1.0,
                "context_cost": 0.0,
                "calls": manifest.calls_used,
            })
    effects_store.locked_write(lambda effects: effects["effects"].extend(new_effects))
    store.seal_manifest()
    return outcome


def _aggregate(rows: list[tuple[str, str, float]]) -> dict[str, Any]:
    buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "observations": 0,
            "score_sum": 0.0,
            "confirmed": 0,
            "disconfirmed": 0,
            "mixed": 0,
        }
    )
    for key, status, score in rows:
        bucket = buckets[key]
        bucket["observations"] += 1
        bucket["score_sum"] += score
        bucket[status] += 1
    result = {}
    for key, bucket in sorted(buckets.items()):
        observations = int(bucket.pop("observations"))
        score_sum = bucket.pop("score_sum")
        result[key] = {
            "observations": observations,
            "observed_accuracy": round(score_sum / observations, 4),
            **{name: int(value) for name, value in bucket.items()},
        }
    return result


def stats_command() -> dict[str, Any]:
    dimensions: dict[str, list[tuple[str, str, float]]] = {
        "model": [],
        "family": [],
        "role": [],
        "domain": [],
        "mode": [],
    }
    runs = 0
    run_roots = iter_run_roots(STATE)
    for run_id, _root in run_roots:
        store, manifest = load_v4_run(run_id)
        try:
            outcome = Outcome.model_validate(store.read_json("outcome.json"))
        except (OSError, ValidationError):
            continue
        if outcome.status == "unknown":
            continue
        score = {
            "confirmed": 1.0,
            "disconfirmed": 0.0,
            "mixed": 0.5,
        }[outcome.status]
        runs += 1
        dimensions["mode"].append((manifest.mode, outcome.status, score))
        identities: list[tuple[str, dict[str, str]]] = []
        identity_path = store._target("private/identity-map.json")
        if identity_path.exists():
            identities.extend(store.read_json("private/identity-map.json").items())
        identities.extend(
            (
                route.label,
                {
                    "model": route.model,
                    "family": route.family,
                    "role": route.role,
                },
            )
            for route in manifest.routes
        )
        subjects_by_label: dict[str, set[str]] = defaultdict(set)
        ledger_entries: list[dict[str, Any]] = []
        ledger_path = store._target("claim-ledger.json")
        if ledger_path.exists():
            ledger = store.read_json("claim-ledger.json")
            ledger_entries = ledger.get("entries", [])
            for entry in ledger_entries:
                claim_id = entry.get("claim", {}).get("id")
                for label in entry.get("supporting_labels", []) + entry.get(
                    "opposing_labels", []
                ):
                    if claim_id:
                        subjects_by_label[label].add(claim_id)
            for criterion_id, claim_ids in ledger.get(
                "acceptance_coverage", {}
            ).items():
                for entry in ledger.get("entries", []):
                    if entry.get("claim", {}).get("id") in claim_ids:
                        for label in entry.get("supporting_labels", []) + entry.get(
                            "opposing_labels", []
                        ):
                            subjects_by_label[label].add(criterion_id)
        graph_path = store._target("contribution-graph.json")
        if graph_path.exists():
            for contribution in store.read_json("contribution-graph.json").get(
                "contributions", []
            ):
                label = contribution.get("candidate_label", "")
                subjects_by_label[label].add(contribution.get("id", ""))
                subjects_by_label[label].update(contribution.get("acceptance_ids", []))
        extraction_path = store._target("evidence-extraction.json")
        if extraction_path.exists():
            extraction = store.read_json("evidence-extraction.json")
            extractor_label = extraction.get("extractor_label", "")
            subjects_by_label[extractor_label].update(
                extraction_subject_ids(extraction, ledger_entries)
            )
        domain = manifest.task_kind or "objective_answer"
        contract_path = store._target("task-contract.json")
        if contract_path.exists():
            domain_tags = store.read_json("task-contract.json").get("domain_tags", [])
            if domain_tags:
                domain = sorted(domain_tags)[0]
        dimensions["domain"].append((domain, outcome.status, score))
        observed_subjects = {item.subject_id for item in outcome.observations}
        run_observation = any(
            item.subject_type == "run" for item in outcome.observations
        )
        seen: set[tuple[str, str, str]] = set()
        for label, identity in identities:
            key = (
                identity.get("model", "unknown"),
                identity.get("family", "unknown"),
                identity.get("role", "unknown"),
            )
            if key in seen:
                continue
            role = key[2]
            subject_label = label.removesuffix(" test-construction")
            if (
                not run_observation
                and role not in {"judge", "validator"}
                and not (
                    subjects_by_label.get(subject_label, set()) & observed_subjects
                )
            ):
                continue
            seen.add(key)
            relevant = [
                observation
                for observation in outcome.observations
                if observation.status != "unknown"
                and (
                    run_observation
                    or role in {"judge", "validator"}
                    or observation.subject_id
                    in subjects_by_label.get(subject_label, set())
                )
            ]
            for observation in relevant:
                observation_score = {
                    "confirmed": 1.0,
                    "disconfirmed": 0.0,
                    "mixed": 0.5,
                }[observation.status]
                dimensions["model"].append(
                    (key[0], observation.status, observation_score)
                )
                dimensions["family"].append(
                    (key[1], observation.status, observation_score)
                )
                dimensions["role"].append(
                    (key[2], observation.status, observation_score)
                )
    reliability_snapshot = ReliabilityStore(STATE / "v4").load()
    diagnostics = {
        f"{item.model}|{item.role}|{item.task_kind}|{item.domain}": {
            "effective_observations": round(item.effective_observations, 4),
            "posterior_mean": round(item.posterior_mean, 4),
            "active": item.active,
            "false_positive_rate": round(
                item.false_positive
                / max(1e-9, item.false_positive + item.true_negative),
                4,
            ),
            "false_negative_rate": round(
                item.false_negative
                / max(1e-9, item.false_negative + item.true_positive),
                4,
            ),
            "error_detection_rate": round(
                item.error_detection_success / max(1e-9, item.error_detection_total),
                4,
            ),
            "calibration_error": round(
                item.calibration_absolute_error
                / max(1e-9, item.calibration_observations),
                4,
            ),
            "order_consistency": round(
                item.order_consistent / max(1e-9, item.order_observations),
                4,
            ),
            "revision_success_rate": round(
                item.revision_success / max(1e-9, item.revision_observations),
                4,
            ),
            "competitor_failure_rates": {
                competitor: round(
                    failures
                    / max(1e-9, item.competitor_observations.get(competitor, 0)),
                    4,
                )
                for competitor, failures in sorted(item.competitor_failures.items())
            },
        }
        for item in reliability_snapshot.buckets
    }
    return {
        "runs_with_observed_outcomes": runs,
        **{name: _aggregate(rows) for name, rows in dimensions.items()},
        "routing_changed": any(item.active for item in reliability_snapshot.buckets),
        "reliability_diagnostics": diagnostics,
        "reliability": reliability_snapshot.model_dump(mode="json"),
    }


async def async_main(args: argparse.Namespace) -> ProtocolResult | int | None:
    if getattr(args, "demo", False) and args.cmd in {"decide", "red-team", "review"}:
        from .demo import run_demo

        prompt = (
            prompt_value(args)
            if args.cmd in {"decide", "red-team"}
            else "Offline review demonstration with bundled evidence."
        )
        result = run_demo(args.cmd, prompt)
        print(format_result(result, args.json))
        return result
    if args.cmd == "sync":
        return await sync_command(args)
    if args.cmd in {"models", "doctor"}:
        catalogue, health, sync = await doctor_command(args)
        print_models(
            catalogue,
            health,
            args.json,
            sync=sync,
            include_sync=args.cmd == "doctor" and args.all_models,
        )
        return None
    if args.cmd == "decide":
        result = await run_council(council_request(args, "decide", prompt_value(args)))
    elif args.cmd == "red-team":
        result = await run_council(
            council_request(args, "red-team", prompt_value(args))
        )
    elif args.cmd == "review":
        root, diff, target = resolve_review_target(
            args.repo,
            base=args.base,
            range_spec=args.range_spec,
            commit=args.commit,
            staged=args.staged,
            working_tree=args.working_tree,
        )
        result = await run_council(
            council_request(
                args,
                "review",
                f"Review the selected changes in repository {root}.",
                contexts=[(f"git:{target}", diff)] + context_files(args.context),
                repo=str(root),
                review_target=target,
            )
        )
    elif args.cmd == "implement":
        task = (
            Path(args.task_file).expanduser().read_text(errors="replace")
            if args.task_file
            else sys.stdin.read()
        )
        result = await run_implementation(
            ImplementationRequest(
                repo=args.repo,
                base=args.base,
                task=task,
                budget_requested=args.budget,
                contexts=context_files(args.context),
                sources=args.source,
                verify_commands=args.verify_command,
                verify_shell=args.verify_shell,
                route_overrides=args.route,
                max_calls=args.max_calls,
                quorum_grace=args.quorum_grace,
                test_commands=args.test_command,
                verification_mode=args.verification_mode,
                worker_timeout=args.worker_timeout,
                judgment_risk=args.judgment_risk,
            )
        )
    elif args.cmd == "replay":
        result = await replay_command(args)
    elif args.cmd == "revisit":
        result = await revisit_command(args)
    else:
        return None
    print(format_result(result, args.json))
    return result


def main() -> None:
    args = parser().parse_args()
    if getattr(args, "verify_shell", False):
        print(
            f"{CANONICAL_CLI}: warning: --verify-shell executes authorized commands "
            "through the local shell",
            file=sys.stderr,
        )
    try:
        migration = prepare_state_root(STATE)
        if migration.errors:
            print(
                f"{CANONICAL_CLI}: warning: legacy state import encountered "
                f"{len(migration.errors)} skipped entries; read-only discovery remains active",
                file=sys.stderr,
            )
        if args.cmd == "show":
            show_command(args)
            return
        if args.cmd == "anchors":
            anchors_command(args)
            return
        if args.cmd == "regrade":
            regrade_command(args)
            return
        if args.cmd == "outcome":
            outcome = outcome_command(args)
            if args.json:
                print(json.dumps(outcome.model_dump(mode="json"), indent=2))
            else:
                print(f"recorded {outcome.status} for {outcome.run_id}")
            return
        if args.cmd == "stats":
            stats = stats_command()
            if args.json:
                print(json.dumps(stats, indent=2, sort_keys=True))
            else:
                print(
                    f"runs_with_observed_outcomes="
                    f"{stats['runs_with_observed_outcomes']}"
                )
                for dimension in ("model", "family", "role", "mode"):
                    for key, row in stats[dimension].items():
                        print(
                            f"{dimension}={key} "
                            f"accuracy={row['observed_accuracy']:.3f} "
                            f"n={row['observations']}"
                        )
            return
        if args.cmd == "apply":
            commit = apply_run(STATE, args.run_id, args.repo)
            print(f"applied {commit} without pushing")
            return
        logging.basicConfig(level=getattr(logging, args.log_level))
        if args.json_progress:
            from .observability import ProgressEmitter

            emitter = ProgressEmitter(json_output=True)
            emitter.stage(args.cmd, "started")
        result = asyncio.run(async_main(args))
        if args.json_progress:
            emitter.stage(
                args.cmd,
                "completed",
                run_id=getattr(result, "run_id", None),
            )
        if isinstance(result, int) and result:
            raise SystemExit(result)
    except (
        RuntimeError,
        OSError,
        subprocess.CalledProcessError,
        ValidationError,
        httpx.HTTPError,
        ValueError,
    ) as error:
        try:
            settings = ProxySettings()
            message = SecretGuard(settings.exact_secrets).redact_text(str(error))
        except BaseException:
            message = SecretGuard().redact_text(str(error))
        print(f"{CANONICAL_CLI}: {message}", file=sys.stderr)
        raise SystemExit(2)


def legacy_main() -> None:
    if os.environ.get("REASON_ASSEMBLY_SUPPRESS_DEPRECATION") != "1":
        print(f"{LEGACY_CLI} is deprecated; use {CANONICAL_CLI} instead.", file=sys.stderr)
    main()


if __name__ == "__main__":
    main()
