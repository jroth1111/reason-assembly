from __future__ import annotations

import json
import asyncio
import inspect
from types import SimpleNamespace

import pytest

from contracts import ModelCapability


def capability(
    model: str,
    family: str,
    priority: int,
    roles: list[str],
) -> ModelCapability:
    return ModelCapability(
        id=model,
        family=family,
        provider=family,
        context_window=200_000,
        efforts=["low", "medium", "high"],
        api_support=True,
        tool_support="worker" in roles or "integrator" in roles,
        input_modalities=["text"],
        output_modalities=["text"],
        priority=priority,
        visibility="list",
        roles=roles,
        eligible=True,
    )


CATALOGUE = [
    capability(
        "gpt-5.6-sol",
        "openai",
        1,
        [
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
        ],
    ),
    capability("gpt-5.6-luna", "openai", 2, ["utility"]),
    capability(
        "gemini-3.1-pro-low",
        "google",
        10,
        [
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
        ],
    ),
    capability(
        "claude-opus-4-6-thinking",
        "anthropic",
        20,
        [
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
        ],
    ),
    capability(
        "qwen3.8-max-preview",
        "qwen",
        30,
        [
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
        ],
    ),
    capability(
        "glm-5.2-nvidia",
        "nvidia",
        40,
        [
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
        ],
    ),
]


