from __future__ import annotations

import ast
import asyncio
import ipaddress
import json
import re
import socket
import subprocess
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from pypdf import PdfReader

from artifacts import EvidenceInventory, RunStore, sha256_text
from contracts import (
    Claim,
    VerificationPlan,
    VerificationReceipt,
    VerificationStep,
)


MAX_SOURCE_BYTES = 5 * 1024 * 1024
ALLOWED_MIME = {
    "text/plain",
    "text/html",
    "text/markdown",
    "text/csv",
    "application/json",
    "application/pdf",
    "application/xml",
    "text/xml",
}
SENSITIVE_QUERY = re.compile(r"(?i)(?:key|token|secret|signature|credential|auth)")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.rows.append(data.strip())


def _public_addresses(host: str) -> list[str]:
    try:
        rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RuntimeError(f"source DNS resolution failed: {host}") from exc
    addresses = sorted({row[4][0] for row in rows})
    if not addresses:
        raise RuntimeError("source hostname has no addresses")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise RuntimeError(f"source resolves to a non-public address: {value}")
    return addresses


def validate_source_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise RuntimeError("sources must use HTTPS")
    if parsed.username or parsed.password:
        raise RuntimeError("source URL must not contain user information")
    if not parsed.hostname or parsed.port not in {None, 443}:
        raise RuntimeError("source URL must use a hostname on port 443")
    if any(SENSITIVE_QUERY.search(key) for key, _ in parse_qsl(parsed.query)):
        raise RuntimeError("source URL contains a credential-shaped query parameter")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        _public_addresses(parsed.hostname)
    else:
        if not address.is_global:
            raise RuntimeError("source IP must be globally routable")


def _validate_connected_peer(response: httpx.Response) -> None:
    """Reject DNS rebinding when the transport exposes its connected peer."""

    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return
    peer = stream.get_extra_info("server_addr")
    if not peer:
        return
    address = ipaddress.ip_address(peer[0] if isinstance(peer, tuple) else peer)
    if not address.is_global:
        raise RuntimeError(f"source connected to a non-public address: {address}")


