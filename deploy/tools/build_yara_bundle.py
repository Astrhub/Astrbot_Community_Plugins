"""Build a versioned, reviewed Yara-Rules subset; never enable the upstream tree wholesale."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import yara

FILES = [
    "malware/MALW_LinuxBew.yar",
    "malware/MALW_LinuxHelios.yar",
    "malware/MALW_LinuxMoose.yar",
    "malware/APT_Seaduke.yar",
    "malware/RAT_FlyingKitten.yar",
    "webshells/WShell_ChinaChopper.yar",
    "webshells/WShell_ASPXSpy.yar",
    "webshells/WShell_Drupalgeddon2_icos.yar",
    "webshells/WShell_PHP_Anuna.yar",
    "webshells/WShell_PHP_in_images.yar",
]
DECLARATION = re.compile(
    r"(?m)^((?:(?:private|global)\s+)?)rule\s+(\w+)\s*(?::\s*([^\{\n]+))?\s*\{"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    commit = subprocess.check_output(
        ["git", "-C", str(args.repository), "rev-parse", "HEAD"], text=True
    ).strip()
    chunks = [
        "// Source: https://github.com/Yara-Rules/rules; GPL-2.0; commit " + commit
    ]
    count = 0
    for name in FILES:
        source = (args.repository / name).read_text()
        tag = (
            "market_medium"
            if name.endswith("WShell_PHP_in_images.yar")
            else "market_high"
        )

        def annotate(match):
            tags = (match[3] or "").split()
            if any(item.startswith("market_") for item in tags):
                raise ValueError("upstream unexpectedly supplies market severity tags")
            return f"{match[1]}rule {match[2]} : {' '.join([*tags, tag])} {{"

        source, rules = DECLARATION.subn(annotate, source)
        if not rules:
            raise ValueError(
                "reviewed rule file no longer contains recognized declarations: " + name
            )
        count += rules
        chunks.extend(["// Original file: " + name, source])
    content = "\n\n".join(chunks).encode()
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("ruleset exceeds scanner limit")
    yara.compile(source=content.decode(), includes=False, error_on_warning=True)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "market.yar").write_bytes(content)
    shutil.copy2(args.repository / "LICENSE", args.output / "LICENSE")
    manifest = {
        "version": f"yara-community-{commit[:12]}-v1",
        "source": "Yara-Rules/rules",
        "commit": commit,
        "sha256": hashlib.sha256(content).hexdigest(),
        "rule_count": count,
        "files": FILES,
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
