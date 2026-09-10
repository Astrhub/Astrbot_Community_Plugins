"""Convert the official OSV PyPI export into the market's bounded advisory snapshot."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import urllib.request
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .advisory import MAX_ADVISORY_SNAPSHOT_BYTES, LocalDependencyAdvisoryProvider

OSV_PYPI_EXPORT = "https://storage.googleapis.com/osv-vulnerabilities/PyPI/all.zip"
MAX_EXPORT_BYTES = 80 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024


def _version(value: str) -> str:
    return str(Version(value))


def _intervals(affected: dict[str, Any]) -> tuple[set[str], set[str], bool]:
    intervals: set[str] = set()
    fixes: set[str] = set()
    unresolved = False
    for item in affected.get("ranges", []):
        if item.get("type") not in {"ECOSYSTEM", "SEMVER"}:
            continue  # Git ranges are supplemented by OSV's explicit PyPI versions below.
        opened = False
        lower = ""
        try:
            for event in item["events"]:
                if len(event) != 1:
                    raise ValueError("ambiguous OSV event")
                kind, value = next(iter(event.items()))
                if kind == "introduced":
                    if opened:
                        raise ValueError("overlapping OSV interval")
                    opened = True
                    lower = "" if value == "0" else ">=" + _version(value)
                    if lower:
                        SpecifierSet(lower)
                elif kind in {"fixed", "last_affected", "limit"} and opened:
                    bound = _version(value)
                    upper = ("<=" if kind == "last_affected" else "<") + bound
                    interval = ",".join(filter(None, [lower, upper]))
                    SpecifierSet(interval)
                    intervals.add(interval)
                    if kind == "fixed":
                        fixes.add(bound)
                    opened = False
                else:
                    raise ValueError("unsupported OSV event")
            if opened:
                intervals.add(lower or ">=0.dev0")
        except (ValueError, KeyError, TypeError):
            unresolved = True
    return intervals, fixes, unresolved


def convert_osv_export(content: bytes, generated_at: datetime) -> tuple[bytes, dict[str, Any]]:
    if not content or len(content) > MAX_EXPORT_BYTES or generated_at.tzinfo is None:
        raise ValueError("invalid OSV export")
    counters: Counter[str] = Counter()
    records: dict[tuple[str, str], dict[str, Any]] = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sum(item.file_size for item in archive.infolist()) > MAX_UNPACKED_BYTES:
            raise ValueError("OSV export expands beyond the configured limit")
        for member in archive.infolist():
            if member.is_dir():
                continue
            if member.file_size > 16 * 1024 * 1024:
                raise ValueError("OSV record exceeds the configured limit")
            document = json.loads(archive.read(member))
            counters["upstream_records"] += 1
            if document.get("withdrawn"):
                counters["withdrawn_records"] += 1
                continue
            identifier = document["id"]
            severity = str(document.get("database_specific", {}).get("severity", "")).lower()
            severity = {"moderate": "medium"}.get(severity, severity)
            if identifier.startswith("MAL-"):
                severity = "critical"  # OpenSSF records identify known malicious packages.
            elif severity not in {"low", "medium", "high", "critical"}:
                # Unknown upstream severity is a manual-review classification, not a clean result.
                severity = "medium"
                counters["unscored_records_manual_review"] += 1
            for affected in document.get("affected", []):
                package = affected.get("package", {})
                if package.get("ecosystem") != "PyPI":
                    continue
                name = canonicalize_name(package["name"])
                intervals, fixes, unresolved = _intervals(affected)
                matchers = [SpecifierSet(item) for item in intervals]
                for value in affected.get("versions", []):
                    try:
                        version = Version(value)
                    except InvalidVersion:
                        counters["non_pep440_versions"] += 1
                        continue  # The runtime package contract also rejects these versions.
                    if not any(item.contains(version, prereleases=True) for item in matchers):
                        # In particular, preserve explicit prereleases excluded by PEP 440's
                        # '<final' semantics and versions supplied alongside Git ranges.
                        intervals.add("==" + str(version))
                conservative = unresolved or not intervals
                if conservative:
                    # Never silently drop an unrepresentable affected package from a clean scan.
                    intervals.add(">=0.dev0")
                    counters["conservative_package_ranges"] += 1
                for interval in sorted(intervals):
                    suffix = hashlib.sha256(interval.encode()).hexdigest()[:16]
                    key = (identifier + ":" + suffix, name)
                    records[key] = {
                        "id": key[0],
                        "package": name,
                        "affected": interval,
                        "fixed_versions": sorted(fixes, key=Version)[:100],
                        "severity": "medium"
                        if conservative and interval == ">=0.dev0"
                        else severity,
                    }
    if not records:
        raise ValueError("OSV export contains no usable PyPI advisories")
    payload = {
        "schema_version": "1",
        "database_version": "osv-pypi-" + hashlib.sha256(content).hexdigest()[:20],
        "source": "osv.dev-PyPI",
        "generated_at": generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "advisories": [records[key] for key in sorted(records)],
        "packages": [],
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    if len(encoded) > MAX_ADVISORY_SNAPSHOT_BYTES:
        raise ValueError("converted OSV snapshot exceeds the provider limit")
    LocalDependencyAdvisoryProvider(encoded)
    return encoded, {
        **counters,
        "advisories": len(records),
        "snapshot_bytes": len(encoded),
        "generated_at": payload["generated_at"],
        "database_version": payload["database_version"],
        "upstream_url": OSV_PYPI_EXPORT,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = urllib.request.Request(OSV_PYPI_EXPORT, headers={"User-Agent": "Astrhub-OSV-Sync/1"})
    with urllib.request.urlopen(request, timeout=90) as response:
        generated = parsedate_to_datetime(response.headers["Last-Modified"])
        content = response.read(MAX_EXPORT_BYTES + 1)
    now = datetime.now(UTC)
    if not now - timedelta(hours=48) <= generated <= now + timedelta(minutes=5):
        raise ValueError("OSV export timestamp is stale or in the future")
    encoded, report = convert_osv_export(content, generated)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".new")
    try:
        temporary.write_bytes(encoded)
        temporary.chmod(0o640)
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
