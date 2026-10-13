#!/usr/bin/env python3
# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""Create or update a GitHub issue when newer major versions are available."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from check_inspector_version import collect_inspector_major_update  # noqa: E402
from check_major_dependency_updates import collect_outdated_major_versions  # noqa: E402
from check_uv_version import fetch_uv_releases  # noqa: E402
from major_version_findings import MajorVersionFinding  # noqa: E402

ISSUE_TITLE = "Dependency updates available"
LEGACY_ISSUE_TITLE = "Major version updates available"
ISSUE_LABEL = "major-version-indicator"
ISSUE_MARKER = "<!-- major-version-indicator-issue -->"
GITHUB_API_VERSION = "2022-11-28"
DEX_RELEASES_URL = "https://api.github.com/repos/dexidp/dex/releases"
DEX_IMAGE_SCRIPT = ROOT / "e2e/scripts/dex.sh"


def collect_findings() -> list[MajorVersionFinding]:
    cutoff = datetime.now(UTC) - timedelta(days=7)
    findings = list(collect_outdated_major_versions(cutoff))
    inspector_finding = collect_inspector_major_update(cutoff)
    if inspector_finding is not None:
        findings.append(inspector_finding)
    dex_finding = collect_dex_release_update(cutoff)
    if dex_finding is not None:
        findings.append(dex_finding)
    pinned_uv = os.environ.get("UV_VERSION")
    if pinned_uv:
        try:
            uv_releases = fetch_uv_releases()
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            print("::warning title=uv lookup failed::uv release status is unknown")
            findings.append(
                MajorVersionFinding(
                    package="uv installer",
                    locked_version=pinned_uv,
                    latest_version="Unknown",
                    registry="uv",
                    eligibility_unknown=True,
                )
            )
        else:
            latest, _ = uv_releases[-1]
            eligible = [
                (version, published)
                for version, published in uv_releases
                if published is not None
                and published <= cutoff
                and tuple(map(int, version.split(".")))
                > tuple(map(int, pinned_uv.split(".")))
            ]
            unknown_eligibility = any(
                published is None
                and tuple(map(int, version.split(".")))
                > tuple(map(int, pinned_uv.split(".")))
                for version, published in uv_releases
            )
            if tuple(map(int, latest.split("."))) > tuple(
                map(int, pinned_uv.split("."))
            ):
                findings.append(
                    MajorVersionFinding(
                        package="uv installer",
                        locked_version=pinned_uv,
                        latest_version=latest,
                        registry="uv",
                        eligible_version=eligible[-1][0] if eligible else None,
                        eligibility_unknown=unknown_eligibility and not eligible,
                    )
                )
    return findings


