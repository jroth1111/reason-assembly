from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from pydantic import BaseModel

from contracts import EvidenceRef
from state_compat import exclusive_state_lock


SECRET_PATTERNS = [
    re.compile(r"(?i)\b(?:bearer|authorization)\s*[:=]?\s+([A-Za-z0-9._~+/=-]{12,})"),
    re.compile(
        r"(?i)\b(?:api[-_ ]?key|access[-_ ]?token|client[-_ ]?secret|secret[-_ ]?key)"
        r"\s*[:=]\s*[\"']?([^\s\"']{8,})"
    ),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SecretGuard:
    def __init__(self, exact_secrets: Iterable[str] = ()):
        self.exact = sorted(
            {
                str(secret)
                for secret in exact_secrets
                if secret and len(str(secret)) >= 6
            },
            key=len,
            reverse=True,
        )

    def redact_text(self, value: str) -> str:
        out = value
        for secret in self.exact:
            out = out.replace(secret, "[REDACTED_SECRET]")
        for pattern in SECRET_PATTERNS:
            if "PRIVATE KEY" in pattern.pattern:
                out = pattern.sub("[REDACTED_PRIVATE_KEY]", out)
            elif pattern.groups:
                out = pattern.sub(
                    lambda match: match.group(0).replace(
                        match.group(1), "[REDACTED_SECRET]"
                    ),
                    out,
                )
            else:
                out = pattern.sub("[REDACTED_SECRET]", out)
        return out

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key).lower().replace("-", "_")
                sensitive_key = bool(
                    re.search(
                        r"(?:^|_)(?:api_key|access_token|refresh_token|"
                        r"secret|authorization|credential)(?:$|_)",
                        normalized_key,
                    )
                    or normalized_key in {"token", "bearer"}
                )
                if sensitive_key:
                    cleaned[str(key)] = "[REDACTED_SECRET]"
                else:
                    cleaned[str(key)] = self.redact(item)
            return cleaned
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def findings(self, value: str) -> list[str]:
        findings: list[str] = []
        for secret in self.exact:
            if secret in value:
                findings.append("exact proxy credential")
        scanned = re.sub(
            r"(?i)\[(?:REDACTED(?:_SECRET|_PRIVATE_KEY)?|PLACEHOLDER)\]|"
            r"\b(?:example|dummy|changeme|your[-_ ]?key[-_ ]?here)\b",
            "x",
            value,
        )
        for pattern in SECRET_PATTERNS:
            if pattern.search(scanned):
                findings.append(pattern.pattern)
        return sorted(set(findings))

    def reject_added_credentials(self, patch: str) -> None:
        added = "\n".join(
            line[1:]
            for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        findings = self.findings(added)
        if findings:
            raise RuntimeError(
                "generated diff contains a possible new credential: "
                + ", ".join(findings)
            )


class RunStore:
    """Private, atomic, sanitized schema-v4 artifact storage."""

    def __init__(self, root: Path, run_id: str, guard: SecretGuard):
        self.root = root.expanduser().resolve()
        self.run_id = run_id
        self.path = self.root / "runs" / run_id
        self.guard = guard
        self.path.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(self.path, 0o700)
        (self.path / "private").mkdir(mode=0o700)

    @classmethod
    def create_unique(
        cls,
        root: Path,
        guard: SecretGuard,
        run_id_factory: Callable[[], str],
        *,
        collision_roots: Iterable[Path] = (),
        max_attempts: int = 16,
    ) -> "RunStore":
        root = root.expanduser().resolve()
        visible_roots = {
            candidate.expanduser().resolve() for candidate in collision_roots
        }
        visible_roots.add(root)
        with exclusive_state_lock(root):
            for _ in range(max_attempts):
                run_id = run_id_factory()
                if any(
                    os.path.lexists(candidate / "runs" / run_id)
                    for candidate in visible_roots
                ):
                    continue
                try:
                    return cls(root, run_id, guard)
                except FileExistsError:
                    continue
        raise RuntimeError(
            f"could not reserve a unique run ID after {max_attempts} attempts"
        )

    @classmethod
    def open_existing(cls, root: Path, run_id: str, guard: SecretGuard) -> "RunStore":
        instance = object.__new__(cls)
        instance.root = root.expanduser().resolve()
        instance.run_id = run_id
        instance.path = instance.root / "runs" / run_id
        instance.guard = guard
        if not instance.path.is_dir():
            raise RuntimeError(f"run not found: {run_id}")
        return instance

    def _target(self, relative: str) -> Path:
        target = (self.path / relative).resolve()
        if self.path != target and self.path not in target.parents:
            raise RuntimeError("artifact path escapes the run directory")
        return target

    def _write_target(self, relative: str) -> Path:
        target = self._target(relative)
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(target.parent, 0o700)
        return target

    def write_bytes(self, relative: str, value: bytes) -> Path:
        target = self._write_target(relative)
        temp = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            os.chmod(target, 0o600)
        finally:
            if temp.exists():
                temp.unlink()
        return target

    def write_text(self, relative: str, value: str) -> Path:
        sanitized = self.guard.redact_text(value)
        return self.write_bytes(relative, sanitized.encode("utf-8"))

    def write_json(self, relative: str, value: Any) -> Path:
        def jsonable(item: Any) -> Any:
            if isinstance(item, BaseModel):
                return item.model_dump(mode="json")
            if isinstance(item, dict):
                return {str(key): jsonable(child) for key, child in item.items()}
            if isinstance(item, (list, tuple)):
                return [jsonable(child) for child in item]
            if isinstance(item, Path):
                return str(item)
            if isinstance(item, datetime):
                return item.isoformat()
            return item

        value = jsonable(value)
        sanitized = self.guard.redact(value)
        encoded = json.dumps(sanitized, indent=2, sort_keys=True, ensure_ascii=False)
        return self.write_text(relative, encoded + "\n")

    def append_event(self, kind: str, **data: Any) -> None:
        target = self._write_target("events.jsonl")
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **data,
        }
        line = json.dumps(self.guard.redact(record), sort_keys=True, ensure_ascii=False)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, 0o600)

    def read_json(self, relative: str) -> Any:
        return json.loads(self._target(relative).read_text())

    def artifact_names(self) -> list[str]:
        return sorted(
            str(path.relative_to(self.path))
            for path in self.path.rglob("*")
            if path.is_file()
        )