def _extract_source(data: bytes, mime: str) -> str:
    if mime == "application/pdf":
        reader = PdfReader(BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    text = data.decode("utf-8", errors="replace")
    if mime == "text/html":
        parser = _TextExtractor()
        parser.feed(text)
        return "\n".join(parser.rows)
    if mime == "application/json":
        return json.dumps(json.loads(text), indent=2, sort_keys=True)
    return text


async def fetch_source(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str, str, int]:
    validate_source_url(url)
    owned = client is None
    http = client or httpx.AsyncClient(
        follow_redirects=False,
        timeout=10,
        headers={"User-Agent": "ccycouncil-v4/0.4.1"},
        cookies=None,
    )
    current = url
    try:
        async with asyncio.timeout(10):
            for _ in range(4):
                validate_source_url(current)
                async with http.stream("GET", current) as response:
                    _validate_connected_peer(response)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError("source redirect is missing Location")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    raw_mime = response.headers.get("content-type", "").split(";", 1)[0]
                    mime = raw_mime.lower().strip()
                    if mime not in ALLOWED_MIME:
                        raise RuntimeError(f"unsupported source content type: {mime}")
                    advertised = response.headers.get("content-length")
                    if advertised and int(advertised) > MAX_SOURCE_BYTES:
                        raise RuntimeError("source exceeds 5 MiB")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_SOURCE_BYTES:
                            raise RuntimeError("source exceeds 5 MiB")
                    return (
                        _extract_source(bytes(content), mime),
                        current,
                        mime,
                        len(content),
                    )
            raise RuntimeError("source exceeds three redirects")
    except TimeoutError as exc:
        raise RuntimeError("source fetch exceeded ten seconds") from exc
    finally:
        if owned:
            await http.aclose()


async def snapshot_sources(
    urls: list[str],
    inventory: EvidenceInventory,
    store: RunStore,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    for index, url in enumerate(urls, 1):
        try:
            text, final_url, mime, size = await fetch_source(url, client=client)
            ref = inventory.add(
                url,
                text,
                kind="pdf" if mime == "application/pdf" else "source",
                priority=85,
            )
            ref.retrieved_at = datetime.now(timezone.utc)
            ref.final_url = final_url
            ref.content_type = mime
            store.append_event(
                "source_fetched",
                index=index,
                evidence_ref=ref.id,
                final_url=final_url,
                content_type=mime,
                size_bytes=size,
            )
        except BaseException as exc:
            store.append_event(
                "source_failed", index=index, url=url, error=str(exc)[:500]
            )
            raise


def build_verification_plan(
    claims: list[Claim],
    verify_commands: list[str],
    preferred_kinds: list[str] | None = None,
    id_namespace: str = "plan",
) -> VerificationPlan:
    steps: list[VerificationStep] = []
    commands = iter(verify_commands)
    kinds = iter(preferred_kinds or [])
    namespace = re.sub(r"[^a-zA-Z0-9]+", "-", id_namespace).strip("-")[:24] or "plan"
    for claim in claims:
        if not claim.testable and claim.evidence_refs:
            continue
        command = next(commands, "")
        if command:
            kind = "command"
            value = command
        elif preferred := next(kinds, ""):
            kind = preferred
            value = claim.text
        elif claim.falsifiers:
            kind = "counterexample"
            value = claim.falsifiers[0]
        else:
            kind = "evidence_entailment"
            value = claim.text
        steps.append(
            VerificationStep(
                id=(
                    "VS-"
                    + namespace
                    + "-"
                    + (
                        claim.id.removeprefix("C-")[:8]
                        if claim.id
                        else sha256_text(claim.text)[:8]
                    )
                    + f"-{len(steps) + 1:02d}"
                ),
                claim_id=claim.id,
                kind=kind,
                instruction=f"Verify claim: {claim.text}",
                expected_observation=claim.text
                if kind == "evidence_entailment"
                else "",
                falsifying_observation=(
                    claim.falsifiers[0] if claim.falsifiers else ""
                ),
                executor_input=value,
                evidence_refs=claim.evidence_refs,
            )
        )
    for command in commands:
        steps.append(
            VerificationStep(
                id=f"VS-{namespace}-run-{len(steps) + 1:02d}",
                claim_id="",
                kind="command",
                instruction="Run the user-authorized deterministic verification command.",
                executor_input=command,
            )
        )
    return VerificationPlan(steps=steps)


def run_command_verifier(
    step: VerificationStep,
    *,
    cwd: Path,
    timeout: int = 120,
) -> VerificationReceipt:
    safe_env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_PROXY": "*",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
    }
    try:
        result = subprocess.run(
            step.executor_input,
            cwd=cwd,
            env=safe_env,
            shell=True,
            executable="/bin/zsh",
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr)[:100_000]
        status = "supported" if result.returncode == 0 else "inconclusive"
        ambiguity = None if result.returncode == 0 else (
            f"command exited {result.returncode}; exit status alone is not falsification"
        )
        if step.expected_observation and step.expected_observation not in output:
            status = "partially_supported" if result.returncode == 0 else "inconclusive"
            ambiguity = "expected observation was absent"
        if step.falsifying_observation and step.falsifying_observation in output:
            status = "falsified"
            ambiguity = None
        return VerificationReceipt(
            step_id=step.id,
            claim_id=step.claim_id,
            kind=step.kind,
            status=status,
            executor="engine-command",
            output_sha256=sha256_text(output),
            observation=output,
            evidence_refs=step.evidence_refs,
            command_exit_code=result.returncode,
            expected_observation=step.expected_observation,
            falsifying_observation=step.falsifying_observation,
            resulting_stance=(
                "support" if status in {"supported", "partially_supported"}
                else "oppose" if status == "falsified" else "uncertain"
            ),
            deterministic=True,
            ambiguity=ambiguity,
        )
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or ""))[:100_000]
        return VerificationReceipt(
            step_id=step.id,
            claim_id=step.claim_id,
            kind=step.kind,
            status="inconclusive",
            executor="engine-command",
            output_sha256=sha256_text(output),
            observation="command timed out",
            evidence_refs=step.evidence_refs,
            timed_out=True,
            deterministic=True,
            ambiguity="command timed out",
            expected_observation=step.expected_observation,
            falsifying_observation=step.falsifying_observation,
        )


