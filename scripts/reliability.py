from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from contracts import (
    ConfidenceBucket,
    Outcome,
    ReliabilityBucket,
    ReliabilitySnapshot,
    Role,
    TaskKind,
)


HALF_LIFE_DAYS = 90.0
ACTIVE_OBSERVATIONS = 8.0
FAMILY_OBSERVATIONS = 20.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lower_bound(alpha: float, beta: float) -> float:
    total = alpha + beta
    if total <= 0:
        return 0.0
    mean = alpha / total
    variance = (alpha * beta) / (total * total * (total + 1))
    return max(0.0, min(1.0, mean - 1.6448536269514722 * math.sqrt(variance)))


def _refresh(bucket):
    total = bucket.alpha + bucket.beta
    bucket.posterior_mean = bucket.alpha / total if total else 0.5
    bucket.conservative_lower_bound = _lower_bound(bucket.alpha, bucket.beta)
    bucket.active = bucket.effective_observations >= ACTIVE_OBSERVATIONS
    return bucket


class ReliabilityStore:
    """Private, atomic model-role-task outcome state."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "reliability.json"

    def empty(self) -> ReliabilitySnapshot:
        return ReliabilitySnapshot(generated_at=_now())

    def load(self, now: datetime | None = None) -> ReliabilitySnapshot:
        current = now or _now()
        if not self.path.exists():
            return self.empty()
        raw = json.loads(self.path.read_text())
        raw.pop("pair_buckets", None)
        value = ReliabilitySnapshot.model_validate(raw)
        decayed: list[ReliabilityBucket] = []
        for bucket in value.buckets:
            age_days = max(
                0.0, (current - bucket.last_updated).total_seconds() / 86_400
            )
            factor = 0.5 ** (age_days / HALF_LIFE_DAYS)
            bucket.alpha = 2.0 + (bucket.alpha - 2.0) * factor
            bucket.beta = 2.0 + (bucket.beta - 2.0) * factor
            bucket.effective_observations *= factor
            for field in (
                "true_positive",
                "false_positive",
                "true_negative",
                "false_negative",
                "error_detection_success",
                "error_detection_total",
                "order_consistent",
                "order_observations",
                "revision_success",
                "revision_observations",
                "calibration_absolute_error",
                "calibration_observations",
            ):
                setattr(bucket, field, getattr(bucket, field) * factor)
            bucket.competitor_failures = {
                key: value * factor for key, value in bucket.competitor_failures.items()
            }
            bucket.competitor_observations = {
                key: value * factor
                for key, value in bucket.competitor_observations.items()
            }
            decayed.append(_refresh(bucket))
        confidence = []
        for bucket in value.confidence_buckets:
            age_days = max(
                0.0, (current - bucket.last_updated).total_seconds() / 86_400
            )
            factor = 0.5 ** (age_days / HALF_LIFE_DAYS)
            bucket.alpha = 2.0 + (bucket.alpha - 2.0) * factor
            bucket.beta = 2.0 + (bucket.beta - 2.0) * factor
            bucket.effective_observations *= factor
            confidence.append(_refresh(bucket))
        return ReliabilitySnapshot(
            generated_at=current,
            buckets=decayed,
            confidence_buckets=confidence,
        )

    def write(self, snapshot: ReliabilitySnapshot) -> None:
        payload = json.dumps(
            snapshot.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        temp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temp.exists():
                temp.unlink()

    @staticmethod
    def key(
        model: str, family: str, role: str, task_kind: str, domain: str
    ) -> tuple[str, ...]:
        return model, family, role, task_kind, domain

    def score(
        self,
        snapshot: ReliabilitySnapshot,
        *,
        model: str,
        family: str,
        role: Role,
        task_kind: TaskKind,
        domain: str = "general",
    ) -> tuple[float, float, bool]:
        exact = next(
            (
                item
                for item in snapshot.buckets
                if self.key(
                    item.model,
                    item.family,
                    item.role,
                    item.task_kind,
                    item.domain,
                )
                == self.key(model, family, role, task_kind, domain)
            ),
            None,
        )
        if exact and exact.active:
            correctness = exact.conservative_lower_bound
            detection = (
                exact.error_detection_success / exact.error_detection_total
                if exact.error_detection_total
                else correctness
            )
            order = (
                exact.order_consistent / exact.order_observations
                if exact.order_observations
                else correctness
            )
            revision = (
                exact.revision_success / exact.revision_observations
                if exact.revision_observations
                else correctness
            )
            calibration = (
                1 - exact.calibration_absolute_error / exact.calibration_observations
                if exact.calibration_observations
                else correctness
            )
            competitor_robustness = (
                1
                - max(
                    exact.competitor_failures.get(model, 0) / observations
                    for model, observations in exact.competitor_observations.items()
                    if observations
                )
                if any(exact.competitor_observations.values())
                else correctness
            )
            return (
                0.50 * correctness
                + 0.15 * detection
                + 0.10 * order
                + 0.10 * revision
                + 0.10 * calibration
                + 0.05 * competitor_robustness,
                exact.effective_observations,
                True,
            )
        family_rows = [
            item
            for item in snapshot.buckets
            if item.family == family
            and item.role == role
            and item.task_kind == task_kind
            and item.domain == domain
        ]
        n = sum(item.effective_observations for item in family_rows)
        if n >= FAMILY_OBSERVATIONS:
            alpha = 2 + sum(max(0, item.alpha - 2) for item in family_rows)
            beta = 2 + sum(max(0, item.beta - 2) for item in family_rows)
            return _lower_bound(alpha, beta), n, True
        return 0.5, n, False

    def update(
        self,
        snapshot: ReliabilitySnapshot,
        outcome: Outcome,
        attributions: Iterable[tuple],
    ) -> ReliabilitySnapshot:
        by_key = {
            self.key(
                item.model,
                item.family,
                item.role,
                item.task_kind,
                item.domain,
            ): item
            for item in snapshot.buckets
        }
        confidence_by_key = {
            (
                item.model,
                item.family,
                item.role,
                item.task_kind,
                item.domain,
                item.decile,
            ): item
            for item in snapshot.confidence_buckets
        }
        for attribution in attributions:
            model, family, role, task_kind, base_weight = attribution[:5]
            reported = attribution[5] if len(attribution) > 5 else None
            subject_ids = set(attribution[6]) if len(attribution) > 6 else set()
            metadata = attribution[7] if len(attribution) > 7 else {}
            domain = str(metadata.get("domain") or "general")
            key = self.key(model, family, role, task_kind, domain)
            bucket = by_key.get(key)
            if not bucket:
                bucket = ReliabilityBucket(
                    model=model,
                    family=family,
                    role=role,
                    task_kind=task_kind,
                    domain=domain,
                    last_updated=outcome.recorded_at,
                )
                by_key[key] = bucket
            observations = outcome.observations or []
            if observations:
                rows = [
                    row
                    for row in observations
                    if not subject_ids or row.subject_id in subject_ids
                ]
            else:
                from contracts import OutcomeObservation

                rows = [
                    OutcomeObservation(
                        subject_type="run",
                        subject_id=outcome.run_id,
                        status=outcome.status,
                        weight=0.25,
                    )
                ]
            raw_weights = [
                min(1.0, observation.weight * base_weight) for observation in rows
            ]
            scale = (
                min(1.0, sum(raw_weights)) / sum(raw_weights) if sum(raw_weights) else 0
            )
            effective_weights = [value * scale for value in raw_weights]
            for observation, weight in zip(rows, effective_weights):
                if observation.status == "confirmed":
                    bucket.alpha += weight
                    bucket.effective_observations += weight
                elif observation.status == "disconfirmed":
                    bucket.beta += weight
                    bucket.effective_observations += weight
                elif observation.status == "mixed":
                    bucket.alpha += weight / 2
                    bucket.beta += weight / 2
                    bucket.effective_observations += weight
                prediction = metadata.get("predictions", {}).get(
                    observation.subject_id,
                    metadata.get("prediction"),
                )
                if prediction == "support":
                    if observation.status == "confirmed":
                        bucket.true_positive += weight
                    elif observation.status == "disconfirmed":
                        bucket.false_positive += weight
                elif prediction == "oppose":
                    if observation.status == "disconfirmed":
                        bucket.true_negative += weight
                    elif observation.status == "confirmed":
                        bucket.false_negative += weight
                if metadata.get("detected_error") is not None:
                    bucket.error_detection_total += weight
                    if metadata["detected_error"]:
                        bucket.error_detection_success += weight
                if metadata.get("order_consistent") is not None:
                    bucket.order_observations += weight
                    if metadata["order_consistent"]:
                        bucket.order_consistent += weight
                if metadata.get("revised") is not None:
                    bucket.revision_observations += weight
                    if metadata.get("revision_correct"):
                        bucket.revision_success += weight
                competitor = metadata.get("competitor")
                if competitor:
                    bucket.competitor_observations[competitor] = (
                        bucket.competitor_observations.get(competitor, 0) + weight
                    )
                    if observation.status == "disconfirmed":
                        bucket.competitor_failures[competitor] = (
                            bucket.competitor_failures.get(competitor, 0) + weight
                        )
                if reported is not None and observation.status in {
                    "confirmed",
                    "disconfirmed",
                }:
                    target = 1 if observation.status == "confirmed" else 0
                    bucket.calibration_absolute_error += (
                        abs(float(reported) - target) * weight
                    )
                    bucket.calibration_observations += weight
            bucket.last_updated = outcome.recorded_at
            _refresh(bucket)
            if reported is not None and rows:
                decile = min(9, max(0, int(float(reported) * 10)))
                confidence_key = (model, family, role, task_kind, domain, decile)
                confidence_bucket = confidence_by_key.get(confidence_key)
                if not confidence_bucket:
                    confidence_bucket = ConfidenceBucket(
                        model=model,
                        family=family,
                        role=role,
                        task_kind=task_kind,
                        domain=domain,
                        decile=decile,
                        last_updated=outcome.recorded_at,
                    )
                    confidence_by_key[confidence_key] = confidence_bucket
                for observation, weight in zip(rows, effective_weights):
                    if observation.status == "confirmed":
                        confidence_bucket.alpha += weight
                        confidence_bucket.effective_observations += weight
                    elif observation.status == "disconfirmed":
                        confidence_bucket.beta += weight
                        confidence_bucket.effective_observations += weight
                    elif observation.status == "mixed":
                        confidence_bucket.alpha += weight / 2
                        confidence_bucket.beta += weight / 2
                        confidence_bucket.effective_observations += weight
                confidence_bucket.last_updated = outcome.recorded_at
                _refresh(confidence_bucket)
        result = ReliabilitySnapshot(
            generated_at=outcome.recorded_at,
            buckets=sorted(
                by_key.values(),
                key=lambda item: (
                    item.model,
                    item.role,
                    item.task_kind,
                    item.domain,
                ),
            ),
            confidence_buckets=sorted(
                confidence_by_key.values(),
                key=lambda item: (
                    item.model,
                    item.role,
                    item.task_kind,
                    item.domain,
                    item.decile,
                ),
            ),
        )
        self.write(result)
        return result

    def confidence_score(
        self,
        snapshot: ReliabilitySnapshot,
        *,
        model: str,
        family: str,
        role: Role,
        task_kind: TaskKind,
        reported: float,
        domain: str = "general",
    ) -> tuple[float, float, bool]:
        decile = min(9, max(0, int(reported * 10)))
        bucket = next(
            (
                item
                for item in snapshot.confidence_buckets
                if item.model == model
                and item.family == family
                and item.role == role
                and item.task_kind == task_kind
                and item.domain == domain
                and item.decile == decile
            ),
            None,
        )
        if bucket and bucket.active:
            return (
                bucket.conservative_lower_bound,
                bucket.effective_observations,
                True,
            )
        return 0.5, bucket.effective_observations if bucket else 0, False

def exploratory_run(run_id: str) -> bool:
    return int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16) % 5 == 0
