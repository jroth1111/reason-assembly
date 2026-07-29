from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .artifacts import SecretGuard
from .identity import CANONICAL_CLI, METADATA_CLIENT_VERSION, PRODUCT_SLUG
from .state_compat import default_state_root
from .v4_state import PrivateJsonStore


RAW_MODELS_ENDPOINT = "/v1/models"
CAPABILITY_METADATA_ENDPOINT = f"/v1/models?client_version={METADATA_CLIENT_VERSION}"
DEFAULT_SYNC_STATE = default_state_root() / "v4"


class AliasConfigurationError(RuntimeError):
    """A safe-to-display failure raised for unsupported alias configuration."""


@dataclass
class AliasPruneResult:
    aliases_before: dict[str, list[str]]
    aliases_after: dict[str, list[str]]
    removed_by_alias: dict[str, list[str]]
    changed: bool
    mode: int
    owner_uid: int
    owner_gid: int

    @property
    def removed_candidates(self) -> list[str]:
        return sorted(
            {
                candidate
                for candidates in self.removed_by_alias.values()
                for candidate in candidates
            }
        )

    @property
    def empty_aliases(self) -> list[str]:
        return sorted(
            alias for alias, candidates in self.aliases_after.items() if not candidates
        )


@dataclass
class SyncReport:
    generated_at: str
    attempts: int
    raw_ids: list[str]
    metadata_ids: list[str]
    council_ids: list[str]
    aliases: dict[str, dict[str, Any]]
    removed_candidates: list[str]
    empty_aliases: list[str]
    prune_attempted: bool
    prune_succeeded: bool
    prune_changed: bool
    warnings: list[str] = field(default_factory=list)
    status: str = "ok"

    @staticmethod
    def _hash_ids(ids: Iterable[str]) -> str:
        encoded = json.dumps(
            sorted(set(ids)),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def counts(self) -> dict[str, int]:
        return {
            "raw": len(self.raw_ids),
            "metadata": len(self.metadata_ids),
            "council": len(self.council_ids),
        }

    @property
    def hashes(self) -> dict[str, str]:
        return {
            "raw_model_ids_sha256": self._hash_ids(self.raw_ids),
            "metadata_model_ids_sha256": self._hash_ids(self.metadata_ids),
            "council_model_ids_sha256": self._hash_ids(self.council_ids),
        }

    @property
    def equality(self) -> dict[str, bool]:
        raw = set(self.raw_ids)
        metadata = set(self.metadata_ids)
        council = set(self.council_ids)
        return {
            "raw_metadata": raw == metadata,
            "raw_council": raw == council,
            "metadata_council": metadata == council,
            "all": raw == metadata == council,
        }

    def to_dict(self, guard: SecretGuard | None = None) -> dict[str, Any]:
        payload = {
            "schema_version": 4,
            "generated_at": self.generated_at,
            "status": self.status,
            "attempts": self.attempts,
            "counts": self.counts,
            "hashes": self.hashes,
            "equality": self.equality,
            "differences": {
                "raw_only": sorted(set(self.raw_ids) - set(self.metadata_ids)),
                "metadata_only": sorted(set(self.metadata_ids) - set(self.raw_ids)),
                "raw_not_in_council": sorted(
                    set(self.raw_ids) - set(self.council_ids)
                ),
                "council_not_in_raw": sorted(
                    set(self.council_ids) - set(self.raw_ids)
                ),
            },
            "aliases": self.aliases,
            "removed_candidates": self.removed_candidates,
            "empty_aliases": self.empty_aliases,
            "pruning": {
                "attempted": self.prune_attempted,
                "succeeded": self.prune_succeeded,
                "changed": self.prune_changed,
            },
            "warnings": self.warnings,
        }
        return guard.redact(payload) if guard else payload

    def receipt(self, guard: SecretGuard) -> dict[str, Any]:
        payload = {
            "schema_version": 4,
            "receipt_type": "catalogue_sync",
            "timestamp": self.generated_at,
            "outcome": self.status,
            "counts": self.counts,
            "hashes": self.hashes,
            "equality": self.equality,
            "removed_model_ids": self.removed_candidates,
            "empty_aliases": self.empty_aliases,
            "warnings": self.warnings,
        }
        return guard.redact(payload)


@dataclass
class CatalogueSyncResult:
    catalogue: list[Any]
    report: SyncReport
    authoritative_available: bool


@dataclass
class _CandidateNode:
    model_id: str
    node: Node


@dataclass
class _AliasNode:
    name: str
    candidates_key: ScalarNode | None
    candidates_node: SequenceNode | None
    candidates: list[_CandidateNode]


def _mapping_value(node: MappingNode, key_name: str) -> Node | None:
    for key, value in node.value:
        if isinstance(key, ScalarNode) and key.value == key_name:
            return value
    return None


def _candidate_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("model", "id", "slug", "name"):
            model_id = value.get(key)
            if isinstance(model_id, str) and model_id:
                return model_id
    return None


def _candidate_node_id(node: Node) -> str | None:
    if isinstance(node, ScalarNode) and node.tag.endswith(":str") and node.value:
        return node.value
    if isinstance(node, MappingNode):
        for name in ("model", "id", "slug", "name"):
            value = _mapping_value(node, name)
            if (
                isinstance(value, ScalarNode)
                and value.tag.endswith(":str")
                and value.value
            ):
                return value.value
    return None


def _parse_alias_document(
    text: str,
) -> tuple[dict[str, Any], dict[str, list[str]], list[_AliasNode]]:
    try:
        parsed = yaml.safe_load(text)
        root = yaml.compose(text)
    except yaml.YAMLError as error:
        raise AliasConfigurationError("malformed proxy configuration") from error
    if not isinstance(parsed, dict) or not isinstance(root, MappingNode):
        raise AliasConfigurationError("proxy configuration must be a YAML mapping")

    smart_node = _mapping_value(root, "smart-aliases")
    smart_data = parsed.get("smart-aliases")
    if smart_node is None and smart_data is None:
        return parsed, {}, []
    if smart_data is None:
        return parsed, {}, []
    if not isinstance(smart_data, dict) or not isinstance(smart_node, MappingNode):
        raise AliasConfigurationError("smart-aliases must be a mapping")

    aliases: dict[str, list[str]] = {}
    alias_nodes: list[_AliasNode] = []
    data_by_name = {str(key): value for key, value in smart_data.items()}
    for alias_key, alias_value_node in smart_node.value:
        if not isinstance(alias_key, ScalarNode) or not alias_key.tag.endswith(":str"):
            raise AliasConfigurationError("smart alias names must be strings")
        alias = alias_key.value
        alias_data = data_by_name.get(alias)
        if not isinstance(alias_data, dict) or not isinstance(
            alias_value_node, MappingNode
        ):
            raise AliasConfigurationError(
                f"smart alias {alias!r} must be a mapping"
            )
        candidates_data = alias_data.get("candidates")
        candidates_key: ScalarNode | None = None
        candidates_node: SequenceNode | None = None
        for key_node, value_node in alias_value_node.value:
            if isinstance(key_node, ScalarNode) and key_node.value == "candidates":
                candidates_key = key_node
                if isinstance(value_node, SequenceNode):
                    candidates_node = value_node
                elif candidates_data not in (None, []):
                    raise AliasConfigurationError(
                        f"smart alias {alias!r} candidates must be a list"
                    )
                break
        if candidates_data is None:
            candidates_data = []
        if not isinstance(candidates_data, list):
            raise AliasConfigurationError(
                f"smart alias {alias!r} candidates must be a list"
            )
        ids: list[str] = []
        for candidate in candidates_data:
            model_id = _candidate_id(candidate)
            if model_id is None:
                raise AliasConfigurationError(
                    f"smart alias {alias!r} has an invalid candidate"
                )
            ids.append(model_id)
        nodes: list[_CandidateNode] = []
        if candidates_node is not None:
            if len(candidates_node.value) != len(ids):
                raise AliasConfigurationError(
                    f"smart alias {alias!r} candidate structure is ambiguous"
                )
            for model_id, item_node in zip(ids, candidates_node.value):
                if _candidate_node_id(item_node) != model_id:
                    raise AliasConfigurationError(
                        f"smart alias {alias!r} candidate structure is unsupported"
                    )
                nodes.append(_CandidateNode(model_id, item_node))
        elif ids:
            raise AliasConfigurationError(
                f"smart alias {alias!r} candidate structure is unsupported"
            )
        aliases[alias] = ids
        alias_nodes.append(
            _AliasNode(alias, candidates_key, candidates_node, nodes)
        )
    return parsed, aliases, alias_nodes


def inspect_smart_aliases(config_path: Path) -> dict[str, list[str]]:
    try:
        text = Path(config_path).expanduser().read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise AliasConfigurationError("proxy configuration is not UTF-8") from error
    _, aliases, _ = _parse_alias_document(text)
    return aliases


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for index, character in enumerate(text):
        if character == "\n":
            offsets.append(index + 1)
    return offsets


def _node_line_span(text: str, offsets: list[int], node: Node) -> tuple[int, int]:
    start_line = node.start_mark.line
    end_line = node.end_mark.line
    if (
        end_line > start_line
        and text[offsets[end_line] : node.end_mark.index].strip() == ""
    ):
        end_line -= 1
    start = offsets[start_line]
    end = offsets[end_line + 1] if end_line + 1 < len(offsets) else len(text)
    return start, end


def _empty_sequence_insertion(text: str, key_node: ScalarNode) -> int:
    line_end = text.find("\n", key_node.end_mark.index)
    if line_end < 0:
        line_end = len(text)
    colon = text.find(":", key_node.end_mark.index, line_end)
    if colon < 0:
        raise AliasConfigurationError("candidates key has no YAML separator")
    suffix = text[colon + 1 : line_end]
    stripped = suffix.strip()
    if stripped and not stripped.startswith("#"):
        raise AliasConfigurationError(
            "inline smart alias candidates cannot be pruned safely"
        )
    return colon + 1


def _apply_replacements(
    text: str, replacements: list[tuple[int, int, str]]
) -> str:
    replacements.sort(key=lambda item: (item[0], item[1]), reverse=True)
    previous_start = len(text) + 1
    for start, end, replacement in replacements:
        if start < 0 or end < start or end > len(text) or end > previous_start:
            raise AliasConfigurationError("overlapping alias edits were refused")
        text = text[:start] + replacement + text[end:]
        previous_start = start
    return text


def _write_atomic_config(
    path: Path,
    value: bytes,
    original: os.stat_result,
) -> None:
    temp = path.with_name(
        f".{path.name}.{PRODUCT_SLUG}-{secrets.token_hex(6)}.tmp"
    )
    fd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        try:
            handle = os.fdopen(fd, "wb")
            fd = -1
            with handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o600)
                current = os.fstat(handle.fileno())
                if (
                    current.st_uid != original.st_uid
                    or current.st_gid != original.st_gid
                ):
                    os.fchown(handle.fileno(), original.st_uid, original.st_gid)
            if hasattr(os, "listxattr"):
                try:
                    for name in os.listxattr(path):
                        os.setxattr(temp, name, os.getxattr(path, name))
                except OSError:
                    pass
            os.replace(temp, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        if temp.exists():
            temp.unlink()


def prune_smart_aliases(
    config_path: Path,
    authoritative_ids: Iterable[str],
    *,
    lock_path: Path,
) -> AliasPruneResult:
    config_path = Path(config_path).expanduser().resolve(strict=True)
    lock_path = Path(lock_path).expanduser()
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    lock_fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        original = config_path.stat()
        if not stat.S_ISREG(original.st_mode):
            raise AliasConfigurationError(
                "proxy configuration must resolve to a regular file"
            )
        raw_bytes = config_path.read_bytes()
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AliasConfigurationError(
                "proxy configuration is not UTF-8"
            ) from error
        parsed, aliases_before, alias_nodes = _parse_alias_document(text)
        authority = set(authoritative_ids)
        removed_by_alias: dict[str, list[str]] = {}
        replacements: list[tuple[int, int, str]] = []
        offsets = _line_offsets(text)
        expected = copy.deepcopy(parsed)
        expected_aliases = expected.get("smart-aliases") or {}

        for alias_node in alias_nodes:
            removed_nodes = [
                candidate
                for candidate in alias_node.candidates
                if candidate.model_id not in authority
            ]
            if not removed_nodes:
                continue
            if (
                alias_node.candidates_node is None
                or alias_node.candidates_node.flow_style
            ):
                raise AliasConfigurationError(
                    "inline smart alias candidates cannot be pruned safely"
                )
            removed_by_alias[alias_node.name] = [
                candidate.model_id for candidate in removed_nodes
            ]
            for candidate in removed_nodes:
                start, end = _node_line_span(text, offsets, candidate.node)
                replacements.append((start, end, ""))
            remaining = [
                candidate
                for candidate in aliases_before[alias_node.name]
                if candidate in authority
            ]
            expected_aliases[alias_node.name]["candidates"] = [
                candidate
                for candidate in expected_aliases[alias_node.name]["candidates"]
                if _candidate_id(candidate) in authority
            ]
            if not remaining:
                if alias_node.candidates_key is None:
                    raise AliasConfigurationError(
                        "empty smart alias candidates cannot be represented safely"
                    )
                insertion = _empty_sequence_insertion(
                    text, alias_node.candidates_key
                )
                replacements.append((insertion, insertion, " []"))

        if not replacements:
            return AliasPruneResult(
                aliases_before=aliases_before,
                aliases_after=copy.deepcopy(aliases_before),
                removed_by_alias={},
                changed=False,
                mode=stat.S_IMODE(original.st_mode),
                owner_uid=original.st_uid,
                owner_gid=original.st_gid,
            )

        updated = _apply_replacements(text, replacements)
        try:
            reparsed = yaml.safe_load(updated)
        except yaml.YAMLError as error:
            raise AliasConfigurationError(
                "pruned proxy configuration did not remain valid YAML"
            ) from error
        if reparsed != expected:
            raise AliasConfigurationError(
                "alias pruning would alter unrelated proxy configuration"
            )
        _write_atomic_config(config_path, updated.encode("utf-8"), original)
        final_stat = config_path.stat()
        if config_path.read_bytes() != updated.encode("utf-8"):
            raise AliasConfigurationError("atomic alias write verification failed")
        if stat.S_IMODE(final_stat.st_mode) != 0o600:
            raise AliasConfigurationError("proxy configuration mode is not 0600")
        if (
            final_stat.st_uid != original.st_uid
            or final_stat.st_gid != original.st_gid
        ):
            raise AliasConfigurationError(
                "proxy configuration ownership changed during pruning"
            )
        _, aliases_after, _ = _parse_alias_document(updated)
        return AliasPruneResult(
            aliases_before=aliases_before,
            aliases_after=aliases_after,
            removed_by_alias=removed_by_alias,
            changed=True,
            mode=stat.S_IMODE(final_stat.st_mode),
            owner_uid=final_stat.st_uid,
            owner_gid=final_stat.st_gid,
        )
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("models", "data", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def model_ids(payload: Any) -> list[str]:
    result: set[str] = set()
    for item in _payload_items(payload):
        for key in ("slug", "id", "model", "name"):
            value = item.get(key)
            if value:
                result.add(str(value))
                break
    return sorted(result)


async def _fetch_payload(
    client: httpx.AsyncClient, endpoint: str
) -> tuple[Any | None, Exception | None]:
    try:
        response = await client.get(endpoint)
        response.raise_for_status()
        return response.json(), None
    except Exception as error:
        return None, error


async def _fetch_pair(
    client: httpx.AsyncClient,
) -> tuple[Any | None, Any | None, int]:
    raw, metadata = await asyncio.gather(
        _fetch_payload(client, RAW_MODELS_ENDPOINT),
        _fetch_payload(client, CAPABILITY_METADATA_ENDPOINT),
    )
    raw_payload, raw_error = raw
    metadata_payload, metadata_error = metadata
    attempts = 1
    mismatch = (
        raw_error is not None
        or metadata_error is not None
        or set(model_ids(raw_payload)) != set(model_ids(metadata_payload))
    )
    if mismatch:
        attempts = 2
        second_raw, second_metadata = await asyncio.gather(
            _fetch_payload(client, RAW_MODELS_ENDPOINT),
            _fetch_payload(client, CAPABILITY_METADATA_ENDPOINT),
        )
        if second_raw[0] is not None:
            raw_payload = second_raw[0]
        if second_metadata[0] is not None:
            metadata_payload = second_metadata[0]
        if raw_payload is None:
            raw_error = second_raw[1] or raw_error
        if metadata_payload is None:
            metadata_error = second_metadata[1] or metadata_error
    return raw_payload, metadata_payload, attempts


def _alias_report(
    aliases_before: dict[str, list[str]],
    aliases_after: dict[str, list[str]],
    removed_by_alias: dict[str, list[str]],
    authority: set[str] | None,
) -> dict[str, dict[str, Any]]:
    names = sorted(set(aliases_before) | set(aliases_after))
    result: dict[str, dict[str, Any]] = {}
    for alias in names:
        before = aliases_before.get(alias, [])
        after = aliases_after.get(alias, [])
        missing = (
            sorted({candidate for candidate in before if candidate not in authority})
            if authority is not None
            else []
        )
        result[alias] = {
            "candidates": after,
            "removed": removed_by_alias.get(alias, []),
            "missing_before_prune": missing,
            "empty": not after,
            "all_candidates_catalogued": (
                all(candidate in authority for candidate in after)
                if authority is not None
                else None
            ),
        }
    return result


def _write_receipts(
    state_root: Path,
    report: SyncReport,
    guard: SecretGuard,
) -> None:
    state_root = Path(state_root).expanduser()
    payload = report.receipt(guard)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    receipt_path = (
        state_root
        / "sync-receipts"
        / f"{stamp}-{secrets.token_hex(4)}.json"
    )
    PrivateJsonStore(receipt_path, {"schema_version": 4}).write(payload)
    PrivateJsonStore(
        state_root / "catalogue-sync-latest.json",
        {"schema_version": 4},
    ).write(payload)


async def synchronize_catalogue(
    client: httpx.AsyncClient,
    *,
    config_path: Path,
    state_root: Path = DEFAULT_SYNC_STATE,
    catalogue_builder: Callable[[Any, Any], list[Any]],
    exact_secrets: Iterable[str] = (),
    warning_sink: Callable[[str], None] | None = None,
) -> CatalogueSyncResult:
    guard = SecretGuard(exact_secrets)
    generated_at = datetime.now(timezone.utc).isoformat()
    warnings: list[str] = []
    try:
        aliases_before = inspect_smart_aliases(config_path)
    except Exception as error:
        aliases_before = {}
        warnings.append(
            "alias inspection failed "
            f"({type(error).__name__}); runtime filtering remains active"
        )

    raw_payload, metadata_payload, attempts = await _fetch_pair(client)
    raw_ids = model_ids(raw_payload)
    metadata_ids = model_ids(metadata_payload)
    authoritative_available = raw_payload is not None
    catalogue: list[Any] = []
    council_ids: list[str] = []
    aliases_after = copy.deepcopy(aliases_before)
    removed_by_alias: dict[str, list[str]] = {}
    prune_attempted = False
    prune_succeeded = False
    prune_changed = False

    if not authoritative_available:
        warnings.append(
            "authoritative /v1/models catalogue is unavailable; "
            "selection cannot continue safely"
        )
    else:
        if metadata_payload is None:
            metadata_payload = {"models": []}
            metadata_ids = []
            warnings.append(
                "capability metadata is unavailable; raw models are listed-only"
            )
        if set(raw_ids) != set(metadata_ids):
            warnings.append(
                "raw and capability-metadata model IDs still differ after retry; "
                "/v1/models is authoritative"
            )
        try:
            catalogue = catalogue_builder(raw_payload, metadata_payload)
        except Exception:
            warnings.append(
                "capability metadata could not be applied; raw models are listed-only"
            )
            catalogue = catalogue_builder(raw_payload, {"models": []})
        council_ids = sorted(
            {
                str(getattr(item, "id"))
                for item in catalogue
                if getattr(item, "id", None)
            }
        )
        authority = set(raw_ids)
        prune_attempted = True
        try:
            pruned = prune_smart_aliases(
                config_path,
                authority,
                lock_path=Path(state_root) / "catalogue-sync.lock",
            )
            aliases_before = pruned.aliases_before
            aliases_after = pruned.aliases_after
            removed_by_alias = pruned.removed_by_alias
            prune_succeeded = True
            prune_changed = pruned.changed
        except Exception as error:
            warnings.append(
                "smart-alias pruning failed "
                f"({type(error).__name__}); runtime filtering remains active"
            )

    aliases = _alias_report(
        aliases_before,
        aliases_after,
        removed_by_alias,
        set(raw_ids) if authoritative_available else None,
    )
    empty_aliases = sorted(
        alias for alias, diagnostics in aliases.items() if diagnostics["empty"]
    )
    if empty_aliases:
        warnings.append(
            "one or more smart aliases have no catalogued candidates: "
            + ", ".join(empty_aliases)
        )
    removed_candidates = sorted(
        {
            candidate
            for candidates in removed_by_alias.values()
            for candidate in candidates
        }
    )
    status = (
        "failed"
        if not authoritative_available
        else "degraded"
        if warnings
        else "ok"
    )
    report = SyncReport(
        generated_at=generated_at,
        attempts=attempts,
        raw_ids=raw_ids,
        metadata_ids=metadata_ids,
        council_ids=council_ids,
        aliases=aliases,
        removed_candidates=removed_candidates,
        empty_aliases=empty_aliases,
        prune_attempted=prune_attempted,
        prune_succeeded=prune_succeeded,
        prune_changed=prune_changed,
        warnings=warnings,
        status=status,
    )
    try:
        _write_receipts(Path(state_root), report, guard)
    except Exception as error:
        report.warnings.append(
            "private sync receipt could not be persisted "
            f"({type(error).__name__})"
        )
        if report.status == "ok":
            report.status = "degraded"

    sink = warning_sink or (
        lambda warning: print(
            f"{CANONICAL_CLI}: warning: {warning}",
            file=sys.stderr,
        )
    )
    for warning in report.warnings:
        sink(guard.redact_text(warning))
    return CatalogueSyncResult(
        catalogue=catalogue,
        report=report,
        authoritative_available=authoritative_available,
    )
