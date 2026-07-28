from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import CalibrationAnchor, RouteEpoch
from v4 import digest


class PrivateJsonStore:
    def __init__(self, path: Path, default: dict[str, Any]):
        self.path = Path(path)
        self.default = default

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return json.loads(json.dumps(self.default))
        data = json.loads(self.path.read_text())
        if data.get("schema_version") != 4:
            raise RuntimeError(f"{self.path.name} is not schema v4")
        return data

    def _write_temp(self, data: dict[str, Any]) -> Path:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        temp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(5)}")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return temp

    def initialize(self, data: dict[str, Any]) -> bool:
        temp = self._write_temp(data)
        try:
            try:
                os.link(temp, self.path)
            except FileExistsError:
                return False
            os.chmod(self.path, 0o600)
            return True
        finally:
            temp.unlink(missing_ok=True)

    def write(self, data: dict[str, Any]) -> None:
        temp = self._write_temp(data)
        try:
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temp.unlink(missing_ok=True)


class AnchorStore:
    def __init__(self, root: Path):
        self.store = PrivateJsonStore(
            Path(root) / "anchors.json",
            {"schema_version": 4, "anchors": []},
        )

    @staticmethod
    def parse_jsonl(path: Path) -> list[CalibrationAnchor]:
        anchors: list[CalibrationAnchor] = []
        ids: set[str] = set()
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid anchor JSONL line {number}: {error}") from error
            if not isinstance(raw, dict):
                raise ValueError(f"anchor line {number} must be an object")
            allowed = {"id", "task", "expected", "task_kind", "domain",
                       "answer_format", "active", "sha256"}
            extra = set(raw) - allowed
            if extra:
                raise ValueError(f"anchor line {number} has forbidden fields: {sorted(extra)}")
            supplied_hash = raw.pop("sha256", None)
            raw["sha256"] = "pending"
            provisional = CalibrationAnchor.model_validate(raw)
            body = provisional.model_dump(mode="json", exclude={"sha256"})
            expected_hash = digest(body)
            if supplied_hash not in (None, expected_hash):
                raise ValueError(f"anchor line {number} sha256 mismatch")
            anchor = provisional.model_copy(update={"sha256": expected_hash})
            if anchor.id in ids:
                raise ValueError(f"duplicate anchor id: {anchor.id}")
            ids.add(anchor.id)
            anchors.append(anchor)
        return anchors

    def import_file(self, path: Path) -> list[CalibrationAnchor]:
        incoming = self.parse_jsonl(path)
        data = self.store.read()
        existing = {row["id"]: row for row in data["anchors"]}
        for anchor in incoming:
            if anchor.id in existing and existing[anchor.id]["sha256"] != anchor.sha256:
                raise ValueError(f"anchor id already exists with different content: {anchor.id}")
            existing[anchor.id] = anchor.model_dump(mode="json")
        data["anchors"] = [existing[key] for key in sorted(existing)]
        self.store.write(data)
        return incoming

    def list(self, *, active_only: bool = False) -> list[CalibrationAnchor]:
        anchors = [CalibrationAnchor.model_validate(row)
                   for row in self.store.read()["anchors"]]
        return [item for item in anchors if item.active] if active_only else anchors

    def validate(self) -> list[CalibrationAnchor]:
        anchors = self.list()
        for item in anchors:
            body = item.model_dump(mode="json", exclude={"sha256"})
            if digest(body) != item.sha256:
                raise ValueError(f"anchor sha256 mismatch: {item.id}")
        return anchors

    def retire(self, anchor_id: str) -> CalibrationAnchor:
        data = self.store.read()
        for row in data["anchors"]:
            if row["id"] == anchor_id:
                row["active"] = False
                body = {key: value for key, value in row.items() if key != "sha256"}
                row["sha256"] = digest(body)
                self.store.write(data)
                return CalibrationAnchor.model_validate(row)
        raise ValueError(f"anchor not found: {anchor_id}")


class RouteEpochStore:
    def __init__(self, root: Path):
        self.store = PrivateJsonStore(
            Path(root) / "route-epochs.json",
            {"schema_version": 4, "epochs": []},
        )

    @staticmethod
    def fingerprint(catalogue: list[dict]) -> str:
        stable = [{
            key: row.get(key) for key in
            ("id", "family", "provider", "context_window", "efforts", "roles")
        } for row in catalogue]
        return digest(sorted(stable, key=lambda row: str(row["id"])))

    def current(self, catalogue: list[dict]) -> RouteEpoch:
        fingerprint = self.fingerprint(catalogue)
        data = self.store.read()
        for row in data["epochs"]:
            if row["catalogue_fingerprint"] == fingerprint:
                return RouteEpoch.model_validate(row)
        epoch = RouteEpoch(
            id="RE-" + fingerprint[:12],
            catalogue_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc),
        )
        data["epochs"].append(epoch.model_dump(mode="json"))
        self.store.write(data)
        return epoch

    def record_anchor(self, epoch_id: str, anchor_id: str, passed: bool) -> RouteEpoch:
        data = self.store.read()
        for row in data["epochs"]:
            if row["id"] == epoch_id:
                row.setdefault("anchor_results", {})[anchor_id] = passed
                row["validated"] = bool(row["anchor_results"]) and all(
                    row["anchor_results"].values()
                )
                self.store.write(data)
                return RouteEpoch.model_validate(row)
        raise ValueError(f"route epoch not found: {epoch_id}")


def initialize_v4_state(root: Path) -> None:
    root = Path(root)
    defaults = {
        "reliability.json": {
            "policy_version": "v4",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "buckets": [], "confidence_buckets": [],
        },
        "cofailure.json": {"version": 4, "buckets": {}},
        "calibration.json": {"schema_version": 4, "examples": []},
        "operation-effects.json": {"schema_version": 4, "effects": []},
        "anchors.json": {"schema_version": 4, "anchors": []},
        "route-epochs.json": {"schema_version": 4, "epochs": []},
    }
    for name, default in defaults.items():
        PrivateJsonStore(root / name, default).initialize(default)
