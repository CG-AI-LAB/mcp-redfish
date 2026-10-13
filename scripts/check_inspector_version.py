#!/usr/bin/env python3
# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""Report when a newer major MCP Inspector release is available on npm."""

from __future__ import annotations

import argparse
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from major_version_findings import MajorVersionFinding  # noqa: E402

from e2e.inspector_version import (  # noqa: E402
    fetch_npm_versions,
    latest_version_on_npm,
    load_inspector_version_config,
    major_version,
)


def collect_inspector_major_update(
    cutoff: datetime | None = None,
) -> MajorVersionFinding | None:
    config = load_inspector_version_config()

    try:
        latest_version = latest_version_on_npm(config.package)
    except urllib.error.URLError as exc:
        print(f"::warning title=npm lookup failed::{config.package}: {exc}")
        return MajorVersionFinding(
            package=config.package,
            locked_version=config.locked_version,
            latest_version="Unknown",
            registry="npm",
            eligibility_unknown=True,
        )

    if major_version(latest_version) <= major_version(config.locked_version):
        return None

    eligible_version = None
    eligibility_unknown = False
    if cutoff is not None:
        try:
            eligible = fetch_npm_versions(config.package, exclude_newer=cutoff)
        except urllib.error.URLError as exc:
            print(
                f"::warning title=npm eligibility lookup failed::{config.package}: {exc}"
            )
            eligibility_unknown = True
        else:
            if eligible and major_version(eligible[-1]) > major_version(
                config.locked_version
            ):
                eligible_version = eligible[-1]

    return MajorVersionFinding(
        package=config.package,
        locked_version=config.locked_version,
        latest_version=latest_version,
        registry="npm",
        eligible_version=eligible_version,
        eligibility_unknown=eligibility_unknown,
    )


def format_summary_markdown(finding: MajorVersionFinding) -> str:
    description = (
        "The npm release lookup failed; the current major-version status is unknown."
        if finding.latest_version == "Unknown"
        else (
            "A newer **major** MCP Inspector release is available on npm. "
            "Minor and patch updates are handled automatically within "
            "`e2e/inspector-version.toml`; major bumps require manual "
            "review of the Inspector migration guide and e2e CLI usage."
        )
    )
    return "\n".join(
        [
            "## MCP Inspector major-version indicator",
            "",
            description,
            "",
            "| Package | Locked | Latest available | Newest eligible major |",
            "| --- | --- | --- | --- |",
            (
                f"| `{finding.package}` | `{finding.locked_version}` | "
                f"`{finding.latest_version}` | "
                f"{finding.eligible_version or ('Unknown' if finding.eligibility_unknown else 'None confirmed')} |"
            ),
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report when a newer major MCP Inspector release is on npm."
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Optional path for a GitHub Actions job summary markdown file.",
    )
    args = parser.parse_args(argv)

    finding = collect_inspector_major_update(datetime.now(UTC) - timedelta(days=7))

    if finding is None:
        config = load_inspector_version_config()
        print(
            f"{config.package} is on the latest major version "
            f"({config.locked_version})."
        )
        return 0

    if finding.latest_version == "Unknown":
        print(
            f"{finding.package} latest major status is unknown due to npm lookup failure."
        )
        print(
            f"::warning title=npm lookup status unknown::{finding.package} "
            "latest version could not be determined"
        )
    else:
        print(
            f"{finding.package} has a newer major release on npm: "
            f"locked {finding.locked_version}, latest {finding.latest_version}."
        )
        print(
            f"::warning title=New major Inspector version available::{finding.package} "
            f"{finding.locked_version} -> {finding.latest_version}"
        )

    if args.summary_file is not None:
        args.summary_file.write_text(format_summary_markdown(finding), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
