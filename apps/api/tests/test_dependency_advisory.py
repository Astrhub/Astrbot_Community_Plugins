from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.artifacts.advisory import (
    AdvisoryStatus,
    DependencyPackage,
    HttpsDependencyAdvisoryProvider,
    LocalDependencyAdvisoryProvider,
    UnavailableDependencyAdvisoryProvider,
)

FIXTURE = Path(__file__).parent / "fixtures/dependency_advisory_v1.json"


async def public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("8.8.8.8",)


def snapshot(*, generated_at: datetime | None = None) -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "database_version": "osv-2026-07-16",
            "source": "local-osv-fixture",
            "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
            "advisories": [
                {
                    "id": "GHSA-demo-1234",
                    "package": "Demo_Lib",
                    "affected": ">=1,<2",
                    "fixed_versions": ["2.0.0"],
                    "severity": "high",
                },
                {
                    "id": "GHSA-withdrawn-1",
                    "package": "demo-lib",
                    "affected": "==1.2.3",
                    "severity": "critical",
                    "withdrawn": True,
                },
            ],
            "packages": [
                {
                    "name": "demo-lib",
                    "version": "1.2.3",
                    "license": "GPL-3.0-only",
                    "withdrawn": True,
                }
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def test_local_advisory_provider_returns_versioned_structured_matches() -> None:
    async def scenario():
        provider = LocalDependencyAdvisoryProvider.from_file(
            FIXTURE,
            clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC),
        )
        return await provider.query(
            (DependencyPackage("Demo_Lib", "1.2.3"),),
            max_age_hours=24,
        )

    result = asyncio.run(scenario())

    assert result.status is AdvisoryStatus.OK
    assert result.database_version == "fixture-db-2026-07-16"
    assert result.snapshot_sha256
    assert [
        (item.advisory_id, item.affected, item.fixed_versions) for item in result.advisories
    ] == [
        ("GHSA-demo-1234", ">=1,<2", ("2.0.0",)),
        ("GHSA-withdrawn-1", "==1.2.3", ()),
    ]
    assert result.packages[0].license_expression == "GPL-3.0-only"
    assert result.packages[0].withdrawn is True


def test_local_advisory_provider_separates_stale_from_clean() -> None:
    async def scenario():
        provider = LocalDependencyAdvisoryProvider(
            snapshot(generated_at=datetime.now(UTC) - timedelta(days=7))
        )
        return await provider.query(
            (DependencyPackage("demo-lib", "3.0.0"),),
            max_age_hours=24,
        )

    result = asyncio.run(scenario())

    assert result.status is AdvisoryStatus.STALE
    assert result.advisories == ()
    assert not result.error_code


def test_local_advisory_provider_rejects_future_snapshot_as_not_clean() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)

    async def scenario():
        provider = LocalDependencyAdvisoryProvider(
            snapshot(generated_at=now + timedelta(minutes=10)),
            clock=lambda: now,
        )
        return await provider.query(
            (DependencyPackage("demo-lib", "3.0.0"),),
            max_age_hours=24,
        )

    result = asyncio.run(scenario())

    assert result.status is AdvisoryStatus.ERROR
    assert result.error_code == "dependency_advisory_timestamp_future"
    assert result.advisories == ()


def test_unavailable_provider_is_not_queried_not_clean() -> None:
    result = asyncio.run(
        UnavailableDependencyAdvisoryProvider().query(
            (DependencyPackage("demo-lib", "1.2.3"),),
            max_age_hours=24,
        )
    )

    assert result.status is AdvisoryStatus.NOT_QUERIED
    assert result.error_code == "dependency_advisory_unavailable"


def test_https_provider_is_bounded_and_never_returns_credentials() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=snapshot())

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpsDependencyAdvisoryProvider(
            "https://advisory.example.test/v1/query",
            api_token="do-not-persist",
            client=client,
            resolver=public_resolver,
        )
        try:
            return await provider.query(
                (DependencyPackage("demo-lib", "1.2.3"),),
                max_age_hours=24,
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())

    assert result.status is AdvisoryStatus.OK
    assert requests[0].headers["Authorization"] == "Bearer do-not-persist"
    assert "do-not-persist" not in repr(result)
    assert "advisory.example.test" not in repr(result)


def test_https_provider_rejects_private_dns_resolution_before_request() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=snapshot())

    async def private_resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("127.0.0.1", "10.0.0.5")

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpsDependencyAdvisoryProvider(
            "https://advisory.example.test/v1/query",
            client=client,
            resolver=private_resolver,
        )
        try:
            return await provider.query(
                (DependencyPackage("demo-lib", "1.2.3"),),
                max_age_hours=24,
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())

    assert result.status is AdvisoryStatus.ERROR
    assert result.error_code == "dependency_advisory_endpoint_untrusted"
    assert requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://advisory.example.test/query",
        "https://user:secret@advisory.example.test/query",
        "https://127.0.0.1/query",
        "https://advisory.example.test/query?token=secret",
    ],
)
def test_https_provider_rejects_unsafe_endpoint(url: str) -> None:
    with pytest.raises(ValueError, match="url_invalid"):
        HttpsDependencyAdvisoryProvider(url)