class FakeTransport:
    prompts: list[tuple[str, str, str]] = []
    judge_action = "select"
    validation_sequence: list[str] = []
    verification_sequence: list[str] = []
    malformed_once: set[str] = set()
    seen_malformed: set[str] = set()

    def __init__(self, settings, *, budget=None, **kwargs):
        self.settings = settings
        self.budget = budget

    @classmethod
    def reset(cls):
        cls.prompts = []
        cls.judge_action = "select"
        cls.validation_sequence = []
        cls.verification_sequence = []
        cls.malformed_once = set()
        cls.seen_malformed = set()

    async def close(self):
        return None

    async def catalogue(self):
        return [item.model_copy(deep=True) for item in CATALOGUE]

    def response(self, stage: str, prompt: str) -> dict:
        evidence = __import__("re").findall(r"E-[0-9a-f]{12}", prompt)
        evidence_id = evidence[0] if evidence else "E-000000000000"
        if stage == "extraction" or stage.startswith("extraction:"):
            high = bool(
                __import__("re").search(
                    r'"task": "[^"]*(?:production|migration)',
                    prompt.lower(),
                )
            )
            return {
                "original_task_sha256": "",
                "objective": "Resolve task",
                "acceptance_criteria": [
                    {
                        "id": "AC-001",
                        "text": "Resolve task with supplied evidence",
                        "verification": "evidence_entailment",
                    }
                ],
                "constraints": [],
                "evidence_refs": evidence,
                "task_kind": "safety_gate" if high else "objective_answer",
                "required_roles": ["proposer", "judge"],
                "risk_level": "high" if high else "low",
                "risk_categories": ["production_safety"] if high else [],
            }
        if stage == "claim-normalization" or stage.startswith("claim-normalization:"):
            claims = json.loads(prompt.split("INPUT\n", 1)[1])["claims"]
            return {
                "aliases": [
                    {
                        "source_claim_id": claim["source_claim_id"],
                        "canonical_text": claim["text"],
                    }
                    for claim in claims
                ]
            }
        if stage == "evidence-extraction" or stage.startswith("evidence-extraction:"):
            extraction_input = json.loads(prompt.split("INPUT\n", 1)[1])
            source_id = extraction_input["task_contract"]["evidence_refs"][0]
            return {
                "extractor_label": "",
                "claims": [
                    {
                        "id": "",
                        "text": "The supplied source supports the objective",
                        "evidence_refs": [source_id],
                        "reported_confidence": 0.8,
                        "position": "support",
                        "blocker": False,
                        "load_bearing": True,
                        "testable": True,
                        "assumptions": [],
                        "falsifiers": ["The source states the opposite"],
                    }
                ],
                "source_coverage": {source_id: ["source-support"]},
                "conflicts": [],
                "evidence_gaps": [],
            }
        if stage == "hypotheses" or stage.startswith("hypotheses:"):
            return {
                "label": "",
                "recommendation": "Proceed with the evidence-backed option",
                "claims": [
                    {
                        "id": "",
                        "text": "Resolve task with supplied evidence",
                        "evidence_refs": [evidence_id],
                        "reported_confidence": 0.8,
                        "position": "support",
                        "blocker": False,
                        "load_bearing": True,
                        "testable": False,
                        "assumptions": [],
                        "falsifiers": [],
                    }
                ],
                "method": "independent",
                "assumptions": [],
                "predicted_observations": [],
                "risks": [],
                "blockers": [],
            }
        if stage == "candidate-peer-review" or stage.startswith(
            "candidate-peer-review:"
        ):
            return {
                "order": ["Candidate A"],
                "action": "select",
                "selected_candidate": "Candidate A",
                "accepted_claim_ids": [],
                "blockers": [],
                "scores": [],
                "rationale": ["Verified"],
                "reported_confidence": 0.8,
            }
        if stage == "implementation-judging" or stage.startswith(
            "implementation-judging:"
        ):
            labels = __import__("re").findall(r'"label": "(Candidate [A-Z])"', prompt)
            selected = sorted(set(labels))[0] if labels else "Candidate A"
            return {
                "order": labels,
                "action": self.judge_action,
                "selected_candidate": (
                    selected if self.judge_action in {"select", "integrate"} else None
                ),
                "accepted_claim_ids": [],
                "blockers": [],
                "scores": [
                    {
                        "candidate_label": selected,
                        "criterion_id": "AC-001",
                        "score": 5,
                        "reason": "Verified",
                        "evidence_refs": [],
                    }
                ],
                "rationale": ["Engine receipts are green."],
                "reported_confidence": 0.9,
            }
        if stage == "judging" or stage.startswith("judging:"):
            return {
                "order": [],
                "action": "synthesize",
                "selected_candidate": None,
                "accepted_claim_ids": [],
                "blockers": [],
                "scores": [],
                "rationale": ["The ledger and evidence support the decision."],
                "reported_confidence": 0.9,
            }
        if stage == "verification" or stage.startswith("verification:"):
            status = (
                self.verification_sequence.pop(0)
                if self.verification_sequence
                else "supported"
            )
            return {
                "step_id": "",
                "claim_id": "",
                "kind": "evidence_entailment",
                "status": status,
                "executor": "",
                "output_sha256": "",
                "observation": (
                    "Evidence supports the claim."
                    if status == "supported"
                    else "Evidence falsifies the claim."
                    if status == "falsified"
                    else "Evidence is inconclusive."
                ),
                "evidence_refs": evidence,
                "command_exit_code": None,
                "timed_out": False,
            }
        if stage == "minority-defense" or stage.startswith("minority-defense:"):
            return {
                "claim_id": "",
                "advocate_label": "",
                "strongest_case": "The minority may be correct.",
                "falsification_attempt": "Evidence did not falsify it.",
                "status": "survived",
                "evidence_refs": evidence,
            }
        if stage == "majority-self-challenge" or stage.startswith(
            "majority-self-challenge:"
        ):
            return {
                "claim_id": "",
                "majority_labels": [],
                "disconfirmation_condition": "A reproducible falsifying receipt.",
                "evidence_refs": evidence,
            }
        if stage == "judge-revision" or stage.startswith("judge-revision:"):
            return {
                "decision": "Revised with validator evidence.",
                "rationale": ["Addressed the material blocker."],
                "dissent": [],
                "confidence": 0.6,
                "blockers": [],
                "action": "synthesize",
                "selected_candidate": None,
                "integration_plan": [],
                "selected_contribution_ids": [],
                "acceptance_reasons": {},
                "majority": [],
                "minority": [],
                "unresolved": [],
            }
        if stage in {"validation", "completion-review"} or stage.startswith(
            ("validation:", "completion-review:")
        ):
            status = (
                self.validation_sequence.pop(0)
                if self.validation_sequence
                else "blocker_free"
            )
            return {
                "label": "",
                "family": "",
                "status": status,
                "verdict_sha256": "",
                "blockers": [] if status == "blocker_free" else ["Material blocker"],
                "checked_claim_ids": [],
                "evidence_refs": evidence,
                "notes": [],
            }
        raise AssertionError(f"unhandled fake stage {stage}")

    async def ask(
        self,
        *,
        model,
        prompt,
        stage,
        participant,
        **kwargs,
    ):
        if self.budget:
            self.budget.consume(stage, model)
        self.prompts.append((stage, model, prompt))
        if stage == "health":
            return "OK", {"input_tokens": 1, "output_tokens": 1}
        base_stage = stage.split(":", 1)[0]
        if (
            base_stage in self.malformed_once
            and base_stage not in self.seen_malformed
            and "schema-repair" not in stage
        ):
            self.seen_malformed.add(base_stage)
            return "{not json", {}
        data = self.response(stage, prompt)
        return json.dumps(data), {"input_tokens": 10, "output_tokens": 10}


@pytest.fixture
def tmp_state_root(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    return root


@pytest.fixture
def fake_codex(tmp_path):
    executable = tmp_path / "fake-codex"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '{\"type\":\"result\",\"result\":\"ok\"}'\n")
    executable.chmod(0o755)
    return executable


@pytest.fixture
def fake_settings():
    return SimpleNamespace(
        base_url="http://proxy.invalid",
        api_key="fake-proxy-key-123456",
        exact_secrets={"fake-proxy-key-123456"},
    )


@pytest.fixture(autouse=True)
def reset_fake_transport():
    FakeTransport.reset()
    yield
    FakeTransport.reset()


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in inspect.signature(pyfuncitem.obj).parameters
    }
    asyncio.run(pyfuncitem.obj(**kwargs))
    return True
