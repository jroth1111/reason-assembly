from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

import catalogue_sync
from artifacts import SecretGuard
from catalogue_sync import (
    AliasConfigurationError,
    prune_smart_aliases,
    synchronize_catalogue,
)
from ccycouncil import print_models
from contracts import HealthResult
from transport import ProxyTransport, merge_catalogues


def raw_payload(*model_ids: str) -> dict:
    return {
        "object": "list",
        "data": [{"id": model_id, "owned_by": "test"} for model_id in model_ids],
    }


def metadata_payload(*model_ids: str, healthy: bool = True) -> dict:
    return {
        "models": [
            {
                "slug": model_id,
                "owned_by": "test",
                "context_window": 100_000,
                "supported_reasoning_levels": ["low", "medium"],
                "supported_in_api": True,
                "supports_parallel_tool_calls": True,
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "credential_availability": {
                    "status": "available" if healthy else "cooldown",
                    "eligible_credentials": 1 if healthy else 0,
                    "total_credentials": 1,
                },
            }
            for model_id in model_ids
        ]
    }


def proxy_config(*candidates: str, secret: str = "client-secret-value") -> str:
    rows = "\n".join(f"      - {candidate}" for candidate in candidates)
    return (
        "host: 127.0.0.1\n"
        "port: 8317\n"
        "api-keys:\n"
        f"  - {secret}\n"
        "smart-aliases:\n"
        "  worker:\n"
        "    candidates:\n"
        f"{rows}\n"
        "    failover: silent\n"
    )


def write_config(path: Path, value: str) -> Path:
    path.write_text(value)
    path.chmod(0o600)
    return path


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://proxy.invalid",
        transport=httpx.MockTransport(handler),
    )


def is_metadata_request(request: httpx.Request) -> bool:
    return request.url.params.get("client_version") == "ccycouncil-v4"


