#!/usr/bin/env python3
# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""Upgrade uv packages while honoring explicit exact-version holds."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

_REVISION_PATTERN = re.compile(r"(?m)^revision = (\d+)\s*$")
_UNSATISFIABLE_RESOLUTION = "no solution found"


@dataclass(frozen=True)
class Artifact:
    url: str
    uploaded: datetime | None


@dataclass(frozen=True)
class LockEntry:
    version: Version
    markers: frozenset[str]
    artifacts: frozenset[Artifact]


Snapshot = dict[str, tuple[LockEntry, ...]]
Preserved = dict[str, Requirement]


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _format_cutoff(value: datetime) -> str:
    utc_value = value.astimezone(UTC)
    if utc_value.microsecond:
        return utc_value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return utc_value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _revision(lock_text: str) -> int:
    match = _REVISION_PATTERN.search(lock_text)
    if match is None:
        raise ValueError("uv.lock is missing its revision")
    return int(match.group(1))


def load_snapshot(lockfile: Path) -> Snapshot:
    """Read registry package versions, forks, and artifact timestamps."""
    document = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    snapshot: dict[str, list[LockEntry]] = {}
    for package in document.get("package", []):
        source = package.get("source", {})
        if not isinstance(source, dict) or "registry" not in source:
            continue
        name = canonicalize_name(str(package["name"]))
        raw_markers = package.get("resolution-markers", [])
        if not isinstance(raw_markers, list) or not all(
            isinstance(marker, str) for marker in raw_markers
        ):
            raise ValueError(f"{name} has invalid resolution markers")
        artifacts: set[Artifact] = set()
        for distribution in [package.get("sdist"), *package.get("wheels", [])]:
            if not isinstance(distribution, dict):
                continue
            artifacts.add(
                Artifact(
                    url=str(distribution.get("url", "")),
                    uploaded=_timestamp(distribution.get("upload-time")),
                )
            )
        snapshot.setdefault(name, []).append(
            LockEntry(
                version=Version(str(package["version"])),
                markers=frozenset(raw_markers),
                artifacts=frozenset(artifacts),
            )
        )
    return {
        name: tuple(
            sorted(entries, key=lambda entry: (entry.version, sorted(entry.markers)))
        )
        for name, entries in snapshot.items()
    }