def collect_dex_release_update(cutoff: datetime) -> MajorVersionFinding | None:
    script = DEX_IMAGE_SCRIPT.read_text(encoding="utf-8")
    pin = re.search(
        r'DEX_IMAGE="\$\{DEX_IMAGE:-ghcr\.io/dexidp/dex:(v?\d+\.\d+\.\d+)@sha256:',
        script,
    )
    if pin is None:
        print("::warning title=Dex version lookup failed::pinned image tag is unknown")
        return MajorVersionFinding(
            package="Dex image (GHCR)",
            locked_version="Unknown",
            latest_version="Unknown",
            registry="GHCR",
            eligibility_unknown=True,
        )
    pinned = pin.group(1).removeprefix("v")
    pinned_version = Version(pinned)
    releases = []
    page = 1
    try:
        while True:
            request = urllib.request.Request(
                f"{DEX_RELEASES_URL}?per_page=100&page={page}",
                headers={"Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if not isinstance(payload, list):
                raise ValueError("Expected Dex release history")
            for release in payload:
                if release.get("draft") or release.get("prerelease"):
                    continue
                tag = release.get("tag_name")
                timestamp = release.get("published_at")
                if not isinstance(tag, str) or not isinstance(timestamp, str):
                    continue
                try:
                    version = Version(tag.removeprefix("v"))
                    published = datetime.fromisoformat(timestamp)
                except (InvalidVersion, ValueError):
                    continue
                if published.utcoffset() is not None:
                    releases.append((version, published))
            if len(payload) < 100:
                break
            page += 1
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
        print(f"::warning title=Dex release lookup failed::{type(exc).__name__}")
        return MajorVersionFinding(
            package="Dex image (GHCR)",
            locked_version=pinned,
            latest_version="Unknown",
            registry="GHCR",
            eligibility_unknown=True,
        )

    if not releases:
        return None
    latest, _ = max(releases, key=lambda release: release[0])
    if latest <= pinned_version:
        return None
    eligible = [
        version
        for version, published in releases
        if version > pinned_version and published <= cutoff
    ]
    return MajorVersionFinding(
        package="Dex image (GHCR)",
        locked_version=pinned,
        latest_version=str(latest),
        registry="GHCR",
        eligible_version=str(max(eligible)) if eligible else None,
        eligibility_unknown=False,
    )


def format_issue_body(
    findings: list[MajorVersionFinding], workflow_run_url: str | None
) -> str:
    updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    unknown_findings = [
        finding for finding in findings if finding.latest_version == "Unknown"
    ]
    confirmed_findings = [
        finding for finding in findings if finding.latest_version != "Unknown"
    ]
    if unknown_findings and confirmed_findings:
        status = (
            "The scheduled dependency checks found updates to review, but one or "
            "more registry lookups failed. Those package statuses are unknown; "
            "this issue remains open until the checks can confirm them."
        )
    elif unknown_findings:
        status = (
            "The scheduled dependency checks could not determine whether updates "
            "are available because one or more registry lookups failed. Package "
            "status is unknown; this issue remains open until checks can confirm it."
        )
    else:
        status = "The scheduled dependency checks found version updates to review."
    lines = [
        ISSUE_MARKER,
        "",
        status + " "
        "This issue is refreshed by each run and does not change dependency "
        "constraints or pins automatically.",
        "",
    ]
    if workflow_run_url:
        lines.append(f"Last updated: {updated_at} ([workflow run]({workflow_run_url}))")
    else:
        lines.append(f"Last updated: {updated_at}")
    lines.append("")

    python_rows = [f for f in findings if f.registry == "PyPI"]
    npm_rows = [f for f in findings if f.registry == "npm"]
    uv_rows = [f for f in findings if f.registry == "uv"]
    dex_rows = [f for f in findings if f.registry == "GHCR"]

    if python_rows:
        lines.extend(
            [
                "## Python dependencies (PyPI)",
                "",
                "| Package | Locked | Latest available | Newest eligible major |",
                "| --- | --- | --- | --- |",
            ]
        )
        for finding in python_rows:
            lines.append(
                f"| `{finding.package}` | `{finding.locked_version}` | "
                f"`{finding.latest_version}` | "
                f"{finding.eligible_version or ('Unknown' if finding.eligibility_unknown else 'None confirmed')} |"
            )
        lines.append("")

    if npm_rows:
        lines.extend(
            [
                "## MCP Inspector (npm)",
                "",
                "| Package | Locked | Latest available | Newest eligible major |",
                "| --- | --- | --- | --- |",
            ]
        )
        for finding in npm_rows:
            lines.append(
                f"| `{finding.package}` | `{finding.locked_version}` | "
                f"`{finding.latest_version}` | "
                f"{finding.eligible_version or ('Unknown' if finding.eligibility_unknown else 'None confirmed')} |"
            )
        lines.append("")

    if uv_rows:
        lines.extend(
            [
                "## uv installer (manual pin)",
                "",
                "The eligible column uses the seven-day cooldown. Update `UV_VERSION` "
                "in both workflows and both Dockerfiles together after review.",
                "",
                "| Pinned | Latest available | Newest eligible |",
                "| --- | --- | --- |",
            ]
        )
        for finding in uv_rows:
            eligible = finding.eligible_version or (
                "Unknown" if finding.eligibility_unknown else "None confirmed"
            )
            lines.append(
                f"| `{finding.locked_version}` | `{finding.latest_version}` | {eligible} |"
            )
        lines.append("")

    if dex_rows:
        lines.extend(
            [
                "## Dex image (GHCR)",
                "",
                "This compares Dex GitHub release dates with the pinned image tag. "
                "It does not verify the GHCR image digest publication age; review "
                "the image manually before changing its pin.",
                "",
                "| Pinned tag | Latest Dex release | Newest release >= 7 days old | GHCR digest age |",
                "| --- | --- | --- | --- |",
            ]
        )
        for finding in dex_rows:
            eligible = finding.eligible_version or (
                "Unknown" if finding.eligibility_unknown else "None confirmed"
            )
            lines.append(
                f"| `{finding.locked_version}` | `{finding.latest_version}` | "
                f"{eligible} | Not available |"
            )
        lines.append("")

    lines.extend(
        [
            "Routine Python and Inspector updates are delivered in the "
            "dependency-update PR. GitHub Actions and image updates, including "
            "pending cooldown checks, are tracked in the Renovate Dependency "
            "Update Dashboard. Close this issue after all reported upgrades are "
            "merged or intentionally deferred.",
        ]
    )
    return "\n".join(lines)


def format_resolved_issue_body(workflow_run_url: str | None) -> str:
    updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        ISSUE_MARKER,
        "",
        "No new version updates requiring review are currently available from "
        "the Python, MCP Inspector, uv, or Dex release checks.",
        "",
    ]
    if workflow_run_url:
        lines.append(f"Last checked: {updated_at} ([workflow run]({workflow_run_url}))")
    else:
        lines.append(f"Last checked: {updated_at}")
    return "\n".join(lines)


def github_api_request(
    method: str,
    url: str,
    token: str,
    payload: dict | list | None = None,
) -> dict | list:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
        if not body:
            return {}
        return json.loads(body)


def workflow_run_url() -> str | None:
    server_url = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not server_url or not repository or not run_id:
        return None
    return f"{server_url}/{repository}/actions/runs/{run_id}"


def find_open_indicator_issue(token: str, repository: str) -> int | None:
    owner, repo = repository.split("/", 1)
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/issues"
        f"?state=open&labels={ISSUE_LABEL}&per_page=100"
    )
    payload = github_api_request("GET", url, token)
    if not isinstance(payload, list):
        raise TypeError("Expected a list of issues from GitHub API")

    matches = [
        issue
        for issue in payload
        if (
            issue.get("title") in {ISSUE_TITLE, LEGACY_ISSUE_TITLE}
            or ISSUE_MARKER in (issue.get("body") or "")
        )
        if "pull_request" not in issue
    ]
    if not matches:
        return None
    return int(matches[0]["number"])


