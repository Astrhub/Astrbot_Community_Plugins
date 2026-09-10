import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime

import pytest

from app.artifacts.advisory import DependencyPackage, LocalDependencyAdvisoryProvider
from app.artifacts.osv_sync import convert_osv_export


def export(affected, **changes):
    record = {
        "id": "GHSA-test",
        "affected": [{"package": {"ecosystem": "PyPI", "name": "Demo_Pkg"}, **affected}],
        **changes,
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("record.json", json.dumps(record))
    content, report = convert_osv_export(stream.getvalue(), datetime.now(UTC))
    return LocalDependencyAdvisoryProvider(content), report


def matches(provider, version):
    result = asyncio.run(provider.query([DependencyPackage("demo-pkg", version)], max_age_hours=48))
    return result.advisories


def test_disjoint_ranges_keep_fixed_versions_and_gaps_clear():
    provider, _ = export(
        {
            "ranges": [
                {
                    "type": "ECOSYSTEM",
                    "events": [
                        {"introduced": "0"},
                        {"fixed": "1.2"},
                        {"introduced": "2.0"},
                        {"last_affected": "2.5"},
                    ],
                }
            ]
        }
    )
    assert matches(provider, "1.1")
    assert not matches(provider, "1.2")
    assert not matches(provider, "1.9")
    assert matches(provider, "2.5")
    assert not matches(provider, "2.6")


def test_explicit_prereleases_and_git_versions_are_not_lost():
    provider, _ = export(
        {
            "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "1.0"}]}],
            "versions": ["1.0rc1", "invalid"],
        }
    )
    assert matches(provider, "1.0rc1")
    assert not matches(provider, "1.0")
    git, _ = export(
        {"ranges": [{"type": "GIT", "events": [{"introduced": "abcdef"}]}], "versions": ["2.1"]}
    )
    assert matches(git, "2.1")
    assert not matches(git, "2.2")


def test_unrepresentable_bounds_require_manual_review_instead_of_false_clean():
    provider, report = export(
        {
            "ranges": [
                {
                    "type": "ECOSYSTEM",
                    "events": [{"introduced": "0"}, {"last_affected": "2.6.0+cu124"}],
                }
            ],
            "versions": ["2.6.0+cu124"],
        }
    )
    assert report["conservative_package_ranges"] == 1
    assert matches(provider, "3.0")[0].severity == "medium"


def test_malicious_packages_are_critical_and_missing_severity_is_manual():
    provider, _ = export({"versions": ["1.0"]}, id="MAL-2026-1")
    assert matches(provider, "1.0")[0].severity == "critical"
    provider, report = export({"versions": ["1.0"]})
    assert matches(provider, "1.0")[0].severity == "medium"
    assert report["unscored_records_manual_review"] == 1


def test_empty_withdrawn_or_non_pypi_export_cannot_replace_a_working_snapshot():
    with pytest.raises(ValueError, match="no usable"):
        export({"versions": ["1.0"]}, withdrawn="2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="no usable"):
        export({"package": {"ecosystem": "npm", "name": "demo"}, "versions": ["1.0"]})