def load_preserved(pyproject: Path) -> Preserved:
    """Load exact package holds from tool.dependency-updates.preserve."""
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    raw = document.get("tool", {}).get("dependency-updates", {}).get("preserve", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("tool.dependency-updates.preserve must be a list of strings")
    preserved: Preserved = {}
    for item in raw:
        try:
            requirement = Requirement(item)
        except InvalidRequirement as error:
            raise ValueError(f"invalid preserved dependency: {item}") from error
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or requirement.marker is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise ValueError(
                f"preserved dependency must use one exact == version: {item}"
            )
        name = canonicalize_name(requirement.name)
        if name in preserved:
            raise ValueError(f"duplicate preserved dependency: {name}")
        preserved[name] = requirement
    return preserved


def validate_preserved(snapshot: Snapshot, preserved: Preserved) -> None:
    """Ensure every configured hold exists at its exact locked version."""
    for name, requirement in preserved.items():
        entries = snapshot.get(name)
        if not entries:
            raise ValueError(f"preserved dependency is not in uv.lock: {name}")
        if any(entry.version not in requirement.specifier for entry in entries):
            versions = ", ".join(str(entry.version) for entry in entries)
            raise ValueError(
                f"preserved dependency {requirement} does not match uv.lock: {versions}"
            )


def fresh_packages(before: Snapshot, after: Snapshot, cutoff: datetime) -> set[str]:
    """Return packages that gained a fresh or undated artifact."""
    fresh: set[str] = set()
    for name, entries in sorted(after.items()):
        old_entries = before.get(name, ())
        for entry in entries:
            previous = next(
                (
                    old
                    for old in old_entries
                    if old.version == entry.version and old.markers == entry.markers
                ),
                None,
            )
            added = (
                entry.artifacts
                if previous is None
                else entry.artifacts - previous.artifacts
            )
            undated_new_release = previous is None and not entry.artifacts
            fresh_or_undated_artifact = any(
                artifact.uploaded is None or artifact.uploaded > cutoff
                for artifact in added
            )
            if undated_new_release or fresh_or_undated_artifact:
                fresh.add(name)
    return fresh


def remove_persisted_cutoff(lockfile: Path, revision: int) -> None:
    """Remove cutoff metadata uv persists, without changing selected versions."""
    output: list[str] = []
    skipping = False
    for line in lockfile.read_text(encoding="utf-8").splitlines(keepends=True):
        if _REVISION_PATTERN.match(line):
            output.append(f"revision = {revision}\n")
            continue
        if line.strip() == "[options]" or line.startswith("[options."):
            skipping = True
            continue
        if skipping:
            if line.startswith("[["):
                skipping = False
                if output and output[-1].strip():
                    output.append("\n")
                output.append(line)
            continue
        output.append(line)
    lockfile.write_text("".join(output), encoding="utf-8")


def _is_unsatisfiable(stderr: str) -> bool:
    return _UNSATISFIABLE_RESOLUTION in stderr.casefold()


def _restore(lockfile: Path, contents: bytes) -> None:
    lockfile.write_bytes(contents)


def _resolve(
    lockfile: Path,
    upgrade_packages: set[str],
    limits: dict[str, datetime],
    revision: int,
) -> subprocess.CompletedProcess[str]:
    command = ["uv", "lock"]
    for package in sorted(upgrade_packages):
        command.extend(["--upgrade-package", package])
    for limited_package, limit in sorted(limits.items()):
        command.extend(
            ["--exclude-newer-package", f"{limited_package}={_format_cutoff(limit)}"]
        )
    environment = os.environ.copy()
    environment.pop("UV_EXCLUDE_NEWER", None)
    completed = subprocess.run(
        command,
        cwd=lockfile.parent,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and lockfile.is_file():
        remove_persisted_cutoff(lockfile, revision)
    return completed


def update_dependencies(
    lockfile: Path, pyproject: Path, cutoff: datetime | None
) -> None:
    """Update all unheld packages in one resolution."""
    before_bytes = lockfile.read_bytes()
    revision = _revision(before_bytes.decode("utf-8"))
    before = load_snapshot(lockfile)
    preserved = load_preserved(pyproject)
    validate_preserved(before, preserved)
    upgrade_packages = set(before) - preserved.keys()
    limits = (
        dict.fromkeys(sorted(upgrade_packages), cutoff) if cutoff is not None else {}
    )

    while True:
        completed = _resolve(lockfile, upgrade_packages, limits, revision)
        if completed.returncode != 0:
            _restore(lockfile, before_bytes)
            if _is_unsatisfiable(completed.stderr):
                print(
                    "Kept existing lock: no eligible resolution satisfies the policy."
                )
                return
            completed.check_returncode()

        try:
            after = load_snapshot(lockfile)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
            _restore(lockfile, before_bytes)
            raise

        changed_holds = {
            name for name in preserved if after.get(name) != before.get(name)
        }
        if changed_holds:
            _restore(lockfile, before_bytes)
            names = ", ".join(sorted(changed_holds))
            print(
                f"Kept existing lock: resolution would change preserved package(s): {names}."
            )
            return

        if cutoff is None:
            break
        fresh = fresh_packages(before, after, cutoff)
        if not fresh:
            break
        new_limits = fresh - limits.keys()
        if not new_limits:
            _restore(lockfile, before_bytes)
            names = ", ".join(sorted(fresh))
            print(
                "Kept existing lock: resolver retained fresh or undated "
                f"artifact(s) for limited package(s): {names}."
            )
            return
        limits.update(dict.fromkeys(sorted(new_limits), cutoff))
        _restore(lockfile, before_bytes)

    if lockfile.read_bytes() == before_bytes:
        print("Python dependencies are already current under the update policy.")
    else:
        print(
            "Python dependency update complete; preserved packages: "
            + (", ".join(sorted(preserved)) if preserved else "none")
            + "."
        )


def _cutoff(value: str) -> datetime:
    try:
        cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--exclude-newer must be an ISO 8601 timestamp"
        ) from error
    if cutoff.utcoffset() is None:
        raise argparse.ArgumentTypeError("--exclude-newer must include a timezone")
    return cutoff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exclude-newer", type=_cutoff)
    parser.add_argument("--lockfile", type=Path, default=Path("uv.lock"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)

    lockfile = args.lockfile.resolve()
    update_dependencies(lockfile, args.pyproject.resolve(), args.exclude_newer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
