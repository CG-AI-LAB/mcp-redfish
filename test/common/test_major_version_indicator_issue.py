# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

import io
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from check_major_dependency_updates import (
    collect_outdated_major_versions,
    fetch_version_availability,
)
from check_major_dependency_updates import (
    format_summary_markdown as format_python_summary,
)
from major_version_findings import MajorVersionFinding
from manage_major_version_indicator_issue import (
    ISSUE_MARKER,
    ISSUE_TITLE,
    LEGACY_ISSUE_TITLE,
    collect_dex_release_update,
    collect_findings,
    find_open_indicator_issue,
    format_issue_body,
    format_resolved_issue_body,
    main,
    sync_indicator_issue,
)


class TestMajorVersionIndicatorIssue(unittest.TestCase):
    @patch(
        "manage_major_version_indicator_issue.collect_findings",
        return_value=[
            MajorVersionFinding(
                "example", "1.2.3", "Unknown", "PyPI", eligibility_unknown=True
            )
        ],
    )
    def test_main_labels_unknown_only_findings_accurately(
        self, _collect_findings
    ) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
            self.assertEqual(main([]), 0)

        self.assertIn("Dependency update status is unknown:", output.getvalue())
        self.assertNotIn("Newer major versions are available:", output.getvalue())
        self.assertIn("latest version could not be determined", output.getvalue())

    @patch(
        "check_major_dependency_updates.fetch_version_availability",
        side_effect=urllib.error.URLError("offline"),
    )
    @patch(
        "check_major_dependency_updates.load_direct_dependencies",
        return_value=["example>=1"],
    )
    @patch(
        "check_major_dependency_updates.load_locked_versions",
        return_value={"example": "1.2.3"},
    )
    def test_python_lookup_failure_remains_visible(
        self, _locked, _dependencies, _fetch
    ) -> None:
        findings = collect_outdated_major_versions()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].latest_version, "Unknown")
        self.assertTrue(findings[0].eligibility_unknown)
        summary = format_python_summary(findings)
        self.assertIn("status is unknown", summary)
        self.assertIn("| Unknown |", summary)

    @patch("manage_major_version_indicator_issue.update_indicator_issue")
    @patch(
        "manage_major_version_indicator_issue.find_open_indicator_issue",
        return_value=42,
    )
    def test_unknown_registry_finding_keeps_issue_open(
        self, _find_issue, update_issue
    ) -> None:
        finding = MajorVersionFinding(
            "example", "1.2.3", "Unknown", "PyPI", eligibility_unknown=True
        )
        result = sync_indicator_issue("token", "nokia/mcp-redfish", [finding], None)
        self.assertEqual(result, "Updated issue #42")
        self.assertEqual(update_issue.call_args.kwargs["state"], "open")

    def test_issue_body_describes_unknown_lookup_without_claiming_update(self) -> None:
        finding = MajorVersionFinding(
            "example", "1.2.3", "Unknown", "PyPI", eligibility_unknown=True
        )
        body = format_issue_body([finding], None)
        self.assertIn("could not determine whether updates are available", body)
        self.assertNotIn("found version updates to review", body)

    @patch(
        "manage_major_version_indicator_issue.fetch_uv_releases",
        side_effect=urllib.error.URLError("offline"),
    )
    @patch(
        "manage_major_version_indicator_issue.collect_inspector_major_update",
        return_value=None,
    )
    @patch(
        "manage_major_version_indicator_issue.collect_outdated_major_versions",
        return_value=[],
    )
    @patch(
        "manage_major_version_indicator_issue.collect_dex_release_update",
        return_value=None,
    )
    def test_uv_lookup_failure_remains_visible(
        self, _dex, _python, _inspector, _uv
    ) -> None:
        with patch.dict("os.environ", {"UV_VERSION": "0.12.7"}, clear=True):
            findings = collect_findings()
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].eligibility_unknown)
        self.assertEqual(findings[0].latest_version, "Unknown")

    @patch("manage_major_version_indicator_issue.urllib.request.urlopen")
    def test_dex_notice_reports_latest_and_eligible_source_release(
        self, urlopen
    ) -> None:
        payload = [
            {
                "tag_name": "v2.44.0",
                "published_at": "2026-10-12T00:00:00Z",
            },
            {
                "tag_name": "v2.43.2",
                "published_at": "2026-10-01T00:00:00Z",
            },
            {"tag_name": "v2.45.0-rc.1", "prerelease": True},
        ]
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode())
        finding = collect_dex_release_update(datetime(2026, 10, 8, tzinfo=UTC))
        self.assertIsNotNone(finding)
        self.assertEqual(finding.locked_version, "2.43.1")
        self.assertEqual(finding.latest_version, "2.44.0")
        self.assertEqual(finding.eligible_version, "2.43.2")

    @patch(
        "manage_major_version_indicator_issue.urllib.request.urlopen",
        side_effect=urllib.error.URLError("offline"),
    )
    def test_dex_lookup_failure_remains_visible(self, _urlopen) -> None:
        finding = collect_dex_release_update(datetime(2026, 10, 8, tzinfo=UTC))
        self.assertIsNotNone(finding)
        self.assertTrue(finding.eligibility_unknown)
        self.assertEqual(finding.latest_version, "Unknown")

    def test_dex_issue_explains_image_age_is_unavailable(self) -> None:
        body = format_issue_body(
            [
                MajorVersionFinding(
                    "Dex image (GHCR)", "2.43.1", "2.44.0", "GHCR", "2.43.2"
                )
            ],
            None,
        )
        self.assertIn("## Dex image (GHCR)", body)
        self.assertIn("does not verify the GHCR image digest publication age", body)
        self.assertIn("Not available", body)

    @patch("check_major_dependency_updates.urllib.request.urlopen")
    def test_newest_eligible_major_can_be_older_than_latest(self, urlopen) -> None:
        payload = {
            "info": {"version": "4.0.0"},
            "releases": {
                "4.0.0": [{"upload_time_iso_8601": "2026-10-07T00:00:00Z"}],
                "3.0.0": [{"upload_time_iso_8601": "invalid"}],
                "2.5.0": [{"upload_time_iso_8601": "2026-10-01T00:00:00Z"}],
                "2.6.0": [
                    {"upload_time_iso_8601": "2026-09-01T00:00:00Z", "yanked": True}
                ],
                "5.0.0rc1": [{"upload_time_iso_8601": "2026-09-01T00:00:00Z"}],
            },
        }
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode())
        self.assertEqual(
            fetch_version_availability("example", datetime(2026, 10, 1, tzinfo=UTC)),
            ("4.0.0", "2.5.0"),
        )

    def test_issue_shows_available_and_eligible_versions(self) -> None:
        body = format_issue_body(
            [MajorVersionFinding("example", "1.0.0", "4.0.0", "PyPI", "2.5.0")],
            None,
        )
        self.assertIn("Latest available", body)
        self.assertIn("Newest eligible major", body)
        self.assertIn("2.5.0", body)

    def test_format_issue_body_includes_marker_and_findings(self) -> None:
        body = format_issue_body(
            [
                MajorVersionFinding("fastmcp", "3.4.7", "4.0.3", "PyPI"),
                MajorVersionFinding(
                    "@modelcontextprotocol/inspector",
                    "2.5.0",
                    "3.0.0",
                    "npm",
                ),
            ],
            "https://github.com/example/repo/actions/runs/1",
        )
        self.assertIn(ISSUE_MARKER, body)
        self.assertIn("## Python dependencies (PyPI)", body)
        self.assertIn("## MCP Inspector (npm)", body)
        self.assertIn("`fastmcp`", body)
        self.assertIn("workflow run", body)

    def test_format_resolved_issue_body(self) -> None:
        body = format_resolved_issue_body(None)
        self.assertIn(ISSUE_MARKER, body)
        self.assertIn("No new version updates requiring review", body)

    @patch("manage_major_version_indicator_issue.github_api_request")
    def test_find_open_indicator_issue_returns_matching_issue(
        self, github_api_request
    ) -> None:
        github_api_request.return_value = [
            {
                "number": 42,
                "title": ISSUE_TITLE,
            },
            {
                "number": 43,
                "title": "Unrelated",
            },
        ]
        self.assertEqual(
            find_open_indicator_issue("token", "nokia/mcp-redfish"),
            42,
        )

    @patch("manage_major_version_indicator_issue.github_api_request")
    def test_find_open_indicator_issue_migrates_legacy_title(
        self, github_api_request
    ) -> None:
        github_api_request.return_value = [
            {"number": 42, "title": LEGACY_ISSUE_TITLE},
            {"number": 43, "title": "Unrelated", "body": ISSUE_MARKER},
        ]
        self.assertEqual(
            find_open_indicator_issue("token", "nokia/mcp-redfish"),
            42,
        )

    @patch("manage_major_version_indicator_issue.update_indicator_issue")
    @patch("manage_major_version_indicator_issue.find_open_indicator_issue")
    def test_sync_indicator_issue_updates_existing_open_issue(
        self, find_open_issue, update_issue
    ) -> None:
        find_open_issue.return_value = 42
        findings = [MajorVersionFinding("fastmcp", "3.4.7", "4.0.3", "PyPI")]

        result = sync_indicator_issue("token", "nokia/mcp-redfish", findings, None)

        self.assertEqual(result, "Updated issue #42")
        update_issue.assert_called_once()
        self.assertEqual(update_issue.call_args.args[2], 42)
        self.assertEqual(update_issue.call_args.kwargs["state"], "open")

    @patch("manage_major_version_indicator_issue.create_indicator_issue")
    @patch("manage_major_version_indicator_issue.find_open_indicator_issue")
    def test_sync_indicator_issue_creates_issue_when_missing(
        self, find_open_issue, create_issue
    ) -> None:
        find_open_issue.return_value = None
        create_issue.return_value = 99
        findings = [MajorVersionFinding("fastmcp", "3.4.7", "4.0.3", "PyPI")]

        result = sync_indicator_issue("token", "nokia/mcp-redfish", findings, None)

        self.assertEqual(result, "Created issue #99")
        create_issue.assert_called_once()

    @patch("manage_major_version_indicator_issue.update_indicator_issue")
    @patch("manage_major_version_indicator_issue.find_open_indicator_issue")
    def test_sync_indicator_issue_closes_open_issue_when_resolved(
        self, find_open_issue, update_issue
    ) -> None:
        find_open_issue.return_value = 42

        result = sync_indicator_issue("token", "nokia/mcp-redfish", [], None)

        self.assertEqual(result, "Closed issue #42")
        update_issue.assert_called_once()
        self.assertEqual(update_issue.call_args.kwargs["state"], "closed")

    @patch("manage_major_version_indicator_issue.sync_indicator_issue")
    @patch("manage_major_version_indicator_issue.collect_findings")
    def test_main_skips_github_sync_without_token(
        self, collect_findings, sync_indicator_issue
    ) -> None:
        collect_findings.return_value = [
            MajorVersionFinding("fastmcp", "3.4.7", "4.0.3", "PyPI")
        ]

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main([]), 0)

        sync_indicator_issue.assert_not_called()

    @patch("manage_major_version_indicator_issue.sync_indicator_issue")
    @patch("manage_major_version_indicator_issue.describe_indicator_issue_action")
    @patch("manage_major_version_indicator_issue.collect_findings")
    def test_main_dry_run_previews_without_sync(
        self, collect_findings, describe_action, sync_indicator_issue
    ) -> None:
        collect_findings.return_value = [
            MajorVersionFinding("fastmcp", "3.4.7", "4.0.3", "PyPI")
        ]
        describe_action.return_value = "Dry run: would create a new indicator issue"

        with patch.dict(
            "os.environ",
            {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "nokia/mcp-redfish"},
            clear=True,
        ):
            self.assertEqual(main(["--dry-run"]), 0)

        describe_action.assert_called_once()
        sync_indicator_issue.assert_not_called()

    @patch(
        "manage_major_version_indicator_issue.collect_findings",
        return_value=[],
    )
    def test_collect_findings_is_used_by_main(self, _collect_findings) -> None:
        with patch.dict(
            "os.environ",
            {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "nokia/mcp-redfish"},
            clear=True,
        ):
            with patch(
                "manage_major_version_indicator_issue.sync_indicator_issue",
                return_value="No newer major versions and no open indicator issue",
            ) as sync_indicator_issue:
                self.assertEqual(main([]), 0)
        sync_indicator_issue.assert_called_once_with(
            "token", "nokia/mcp-redfish", [], None
        )


if __name__ == "__main__":
    unittest.main()
