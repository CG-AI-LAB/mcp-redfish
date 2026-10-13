"""Report a newer stable uv release without changing the installer pin."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e.inspector_version import parse_version  # noqa: E402

UV_RELEASE_URL = "https://api.github.com/repos/astral-sh/uv/releases"


def parse_uv_release(payload: object) -> tuple[str, datetime | None] | None:
    if not isinstance(payload, dict):
        raise ValueError("Expected uv release metadata")
    if payload.get("draft") or payload.get("prerelease"):
        return None
    tag = payload.get("tag_name")
    if not isinstance(tag, str):
        raise ValueError("Expected a uv release tag")
    version = tag.removeprefix("v")
    parse_version(version)
    published = None
    timestamp = payload.get("published_at")
    if isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            pass
        else:
            if parsed.utcoffset() is not None:
                published = parsed
    return version, published


def fetch_uv_releases() -> list[tuple[str, datetime | None]]:
    releases = []
    page = 1
    while True:
        request = urllib.request.Request(
            f"{UV_RELEASE_URL}?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        if not isinstance(payload, list):
            raise ValueError("Expected uv release history")
        for item in payload:
            release = parse_uv_release(item)
            if release is not None:
                releases.append(release)
        if len(payload) < 100:
            break
        page += 1
    if not releases:
        raise ValueError("No stable uv releases found")
    return sorted(releases, key=lambda release: parse_version(release[0]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pinned-version", required=True)
    parser.add_argument("--cooldown-days", type=int, default=7)
    parser.add_argument("--summary-file", type=Path)
    args = parser.parse_args(argv)
    try:
        pinned = parse_version(args.pinned_version)
    except ValueError:
        parser.error("--pinned-version must use X.Y.Z format")
    if args.cooldown_days < 0:
        parser.error("--cooldown-days must be non-negative")

    try:
        releases = fetch_uv_releases()
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        summary = (
            "## uv version indicator\n\nLookup failed; uv release status is unknown.\n"
        )
        print("::warning title=uv lookup failed::uv release status is unknown")
    else:
        latest, published = releases[-1]
        newer = parse_version(latest) > pinned
        cutoff = datetime.now(UTC) - timedelta(days=args.cooldown_days)
        eligible = [
            release
            for release in releases
            if release[1] is not None
            and release[1] <= cutoff
            and parse_version(release[0]) > pinned
        ]
        if not newer:
            status = "No newer stable release reported"
        elif published is None:
            status = "Publication time unknown; manual review required"
        elif published > cutoff:
            status = (
                f"Within the {args.cooldown_days}-day cooldown; wait before upgrading"
            )
        else:
            status = "Cooldown complete; manual pin update available"
        recommendation = "None; keep the current pin"
        if eligible:
            recommended, recommended_at = eligible[-1]
            assert recommended_at is not None
            recommendation = f"`{recommended}` (published {recommended_at.isoformat()})"
            print(
                f"::warning title=uv update available::{args.pinned_version} -> {recommended}; "
                "cooldown complete, review and update the pin manually"
            )
        publication = published.isoformat() if published else "Unknown"
        summary = (
            "## uv version indicator\n\n"
            "| Pinned | Latest stable | Published | Status |\n"
            "| --- | --- | --- | --- |\n"
            f"| `{args.pinned_version}` | `{latest}` | {publication} | {status} |\n\n"
            f"**Newest eligible upgrade:** {recommendation}.\n\n"
            "This check is informational only; it never changes the pin. "
            "[Release details](https://github.com/astral-sh/uv/releases).\n"
        )
    print(summary)
    if args.summary_file is not None:
        args.summary_file.write_text(summary, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