def run_calculation_verifier(step: VerificationStep) -> VerificationReceipt:
    try:
        observation = calculate(step.executor_input)
        status = (
            "supported"
            if not step.expected_observation
            or observation == step.expected_observation.strip()
            else "falsified"
        )
    except BaseException as exc:
        observation = str(exc)
        status = "inconclusive"
    return VerificationReceipt(
        id=step.id,
        step_id=step.id,
        claim_id=step.claim_id,
        kind=step.kind,
        status=status,
        executor="engine-calculation",
        output_sha256=sha256_text(observation),
        observation=observation,
        evidence_refs=step.evidence_refs,
        expected_observation=step.expected_observation,
        falsifying_observation=step.falsifying_observation,
        resulting_stance=(
            "support"
            if status == "supported"
            else "oppose"
            if status == "falsified"
            else "uncertain"
        ),
        deterministic=True,
    )


def run_evidence_verifier(
    step: VerificationStep,
    evidence_by_ref: dict[str, str],
) -> VerificationReceipt:
    """Conservative deterministic verifier for immutable evidence.

    Exact falsifiers are decisive. Positive lexical coverage can support an
    invariant or explicit expected observation; otherwise the engine refuses to
    manufacture entailment and returns inconclusive for a model verifier.
    """

    text = "\n".join(evidence_by_ref.get(ref, "") for ref in step.evidence_refs)
    lowered = re.sub(r"\s+", " ", text.lower())
    expected = re.sub(r"\s+", " ", step.expected_observation.lower()).strip()
    falsifier = re.sub(r"\s+", " ", step.falsifying_observation.lower()).strip()
    if falsifier and falsifier in lowered:
        status = "falsified"
        observation = f"falsifying observation found: {step.falsifying_observation}"
    elif expected and expected in lowered:
        status = "supported"
        observation = f"expected observation found: {step.expected_observation}"
    elif step.kind in {"source", "evidence_entailment"} and text:
        expected_tokens = {
            token
            for token in re.findall(
                r"[a-z0-9]+", expected or step.executor_input.lower()
            )
            if len(token) > 3
        }
        evidence_tokens = set(re.findall(r"[a-z0-9]+", lowered))
        coverage = (
            len(expected_tokens & evidence_tokens) / len(expected_tokens)
            if expected_tokens
            else 0
        )
        if coverage >= 0.85:
            status = "supported"
            observation = f"immutable evidence lexical coverage={coverage:.3f}"
        else:
            status = "inconclusive"
            observation = f"immutable evidence lexical coverage={coverage:.3f}"
    else:
        status = "inconclusive"
        observation = "immutable evidence does not decide the claim"
    return VerificationReceipt(
        id=step.id,
        step_id=step.id,
        claim_id=step.claim_id,
        kind=step.kind,
        status=status,
        executor="engine-evidence",
        output_sha256=sha256_text(observation + "\n" + text),
        observation=observation,
        evidence_refs=step.evidence_refs,
        expected_observation=step.expected_observation,
        falsifying_observation=step.falsifying_observation,
        resulting_stance=(
            "support"
            if status == "supported"
            else "oppose"
            if status == "falsified"
            else "uncertain"
        ),
        deterministic=True,
    )


_ALLOWED_AST = (
    ast.Expression,
    ast.Constant,
    ast.UnaryOp,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)


def calculate(expression: str) -> str:
    tree = ast.parse(expression, mode="eval")
    if any(not isinstance(node, _ALLOWED_AST) for node in ast.walk(tree)):
        raise RuntimeError("calculation contains a disallowed expression")
    value = eval(compile(tree, "<verification>", "eval"), {"__builtins__": {}}, {})
    if not isinstance(value, (int, float)) or not math_is_finite(value):
        raise RuntimeError("calculation did not produce a finite number")
    return str(value)


def math_is_finite(value: int | float) -> bool:
    return value == value and value not in {float("inf"), float("-inf")}
