from __future__ import annotations

from io import BytesIO

import httpx
import pytest
from pypdf import PdfWriter

from contracts import Claim, VerificationStep
from verification import (
    MAX_SOURCE_BYTES,
    build_verification_plan,
    calculate,
    command_shell,
    fetch_source,
    run_calculation_verifier,
    run_command_verifier,
    run_evidence_verifier,
    validate_source_url,
)
from verification import _extract_source


def test_command_shell_falls_back_to_posix_sh(monkeypatch):
    locations = {"zsh": None, "sh": "/bin/sh"}
    monkeypatch.setattr(
        "verification.shutil.which", lambda name: locations.get(name)
    )
    assert command_shell() == "/bin/sh"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/a",
        "https://test-user:test-password@example.invalid/a",
        "https://127.0.0.1/a",
        "https://169.254.169.254/a",
        "https://example.invalid:8443/a",
        "https://example.invalid/a?api_key=test-value",
    ],
)
def test_source_url_rejects_unsafe_authority(url, monkeypatch):
    monkeypatch.setattr("verification._public_addresses", lambda host: ["8.8.8.8"])
    with pytest.raises(RuntimeError):
        validate_source_url(url)


@pytest.mark.asyncio
async def test_https_source_redirect_revalidation_and_html_extraction(monkeypatch):
    monkeypatch.setattr("verification._public_addresses", lambda host: ["8.8.8.8"])

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><script>secret()</script><body><h1>Primary source</h1></body></html>",
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        text, final_url, mime, size = await fetch_source(
            "https://example.invalid/start", client=client
        )
    assert "Primary source" in text
    assert "secret()" not in text
    assert final_url.endswith("/final")
    assert mime == "text/html"
    assert size > 0


@pytest.mark.asyncio
async def test_source_rejects_mime_and_size(monkeypatch):
    monkeypatch.setattr("verification._public_addresses", lambda host: ["8.8.8.8"])

    async def run(response):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response)
        ) as client:
            return await fetch_source("https://example.invalid/a", client=client)

    with pytest.raises(RuntimeError, match="content type"):
        await run(
            httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"x",
            )
        )
    with pytest.raises(RuntimeError, match="5 MiB"):
        await run(
            httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"x" * (MAX_SOURCE_BYTES + 1),
            )
        )


def test_command_verifier_scrubs_environment_hashes_and_times_out(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "secret")
    step = VerificationStep(
        id="VS-001",
        claim_id="C-1",
        kind="command",
        instruction="check",
        executor_input='test -z "$SHOULD_NOT_LEAK"',
        shell=True,
    )
    receipt = run_command_verifier(step, cwd=tmp_path)
    assert receipt.status == "supported"
    assert receipt.output_sha256
    timeout = step.model_copy(update={"executor_input": "sleep 2"})
    timed = run_command_verifier(timeout, cwd=tmp_path, timeout=1)
    assert timed.status == "inconclusive"
    assert timed.timed_out


def test_command_verifier_defaults_to_argv_and_shell_is_explicit(tmp_path):
    marker = tmp_path / "should-not-exist"
    argv_step = VerificationStep(
        id="VS-ARGV",
        claim_id="C-1",
        kind="command",
        instruction="check",
        executor_input=f'printf %s "hello; touch {marker}"',
    )
    receipt = run_command_verifier(argv_step, cwd=tmp_path)
    assert receipt.status == "supported"
    assert not marker.exists()

    shell_step = argv_step.model_copy(
        update={"id": "VS-SHELL", "executor_input": "printf hello | grep hello", "shell": True}
    )
    shell_receipt = run_command_verifier(shell_step, cwd=tmp_path)
    assert shell_receipt.status == "supported"


def test_command_verifier_uses_configured_safe_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "verify-local"
    command.write_text("#!/bin/sh\nprintf configured")
    command.chmod(0o755)
    monkeypatch.setenv("REASON_ASSEMBLY_VERIFY_PATH", str(bin_dir))
    step = VerificationStep(
        id="VS-PATH",
        claim_id="C-1",
        kind="command",
        instruction="check",
        executor_input="verify-local",
    )
    assert run_command_verifier(step, cwd=tmp_path).observation == "configured"


def test_calculation_allowlist_and_verification_plan():
    assert calculate("(2 + 3) * 4") == "20"
    with pytest.raises(RuntimeError):
        calculate("__import__('os').system('id')")
    plan = build_verification_plan(
        [
            Claim(
                id="C-1",
                text="The invariant holds",
                testable=True,
                falsifiers=["A counterexample"],
            )
        ],
        [],
    )
    assert plan.steps[0].kind == "counterexample"
    run_plan = build_verification_plan([], ["true"], id_namespace="run")
    assert len(run_plan.steps) == 1
    assert run_plan.steps[0].kind == "command"
    assert run_plan.steps[0].claim_id == ""
    second = build_verification_plan(
        [
            Claim(
                id="C-2",
                text="A second invariant",
                testable=True,
            )
        ],
        [],
    )
    assert plan.steps[0].id != second.steps[0].id
    repeated = build_verification_plan(
        [
            Claim(
                id="C-1",
                text="The invariant holds",
                testable=True,
                falsifiers=["A counterexample"],
            )
        ],
        [],
        id_namespace="minority-C-1",
    )
    assert plan.steps[0].id != repeated.steps[0].id
    receipt = run_calculation_verifier(
        VerificationStep(
            id="VS-002",
            claim_id="C-2",
            kind="calculation",
            instruction="calculate",
            executor_input="2 + 2",
            expected_observation="4",
        )
    )
    assert receipt.status == "supported"


def test_immutable_evidence_verifier_prefers_falsification():
    step = VerificationStep(
        id="VS-003",
        claim_id="C-3",
        kind="invariant",
        instruction="check",
        expected_observation="all rows are valid",
        falsifying_observation="invalid row 7",
        evidence_refs=["E-1"],
    )
    receipt = run_evidence_verifier(step, {"E-1": "Audit output: invalid row 7"})
    assert receipt.status == "falsified"
    assert receipt.resulting_stance == "oppose"
    assert receipt.executor == "engine-evidence"


def test_json_and_pdf_source_extraction_are_bounded_text():
    assert '"answer": 42' in _extract_source(b'{"answer": 42}', "application/json")
    assert "GET,yes,yes" in _extract_source(
        b"method,safe,idempotent\nGET,yes,yes\n",
        "text/csv",
    )
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    assert isinstance(_extract_source(output.getvalue(), "application/pdf"), str)


@pytest.mark.asyncio
async def test_source_rejects_rebound_connected_peer(monkeypatch):
    monkeypatch.setattr("verification._public_addresses", lambda host: ["8.8.8.8"])

    class Stream:
        def get_extra_info(self, key):
            assert key == "server_addr"
            return ("127.0.0.1", 443)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="x",
                extensions={"network_stream": Stream()},
            )
        )
    ) as client:
        with pytest.raises(RuntimeError, match="non-public"):
            await fetch_source("https://example.invalid/a", client=client)