class EvidenceInventory:
    def __init__(self) -> None:
        self.refs: list[EvidenceRef] = []
        self.contents: dict[str, str] = {}

    def add(
        self,
        source: str,
        content: str,
        *,
        kind: str = "file",
        priority: int = 50,
    ) -> EvidenceRef:
        digest = sha256_text(content)
        stable_id = f"E-{digest[:12]}"
        existing = next((item for item in self.refs if item.id == stable_id), None)
        if existing:
            return existing
        ref = EvidenceRef(
            id=stable_id,
            source=source,
            sha256=digest,
            kind=kind,
            size_bytes=len(content.encode("utf-8")),
            priority=priority,
        )
        self.refs.append(ref)
        self.contents[stable_id] = content
        return ref

    def snapshot(self, store: RunStore) -> None:
        for ref in self.refs:
            store.write_text(f"evidence/{ref.id}.txt", self.contents[ref.id])
        store.write_json("evidence/inventory.json", self.refs)

    def _summary_block(self, ref: EvidenceRef, char_budget: int) -> str:
        header = (
            f"\n[EVIDENCE SUMMARY {ref.id} source={ref.source} "
            f"sha256={ref.sha256} bytes={ref.size_bytes}]\n"
        )
        footer = "\n[END COVERAGE-CHECKED SUMMARY; FULL SOURCE IN RUN ARTIFACT]\n"
        if len(header) + len(footer) > char_budget:
            return ""
        lines = [
            (index, line.strip())
            for index, line in enumerate(self.contents[ref.id].splitlines(), 1)
            if line.strip()
        ]
        important = re.compile(
            r"(?i)\b(?:must|never|only|without|do not|preserve|required?|"
            r"acceptance|verify|risk|blocker|decision|error|failed?)\b"
        )
        ranked = sorted(
            lines,
            key=lambda item: (
                0 if important.search(item[1]) else 1,
                0 if re.match(r"^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)", item[1]) else 1,
                min(item[0] - 1, max(0, len(lines) - item[0])),
                item[0],
            ),
        )
        selected: list[tuple[int, str]] = []
        used = len(header) + len(footer)
        for line_number, line in ranked:
            row = f"L{line_number} [{sha256_text(line)[:12]}] {line}\n"
            if used + len(row) > char_budget:
                continue
            selected.append((line_number, row))
            used += len(row)
        selected.sort()
        return header + "".join(row for _, row in selected) + footer

    def packed(
        self, token_budget: int, reserve_tokens: int = 0
    ) -> tuple[str, list[str]]:
        available = max(256, token_budget - reserve_tokens)
        char_budget = available * 4
        ordered = sorted(self.refs, key=lambda ref: (-ref.priority, ref.id))
        blocks = [
            (
                ref,
                f"\n[EVIDENCE {ref.id} source={ref.source} sha256={ref.sha256}]\n"
                + self.contents[ref.id],
            )
            for ref in ordered
        ]
        if sum(len(block) for _, block in blocks) <= char_budget:
            return "".join(block for _, block in blocks), [ref.id for ref, _ in blocks]

        summary_reserve = min(
            char_budget // 3,
            max(512, min(char_budget // 5, 384 * len(blocks))),
        )
        full_budget = char_budget - summary_reserve
        chunks: list[str] = []
        included: list[str] = []
        omitted: list[EvidenceRef] = []
        used = 0
        for ref, block in blocks:
            if used + len(block) <= full_budget:
                chunks.append(block)
                included.append(ref.id)
                used += len(block)
            else:
                omitted.append(ref)
        for index, ref in enumerate(omitted):
            remaining = char_budget - used
            sources_left = len(omitted) - index
            block = self._summary_block(ref, remaining // sources_left)
            if block:
                chunks.append(block)
                used += len(block)
        return "".join(chunks), included

    def coverage(self, packed: str) -> list[str]:
        return [ref.id for ref in self.refs if ref.id in packed]