@pytest.mark.asyncio
async def test_exact_catalogue_equality_uses_one_snapshot_and_writes_receipt(
    tmp_path,
):
    config = write_config(tmp_path / "config.yaml", proxy_config("alpha", "beta"))
    calls = {"raw": 0, "metadata": 0}

    def handler(request):
        key = "metadata" if is_metadata_request(request) else "raw"
        calls[key] += 1
        payload = (
            metadata_payload("alpha", "beta")
            if key == "metadata"
            else raw_payload("alpha", "beta")
        )
        return httpx.Response(200, json=payload)

    warnings: list[str] = []
    state = tmp_path / "state"
    async with mock_client(handler) as client:
        result = await synchronize_catalogue(
            client,
            config_path=config,
            state_root=state,
            catalogue_builder=merge_catalogues,
            exact_secrets={"client-secret-value"},
            warning_sink=warnings.append,
        )

    assert result.authoritative_available
    assert result.report.status == "ok"
    assert result.report.equality["all"]
    assert result.report.counts == {"raw": 2, "metadata": 2, "council": 2}
    assert result.report.attempts == 1
    assert calls == {"raw": 1, "metadata": 1}
    assert warnings == []
    receipt = json.loads((state / "catalogue-sync-latest.json").read_text())
    assert receipt["schema_version"] == 4
    assert receipt["outcome"] == "ok"
    assert receipt["counts"]["raw"] == 2
    assert "client-secret-value" not in json.dumps(receipt)
    assert stat.S_IMODE(
        (state / "catalogue-sync-latest.json").stat().st_mode
    ) == 0o600
    historical = list((state / "sync-receipts").glob("*.json"))
    assert len(historical) == 1
    assert stat.S_IMODE(historical[0].stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_transient_catalogue_mismatch_retries_once_and_recovers(tmp_path):
    config = write_config(tmp_path / "config.yaml", proxy_config("alpha", "beta"))
    calls = {"raw": 0, "metadata": 0}

    def handler(request):
        key = "metadata" if is_metadata_request(request) else "raw"
        calls[key] += 1
        if key == "raw" and calls[key] == 1:
            return httpx.Response(200, json=raw_payload("alpha"))
        return httpx.Response(
            200,
            json=(
                metadata_payload("alpha", "beta")
                if key == "metadata"
                else raw_payload("alpha", "beta")
            ),
        )

    warnings: list[str] = []
    async with mock_client(handler) as client:
        result = await synchronize_catalogue(
            client,
            config_path=config,
            state_root=tmp_path / "state",
            catalogue_builder=merge_catalogues,
            warning_sink=warnings.append,
        )

    assert result.report.attempts == 2
    assert result.report.equality["all"]
    assert result.report.status == "ok"
    assert calls == {"raw": 2, "metadata": 2}
    assert warnings == []


@pytest.mark.asyncio
async def test_persistent_mismatch_uses_raw_authority_and_listed_only_fallback(
    tmp_path,
):
    config = write_config(tmp_path / "config.yaml", proxy_config("alpha", "raw-only"))
    calls = {"raw": 0, "metadata": 0}

    def handler(request):
        key = "metadata" if is_metadata_request(request) else "raw"
        calls[key] += 1
        return httpx.Response(
            200,
            json=(
                metadata_payload("alpha", "metadata-only")
                if key == "metadata"
                else raw_payload("alpha", "raw-only")
            ),
        )

    warnings: list[str] = []
    async with mock_client(handler) as client:
        result = await synchronize_catalogue(
            client,
            config_path=config,
            state_root=tmp_path / "state",
            catalogue_builder=merge_catalogues,
            warning_sink=warnings.append,
        )

    by_id = {item.id: item for item in result.catalogue}
    assert set(by_id) == {"alpha", "raw-only"}
    assert by_id["raw-only"].listed_only
    assert not by_id["raw-only"].eligible
    assert "metadata-only" not in by_id
    assert result.report.equality == {
        "raw_metadata": False,
        "raw_council": True,
        "metadata_council": False,
        "all": False,
    }
    assert result.report.status == "degraded"
    assert result.report.attempts == 2
    assert calls == {"raw": 2, "metadata": 2}
    assert any("/v1/models is authoritative" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_metadata_failure_warns_and_keeps_raw_models_listed_only(tmp_path):
    config = write_config(tmp_path / "config.yaml", proxy_config("alpha"))
    calls = {"raw": 0, "metadata": 0}

    def handler(request):
        key = "metadata" if is_metadata_request(request) else "raw"
        calls[key] += 1
        if key == "metadata":
            return httpx.Response(503, text="metadata unavailable")
        return httpx.Response(200, json=raw_payload("alpha"))

    warnings: list[str] = []
    async with mock_client(handler) as client:
        result = await synchronize_catalogue(
            client,
            config_path=config,
            state_root=tmp_path / "state",
            catalogue_builder=merge_catalogues,
            warning_sink=warnings.append,
        )

    assert result.authoritative_available
    assert [item.id for item in result.catalogue] == ["alpha"]
    assert result.catalogue[0].listed_only
    assert not result.catalogue[0].eligible
    assert result.report.status == "degraded"
    assert result.report.attempts == 2
    assert result.report.equality["raw_council"]
    assert calls == {"raw": 2, "metadata": 2}
    assert any("metadata is unavailable" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_sync_reports_alias_that_becomes_empty(tmp_path):
    config = write_config(tmp_path / "config.yaml", proxy_config("stale"))

    def handler(request):
        return httpx.Response(
            200,
            json=(
                metadata_payload("alpha")
                if is_metadata_request(request)
                else raw_payload("alpha")
            ),
        )

    warnings: list[str] = []
    async with mock_client(handler) as client:
        result = await synchronize_catalogue(
            client,
            config_path=config,
            state_root=tmp_path / "state",
            catalogue_builder=merge_catalogues,
            warning_sink=warnings.append,
        )

    assert result.report.removed_candidates == ["stale"]
    assert result.report.empty_aliases == ["worker"]
    assert result.report.aliases["worker"]["candidates"] == []
    assert result.report.aliases["worker"]["empty"]
    assert result.report.status == "degraded"
    assert any("no catalogued candidates" in warning for warning in warnings)


def test_noop_alias_pruning_preserves_bytes_inode_and_mode(tmp_path):
    config = write_config(
        tmp_path / "config.yaml",
        "# leading comment\n" + proxy_config("alpha", "beta"),
    )
    before = config.read_bytes()
    before_stat = config.stat()
    result = prune_smart_aliases(
        config,
        {"alpha", "beta"},
        lock_path=tmp_path / "state" / "sync.lock",
    )

    after_stat = config.stat()
    assert not result.changed
    assert config.read_bytes() == before
    assert after_stat.st_ino == before_stat.st_ino
    assert stat.S_IMODE(after_stat.st_mode) == 0o600
    assert after_stat.st_uid == before_stat.st_uid
    assert after_stat.st_gid == before_stat.st_gid
    assert stat.S_IMODE(
        (tmp_path / "state" / "sync.lock").stat().st_mode
    ) == 0o600


def test_targeted_yaml_pruning_preserves_unrelated_text_comments_and_credentials(
    tmp_path,
):
    original = (
        "# preserve this header\n"
        "host: 127.0.0.1\n"
        "api-keys:\n"
        "  - credential-must-remain-byte-identical # credential comment\n"
        "smart-aliases:\n"
        "  worker:\n"
        "    # preserve candidate policy\n"
        "    candidates:\n"
        "      - alpha # keep comment\n"
        "      - stale-one # remove this candidate only\n"
        "      - beta\n"
        "    failover: silent # preserve route comment\n"
        "  rescue:\n"
        "    candidates:\n"
        "      - model: stale-two\n"
        "        weight: 3\n"
        "      - model: alpha\n"
        "        weight: 1\n"
        "other-setting: unchanged\n"
    )
    config = write_config(tmp_path / "config.yaml", original)
    before_stat = config.stat()
    result = prune_smart_aliases(
        config,
        {"alpha", "beta"},
        lock_path=tmp_path / "state" / "sync.lock",
    )

    expected = original.replace(
        "      - stale-one # remove this candidate only\n", ""
    ).replace("      - model: stale-two\n        weight: 3\n", "")
    assert config.read_text() == expected
    assert result.removed_candidates == ["stale-one", "stale-two"]
    assert result.aliases_after == {
        "worker": ["alpha", "beta"],
        "rescue": ["alpha"],
    }
    after_stat = config.stat()
    assert stat.S_IMODE(after_stat.st_mode) == 0o600
    assert after_stat.st_uid == before_stat.st_uid
    assert after_stat.st_gid == before_stat.st_gid
    assert "credential-must-remain-byte-identical" in config.read_text()


def test_pruning_last_candidate_writes_explicit_empty_list(tmp_path):
    config = write_config(
        tmp_path / "config.yaml",
        "api-keys:\n"
        "  - client-secret-value\n"
        "smart-aliases:\n"
        "  empty-route:\n"
        "    candidates: # keep this note\n"
        "      - gone\n"
        "    failover: silent\n",
    )
    result = prune_smart_aliases(
        config,
        {"other"},
        lock_path=tmp_path / "state" / "sync.lock",
    )

    parsed = yaml.safe_load(config.read_text())
    assert parsed["smart-aliases"]["empty-route"]["candidates"] == []
    assert "candidates: [] # keep this note" in config.read_text()
    assert result.empty_aliases == ["empty-route"]


def test_malformed_alias_configuration_is_refused_without_changes(tmp_path):
    config = write_config(
        tmp_path / "config.yaml",
        "api-keys:\n  - client-secret-value\nsmart-aliases: [\n",
    )
    before = config.read_bytes()
    with pytest.raises(AliasConfigurationError, match="malformed"):
        prune_smart_aliases(
            config,
            {"alpha"},
            lock_path=tmp_path / "state" / "sync.lock",
        )
    assert config.read_bytes() == before


def test_atomic_write_failure_leaves_original_and_no_credential_backup(
    tmp_path, monkeypatch
):
    config = write_config(tmp_path / "config.yaml", proxy_config("alpha", "stale"))
    before = config.read_bytes()

    def fail_replace(source, target):
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(catalogue_sync.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        prune_smart_aliases(
            config,
            {"alpha"},
            lock_path=tmp_path / "state" / "sync.lock",
        )

    assert config.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []
    assert [path.name for path in tmp_path.iterdir()] == ["config.yaml", "state"]


def test_concurrent_pruning_is_serialized_and_idempotent(tmp_path):
    config = write_config(
        tmp_path / "config.yaml",
        proxy_config("alpha", "stale-one", "stale-two"),
    )
    lock = tmp_path / "state" / "sync.lock"

    def invoke():
        return prune_smart_aliases(config, {"alpha"}, lock_path=lock)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: invoke(), range(8)))

    assert sum(result.changed for result in results) == 1
    assert yaml.safe_load(config.read_text())["smart-aliases"]["worker"][
        "candidates"
    ] == ["alpha"]
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_advertised_unhealthy_candidate_is_not_pruned(tmp_path):
    config = write_config(tmp_path / "config.yaml", proxy_config("cooldown-model"))

    def handler(request):
        return httpx.Response(
            200,
            json=(
                metadata_payload("cooldown-model", healthy=False)
                if is_metadata_request(request)
                else raw_payload("cooldown-model")
            ),
        )

    async with mock_client(handler) as client:
        result = await synchronize_catalogue(
            client,
            config_path=config,
            state_root=tmp_path / "state",
            catalogue_builder=merge_catalogues,
            warning_sink=lambda warning: None,
        )

    assert not result.report.prune_changed
    assert result.report.aliases["worker"]["candidates"] == ["cooldown-model"]
    assert result.report.aliases["worker"]["removed"] == []


@pytest.mark.asyncio
async def test_prune_failure_warns_redacted_and_does_not_block_model_ids(
    tmp_path, monkeypatch
):
    secret = "test-secret-value-that-must-never-appear"
    config = write_config(
        tmp_path / "config.yaml",
        proxy_config("alpha", "stale", secret=secret),
    )

    def fail_prune(*args, **kwargs):
        raise RuntimeError(f"write failed with {secret}")

    monkeypatch.setattr(catalogue_sync, "prune_smart_aliases", fail_prune)

    def handler(request):
        return httpx.Response(
            200,
            json=(
                metadata_payload("alpha")
                if is_metadata_request(request)
                else raw_payload("alpha")
            ),
        )

    warnings: list[str] = []
    async with mock_client(handler) as client:
        result = await synchronize_catalogue(
            client,
            config_path=config,
            state_root=tmp_path / "state",
            catalogue_builder=merge_catalogues,
            exact_secrets={secret},
            warning_sink=warnings.append,
        )

    assert [item.id for item in result.catalogue] == ["alpha"]
    assert result.authoritative_available
    assert not result.report.prune_succeeded
    assert result.report.status == "degraded"
    serialized = json.dumps(result.report.to_dict(SecretGuard({secret})))
    assert secret not in serialized
    assert all(secret not in warning for warning in warnings)
    assert "stale" in config.read_text()


@pytest.mark.asyncio
async def test_proxy_transport_synchronizes_on_every_catalogue_read(tmp_path):
    config = write_config(tmp_path / "config.yaml", proxy_config("alpha"))
    calls = {"raw": 0, "metadata": 0}

    def handler(request):
        key = "metadata" if is_metadata_request(request) else "raw"
        calls[key] += 1
        return httpx.Response(
            200,
            json=(
                metadata_payload("alpha")
                if key == "metadata"
                else raw_payload("alpha")
            ),
        )

    settings = SimpleNamespace(
        path=config,
        base_url="http://proxy.invalid",
        api_key="client-secret-value",
        exact_secrets={"client-secret-value"},
    )
    async with mock_client(handler) as client:
        transport = ProxyTransport(
            settings,
            client=client,
            sync_state_root=tmp_path / "state",
            sync_warning_sink=lambda warning: None,
        )
        assert [item.id for item in await transport.catalogue()] == ["alpha"]
        assert [item.id for item in await transport.catalogue()] == ["alpha"]

    assert calls == {"raw": 2, "metadata": 2}
    assert transport.last_sync is not None
    assert transport.last_sync.equality["all"]


def test_doctor_all_models_json_includes_sync_and_alias_diagnostics(capsys):
    capability = merge_catalogues(
        raw_payload("alpha"),
        metadata_payload("alpha"),
    )[0]
    health = HealthResult(
        model="alpha",
        family=capability.family,
        status="healthy",
    )
    sync = {
        "status": "ok",
        "aliases": {
            "worker": {
                "candidates": ["alpha"],
                "removed": [],
                "empty": False,
            }
        },
        "removed_candidates": [],
        "empty_aliases": [],
        "pruning": {"attempted": True, "succeeded": True, "changed": False},
    }

    print_models(
        [capability],
        [health],
        True,
        sync=sync,
        include_sync=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["models"][0]["health"]["status"] == "healthy"
    assert payload["sync"]["status"] == "ok"
    assert payload["alias_resolution"]["aliases"]["worker"]["candidates"] == [
        "alpha"
    ]
