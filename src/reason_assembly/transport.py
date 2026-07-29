from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx
import yaml

from .artifacts import RunStore
from .catalogue_sync import CatalogueSyncResult, SyncReport, synchronize_catalogue
from .contracts import BudgetEvent, ModelCapability
from .identity import (
    PROXY_ADAPTER_CONFIG_ENV,
    PROXY_CONFIG_ENV,
    SESSION_NAMESPACE,
    LEGACY_WORKER_EXECUTABLE_ENV,
    WORKER_EXECUTABLE_ENV,
    USER_AGENT,
)
from .state_compat import resolve_state_root


DEFAULT_CONFIG = Path(
    "~/Library/Application Support/AIUsage/CLIProxyAPI/config.yaml"
).expanduser()


def proxy_config_path_from_env(
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get(PROXY_CONFIG_ENV) or values.get(PROXY_ADAPTER_CONFIG_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG


class ProxyCallError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class QuotaError(ProxyCallError):
    pass


class ContextError(ProxyCallError):
    pass


class CallBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    backoff_factor: float = 2.0
    jitter: bool = True

    def delay(self, retry_number: int) -> float:
        raw = min(
            self.base_delay * (self.backoff_factor ** max(0, retry_number - 1)),
            self.max_delay,
        )
        return raw * (0.5 + random.random() * 0.5) if self.jitter else raw


_RETRYABLE_STATUS = {500, 502, 503, 504}
_RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)


class ProxySettings:
    def __init__(self, path: Path | None = None):
        self.path = (path or proxy_config_path_from_env()).expanduser()
        raw = yaml.safe_load(self.path.read_text()) or {}
        host = raw.get("host") or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        self.base_url = f"http://{host}:{int(raw.get('port', 8317))}"
        keys = raw.get("api-keys") or []
        if not keys:
            raise RuntimeError(f"no client API key in {self.path}")
        self.api_key = str(keys[0])
        self.exact_secrets = self._collect_exact_secrets(raw)
        self.worker_executable = (
            os.environ.get(WORKER_EXECUTABLE_ENV)
            or os.environ.get(LEGACY_WORKER_EXECUTABLE_ENV)
            or "codex"
        )

    @staticmethod
    def _collect_exact_secrets(value: Any, key: str = "") -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for item_key, item in value.items():
                lowered = str(item_key).lower()
                if any(
                    marker in lowered
                    for marker in ("key", "token", "secret", "authorization")
                ):
                    if isinstance(item, str) and len(item) >= 6:
                        found.add(item)
                    elif isinstance(item, list):
                        for child in item:
                            if isinstance(child, str) and len(child) >= 6:
                                found.add(child)
                            elif isinstance(child, dict):
                                found.update(
                                    ProxySettings._collect_exact_secrets(
                                        child, str(item_key)
                                    )
                                )
                found.update(ProxySettings._collect_exact_secrets(item, str(item_key)))
        elif isinstance(value, list):
            for item in value:
                found.update(ProxySettings._collect_exact_secrets(item, key))
        elif (
            isinstance(value, str)
            and len(value) >= 6
            and any(
                marker in key.lower()
                for marker in ("key", "token", "secret", "authorization")
            )
        ):
            found.add(value)
        return found


class CallBudget:
    def __init__(self, cap: int, store: RunStore | None = None):
        if cap < 1:
            raise ValueError("call cap must be positive")
        self.cap = cap
        self.used = 0
        self.store = store
        self.events: list[BudgetEvent] = []

    def ensure_available(self, stage: str) -> None:
        if self.used >= self.cap:
            raise CallBudgetExceeded(
                f"direct-call cap exhausted ({self.used}/{self.cap}) before {stage}"
            )

    def consume(self, stage: str, model: str) -> BudgetEvent:
        self.ensure_available(stage)
        self.used += 1
        item = BudgetEvent(
            index=self.used,
            cap=self.cap,
            stage=stage,
            model=model,
            at=datetime.now(timezone.utc),
        )
        self.events.append(item)
        if self.store:
            self.store.append_event(
                "call_budget",
                index=item.index,
                cap=item.cap,
                stage=stage,
                model=model,
            )
            self.store.write_json("call-budget.json", self.events)
        return item


def provider_family(model: str, owned_by: str = "") -> str:
    ident = model.lower()
    owner = owned_by.lower()
    if "claude" in ident:
        return "anthropic"
    if "gemini" in ident or "gemma" in ident:
        return "google"
    if ident.startswith(("gpt-", "codex-", "o1", "o3", "o4")):
        return "openai"
    if "qwen" in ident:
        return "qwen"
    if "kimi" in ident:
        return "moonshot"
    if "minimax" in ident:
        return "minimax"
    if "glm" in ident:
        return "zai"
    if any(item in ident for item in ("mistral", "ministral", "devstral")):
        return "mistral"
    if "deepseek" in ident:
        return "deepseek"
    if "nemotron" in ident or "nvidia" in owner:
        return "nvidia"
    if owner:
        return owner
    return ident.split("-", 1)[0]


def _first(mapping: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return default


def _efforts(value: Any) -> list[str]:
    if isinstance(value, dict):
        if "levels" in value:
            return _efforts(value["levels"])
        return [
            str(key)
            for key, enabled in value.items()
            if enabled is not False and enabled is not None
        ]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            effort = item
        elif isinstance(item, dict):
            effort = _first(item, ("effort", "level", "name", "value", "id"), "")
        else:
            effort = ""
        if effort and str(effort) not in result:
            result.append(str(effort))
    return result


def _modalities(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, str):
        return [value.lower()]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                result.append(item.lower())
            elif isinstance(item, dict):
                name = _first(item, ("type", "name", "modality"))
                if name:
                    result.append(str(name).lower())
        return sorted(set(result)) or default
    return default


def _metadata_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("models", "data", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def merge_catalogues(raw_payload: Any, metadata_payload: Any) -> list[ModelCapability]:
    raw_items = _metadata_items(raw_payload)
    meta_items = _metadata_items(metadata_payload)

    def ident(item: dict[str, Any]) -> str:
        return str(_first(item, ("slug", "id", "model", "name"), ""))

    raw_by_id = {ident(item): item for item in raw_items if ident(item)}
    meta_by_id = {ident(item): item for item in meta_items if ident(item)}
    capabilities: list[ModelCapability] = []
    for model_id in sorted(raw_by_id):
        raw = raw_by_id.get(model_id, {})
        meta = meta_by_id.get(model_id, {})
        metadata_matched = model_id in meta_by_id
        owner = str(
            _first(
                raw,
                ("owned_by", "provider", "owner"),
                _first(meta, ("owned_by", "provider", "owner"), ""),
            )
        )
        context_raw = _first(
            meta,
            (
                "context_window",
                "max_context_window",
                "context_length",
                "max_input_tokens",
            ),
        )
        if isinstance(context_raw, dict):
            context_raw = _first(context_raw, ("tokens", "max", "input", "total"))
        try:
            context = int(context_raw) if context_raw else None
        except (TypeError, ValueError):
            context = None
        efforts = _efforts(
            _first(
                meta,
                (
                    "supported_reasoning_levels",
                    "supported_reasoning_efforts",
                    "reasoning_levels",
                    "efforts",
                    "reasoning",
                ),
                [],
            )
        )
        api_value = _first(
            meta,
            ("supported_in_api", "api_support", "supports_api"),
            False,
        )
        if isinstance(api_value, (list, tuple, set)):
            api_support = bool(
                {"responses", "openai", "v1/responses"} & set(map(str, api_value))
            )
        else:
            api_support = bool(api_value)
        inputs = _modalities(
            _first(meta, ("input_modalities", "modalities", "inputs")),
            [],
        )
        outputs = _modalities(
            _first(meta, ("output_modalities", "outputs")),
            ["text"],
        )
        lower_id = model_id.lower()
        image_route = (
            lower_id.startswith("gpt-image")
            or "-image" in lower_id
            or outputs == ["image"]
        )
        if image_route and "text" in outputs:
            outputs = ["image"]
        tool_support = bool(
            _first(
                meta,
                (
                    "supports_parallel_tool_calls",
                    "supports_tools",
                    "tool_support",
                ),
                False,
            )
            or meta.get("tool_mode")
            or meta.get("apply_patch_tool_type")
        )
        visibility = meta.get("visibility")
        reasons: list[str] = []
        if not metadata_matched:
            reasons.append("listed-only: missing capability metadata")
        if not context:
            reasons.append("missing context metadata")
        if not efforts:
            reasons.append("missing effort metadata")
        if not api_support:
            reasons.append("responses API unsupported")
        if "text" not in inputs:
            reasons.append("text input unsupported")
        if "text" not in outputs or image_route:
            reasons.append("image-only output route")
        if visibility in {"hide", "hidden", "disabled"}:
            reasons.append(f"visibility={visibility}")
        base_eligible = not reasons
        roles: list[str] = []
        if base_eligible:
            roles = [
                "proposer",
                "evidence_extractor",
                "critic",
                "risk_analyst",
                "minority_advocate",
                "verifier",
                "judge",
                "validator",
                "utility",
            ]
            if tool_support:
                roles.extend(["worker", "test_constructor", "integrator"])
        try:
            priority = max(0, int(meta.get("priority", 10_000)))
        except (TypeError, ValueError):
            priority = 10_000
        capability = ModelCapability(
            id=model_id,
            family=provider_family(model_id, owner),
            provider=owner or "unknown",
            listed_only=not metadata_matched,
            context_window=context,
            efforts=efforts,
            api_support=api_support,
            tool_support=tool_support,
            input_modalities=inputs,
            output_modalities=outputs,
            priority=priority,
            visibility=str(visibility) if visibility is not None else None,
            roles=roles,
            eligible=bool(roles),
            exclusion_reasons=reasons,
        )
        capabilities.append(capability)
    return sorted(capabilities, key=lambda item: (item.priority, item.id))


def extract_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    pieces: list[str] = []
    for output in payload.get("output", []) or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"}:
                value = content.get("text")
                if isinstance(value, str):
                    pieces.append(value)
    return "".join(pieces)


class ProxyTransport:
    def __init__(
        self,
        settings: ProxySettings,
        *,
        budget: CallBudget | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120,
        sync_state_root: Path | None = None,
        sync_warning_sink: Callable[[str], None] | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        self.settings = settings
        self.budget = budget
        self.sync_state_root = Path(sync_state_root or (resolve_state_root() / "v4"))
        self.sync_warning_sink = sync_warning_sink
        self.last_sync: SyncReport | None = None
        self.retry_policy = retry_policy or RetryPolicy()
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=settings.base_url,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
        )

    async def close(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    async def synchronize(self) -> CatalogueSyncResult:
        result = await synchronize_catalogue(
            self.client,
            config_path=self.settings.path,
            state_root=self.sync_state_root,
            catalogue_builder=merge_catalogues,
            exact_secrets=getattr(self.settings, "exact_secrets", ()),
            warning_sink=self.sync_warning_sink,
        )
        self.last_sync = result.report
        return result

    async def catalogue(self) -> list[ModelCapability]:
        result = await self.synchronize()
        if not result.authoritative_available:
            raise ProxyCallError(
                "authoritative /v1/models catalogue is unavailable"
            )
        return result.catalogue

    async def ask(
        self,
        *,
        run_id: str,
        participant: str,
        model: str,
        effort: str,
        prompt: str,
        stage: str,
        max_output_tokens: int = 5000,
        repack: Callable[[], str | Awaitable[str]] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
            "reasoning": {"effort": effort},
        }
        headers = {"X-Session-ID": f"{SESSION_NAMESPACE}:{run_id}:{participant}"}
        context_repacked = False
        attempt = 0
        if self.budget:
            self.budget.ensure_available(stage)
        while attempt < self.retry_policy.max_attempts:
            attempt += 1
            started = time.monotonic()
            try:
                response = await self.client.post(
                    "/v1/responses", json=payload, headers=headers
                )
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt >= self.retry_policy.max_attempts:
                    kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "transport error"
                    raise ProxyCallError(f"{kind} during {stage} after {attempt} attempts") from exc
                await asyncio.sleep(self.retry_policy.delay(attempt))
                continue
            except httpx.HTTPError as exc:
                raise ProxyCallError(f"transport error during {stage}: {exc}") from exc
            if response.status_code == 429:
                raise QuotaError(
                    f"quota response during {stage}: {response.text[:300]}",
                    status_code=429,
                )
            if response.status_code == 400:
                body = response.text[:1000]
                context_problem = any(
                    marker in body.lower()
                    for marker in ("context", "token", "too long", "maximum")
                )
                if context_problem and repack and not context_repacked:
                    replacement = repack()
                    if hasattr(replacement, "__await__"):
                        replacement = await replacement  # type: ignore[assignment]
                    payload["input"] = str(replacement)
                    context_repacked = True
                    attempt -= 1
                    continue
                error = ContextError if context_problem else ProxyCallError
                raise error(f"400 response during {stage}: {body}", status_code=400)
            if response.status_code in _RETRYABLE_STATUS:
                if attempt < self.retry_policy.max_attempts:
                    await asyncio.sleep(self.retry_policy.delay(attempt))
                    continue
                raise ProxyCallError(
                    f"{response.status_code} response during {stage} after {attempt} attempts: "
                    f"{response.text[:300]}",
                    status_code=response.status_code,
                )
            if response.is_error:
                raise ProxyCallError(
                    f"{response.status_code} response during {stage}: "
                    f"{response.text[:300]}",
                    status_code=response.status_code,
                )
            if self.budget:
                self.budget.consume(stage, model)
            data = response.json()
            usage = data.get("usage") or {}
            usage["latency_ms"] = int((time.monotonic() - started) * 1000)
            return extract_output_text(data), usage
        raise ProxyCallError(f"retry attempts exhausted during {stage}")


async def bounded(coro: Awaitable[Any], timeout: float) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ProxyCallError("operation timed out") from exc