def ensure_issue_label(token: str, repository: str) -> None:
    owner, repo = repository.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/labels"
    try:
        github_api_request(
            "POST",
            url,
            token,
            {
                "name": ISSUE_LABEL,
                "color": "fbca04",
                "description": "Tracks dependency versions needing manual review",
            },
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 422:
            raise


def create_indicator_issue(token: str, repository: str, body: str) -> int:
    owner, repo = repository.split("/", 1)
    ensure_issue_label(token, repository)
    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    payload = github_api_request(
        "POST",
        url,
        token,
        {"title": ISSUE_TITLE, "body": body, "labels": [ISSUE_LABEL]},
    )
    if not isinstance(payload, dict):
        raise TypeError("Expected issue payload from GitHub API")
    return int(payload["number"])


def update_indicator_issue(
    token: str, repository: str, issue_number: int, body: str, *, state: str = "open"
) -> None:
    owner, repo = repository.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"
    github_api_request(
        "PATCH", url, token, {"title": ISSUE_TITLE, "body": body, "state": state}
    )


def sync_indicator_issue(
    token: str,
    repository: str,
    findings: list[MajorVersionFinding],
    workflow_url: str | None,
) -> str:
    open_issue = find_open_indicator_issue(token, repository)

    if findings:
        body = format_issue_body(findings, workflow_url)
        if open_issue is None:
            issue_number = create_indicator_issue(token, repository, body)
            return f"Created issue #{issue_number}"
        update_indicator_issue(token, repository, open_issue, body, state="open")
        return f"Updated issue #{open_issue}"

    if open_issue is None:
        return "No newer major versions and no open indicator issue"

    body = format_resolved_issue_body(workflow_url)
    update_indicator_issue(token, repository, open_issue, body, state="closed")
    return f"Closed issue #{open_issue}"


def emit_warnings(findings: list[MajorVersionFinding]) -> None:
    for finding in findings:
        if finding.latest_version == "Unknown":
            print(
                f"::warning title=Dependency lookup status unknown::{finding.package} "
                f"({finding.registry}) latest version could not be determined"
            )
            continue
        print(
            f"::warning title=New major version available::{finding.package} "
            f"{finding.locked_version} -> {finding.latest_version}"
        )


def write_summary_files(summary_dir: Path, findings: list[MajorVersionFinding]) -> None:
    from check_inspector_version import format_summary_markdown as inspector_summary
    from check_major_dependency_updates import format_summary_markdown as python_summary

    summary_dir.mkdir(parents=True, exist_ok=True)
    python_rows = [finding for finding in findings if finding.registry == "PyPI"]
    npm_rows = [finding for finding in findings if finding.registry == "npm"]

    if python_rows:
        (summary_dir / "major-versions.md").write_text(
            python_summary(python_rows),
            encoding="utf-8",
        )
    if npm_rows:
        (summary_dir / "inspector-major.md").write_text(
            inspector_summary(npm_rows[0]),
            encoding="utf-8",
        )


def describe_indicator_issue_action(
    token: str,
    repository: str,
    findings: list[MajorVersionFinding],
) -> str:
    open_issue = find_open_indicator_issue(token, repository)

    if findings:
        if open_issue is None:
            return "Dry run: would create a new indicator issue"
        return f"Dry run: would update open indicator issue #{open_issue}"

    if open_issue is None:
        return "Dry run: no indicator issue changes"

    return f"Dry run: would close open indicator issue #{open_issue}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create or update the major-version indicator GitHub issue."
    )
    parser.add_argument(
        "--summary-dir",
        type=Path,
        help="Optional directory for GitHub Actions job summary markdown files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report findings and preview issue actions without writing to GitHub.",
    )
    args = parser.parse_args(argv)

    findings = collect_findings()

    if findings:
        has_confirmed_updates = any(
            finding.latest_version != "Unknown" for finding in findings
        )
        has_unknown_status = any(
            finding.latest_version == "Unknown" for finding in findings
        )
        if has_confirmed_updates and has_unknown_status:
            print("Major updates were found; some lookup statuses are unknown:")
        elif has_confirmed_updates:
            print("Newer major versions are available:")
        else:
            print("Dependency update status is unknown:")
        for finding in findings:
            if finding.latest_version == "Unknown":
                print(
                    f"- {finding.package} ({finding.registry}): latest version "
                    "could not be determined"
                )
            else:
                print(
                    f"- {finding.package} ({finding.registry}): "
                    f"{finding.locked_version} -> {finding.latest_version}"
                )
        emit_warnings(findings)
    else:
        print("No dependency version updates require review.")

    if args.summary_dir is not None:
        write_summary_files(args.summary_dir, findings)

    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        print(
            "Skipping GitHub issue sync because GITHUB_TOKEN or "
            "GITHUB_REPOSITORY is not set."
        )
        return 0

    if args.dry_run:
        try:
            print(describe_indicator_issue_action(token, repository, findings))
        except urllib.error.URLError as exc:
            print(f"::warning title=GitHub issue preview failed::{exc}")
        return 0

    try:
        result = sync_indicator_issue(token, repository, findings, workflow_run_url())
    except urllib.error.URLError as exc:
        print(f"::warning title=GitHub issue sync failed::{exc}")
        return 0

    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
