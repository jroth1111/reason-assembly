from __future__ import annotations

from types import SimpleNamespace

import pytest

import reason_assembly
from conftest import FakeTransport
from protocols import CouncilRequest, run_council
from contracts import RouteRecord


def common_args(run_id: str):
    return SimpleNamespace(
        run_id=run_id,
        budget="adaptive",
        context=[],
        source=[],
        verify_command=[],
        route=[],
        max_calls=None,
        quorum_grace=0,
        json=False,
    )


@pytest.mark.asyncio
async def test_replay_and_revisit_create_children_with_full_ancestry(
    tmp_path, fake_settings, monkeypatch
):
    parent = await run_council(
        CouncilRequest(
            mode="decide",
            prompt="Resolve task with supplied evidence.",
            budget_requested="quick",
            contexts=[("note", "supporting evidence")],
            quorum_grace=0,
        ),
        state=tmp_path,
        settings=fake_settings,
        transport_factory=FakeTransport,
    )
    monkeypatch.setattr(reason_assembly, "STATE", tmp_path)

    async def fake_run(request):
        return await run_council(
            request,
            state=tmp_path,
            settings=fake_settings,
            transport_factory=FakeTransport,
        )

    monkeypatch.setattr(reason_assembly, "run_council", fake_run)
    replay = await reason_assembly.replay_command(common_args(parent.run_id))
    assert replay.run_id != parent.run_id
    assert replay.manifest.mode == "replay"
    assert replay.manifest.parent_run_id == parent.run_id
    assert replay.manifest.ancestry_relation == "replay"

    revisit_args = common_args(parent.run_id)
    revisit_args.correction = "The supporting note is now authoritative."
    revisit = await reason_assembly.revisit_command(revisit_args)
    assert revisit.manifest.mode == "revisit"
    assert revisit.manifest.parent_run_id == parent.run_id
    store, _ = reason_assembly.load_v4_run(revisit.run_id)
    assert store._target("revisit-report.json").exists()


def test_route_substitutions_align_by_role_not_list_position():
    parent = SimpleNamespace(
        routes=[
            RouteRecord(
                label="Proposer route 1",
                model="a",
                family="fa",
                effort="medium",
                role="proposer",
            ),
            RouteRecord(
                label="Judge",
                model="j",
                family="fj",
                effort="medium",
                role="judge",
            ),
        ]
    )
    current = SimpleNamespace(
        routes=[
            parent.routes[0],
            RouteRecord(
                label="Proposer route 2",
                model="b",
                family="fb",
                effort="medium",
                role="proposer",
            ),
            parent.routes[1],
        ]
    )
    assert reason_assembly.route_substitutions(parent, current) == [
        {
            "role": "proposer",
            "prior_model": "none",
            "replacement_model": "b",
        }
    ]


def test_evidence_extractor_outcome_subjects_link_to_normalized_ledger_claims():
    subjects = reason_assembly.extraction_subject_ids(
        {
            "claims": [
                {
                    "id": "C-extracted",
                    "acceptance_ids": ["AC-1"],
                    "evidence_refs": ["E-1"],
                }
            ]
        },
        [
            {
                "claim": {
                    "id": "C-normalized",
                    "acceptance_ids": ["AC-1"],
                    "evidence_refs": ["E-1"],
                }
            },
            {
                "claim": {
                    "id": "C-unrelated",
                    "acceptance_ids": ["AC-2"],
                    "evidence_refs": ["E-1"],
                }
            },
        ],
    )
    assert subjects == {"C-extracted", "C-normalized"}


@pytest.mark.asyncio
async def test_outcome_stats_and_show_private_boundary(
    tmp_path, fake_settings, monkeypatch
):
    result = await run_council(
        CouncilRequest(
            mode="decide",
            prompt="Resolve task with supplied evidence.",
            budget_requested="quick",
            quorum_grace=0,
        ),
        state=tmp_path,
        settings=fake_settings,
        transport_factory=FakeTransport,
    )
    monkeypatch.setattr(reason_assembly, "STATE", tmp_path)
    outcome = reason_assembly.outcome_command(
        SimpleNamespace(
            run_id=result.run_id,
            status="confirmed",
            notes="Observed in production",
            evidence=["verdict.json"],
            claim=[],
            criterion=[],
            component=[],
        )
    )
    assert outcome.status == "confirmed"
    store, manifest = reason_assembly.load_v4_run(result.run_id)
    assert "outcome.json" in manifest.artifacts
    stats = reason_assembly.stats_command()
    assert stats["runs_with_observed_outcomes"] == 1
    assert stats["routing_changed"] is False
    assert stats["mode"]["decide"]["observed_accuracy"] == 1.0
    assert stats["role"]["judge"]["observed_accuracy"] == 1.0
    with pytest.raises(RuntimeError, match="private"):
        reason_assembly.show_command(
            SimpleNamespace(
                run_id=result.run_id,
                artifact="private/identity-map.json",
                json=False,
            )
        )
