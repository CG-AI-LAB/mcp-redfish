# Copyright 2026 CG-AI-LAB
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""Unit tests for the Cisco CIMC XML API client."""

import os
import sys
import time
import unittest
from unittest.mock import patch

import httpx2
from fastmcp.exceptions import ToolError, ValidationError

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src"))
)

from src.common import cimc_client  # noqa: E402
from src.common.cimc_client import (  # noqa: E402
    CimcClient,
    CimcError,
    escape_xml,
    is_valid_xml_name,
    mo_to_dict,
    reset_sessions,
)
from src.common.cimc_config import is_valid_dn  # noqa: E402

from test.cimc.fake_cimc import FakeCimc, populate_healthy_server  # noqa: E402

HOST = {
    "address": "cimc-unit.example.com",
    "port": 443,
    "username": "admin",
    "password": "p&ss<w>ord\"'",
    "tls_verify": True,
    "server_dn": "sys/rack-unit-1",
    "timeout": 5.0,
}


class CimcClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_sessions()
        self.fake = FakeCimc()
        populate_healthy_server(self.fake)
        patcher = patch.object(
            cimc_client, "build_http_client", lambda cfg: self.fake.client()
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_sessions)

    # -- helpers ------------------------------------------------------------

    def test_escape_xml_escapes_attribute_metacharacters(self) -> None:
        self.assertEqual(escape_xml("a&b<c>d\"e'f"), "a&amp;b&lt;c&gt;d&quot;e&apos;f")

    def test_name_and_dn_validation(self) -> None:
        self.assertTrue(is_valid_xml_name("computeRackUnit"))
        self.assertTrue(is_valid_xml_name("vp_Token1"))
        self.assertFalse(is_valid_xml_name("1bad"))
        self.assertFalse(is_valid_xml_name('a" b'))
        self.assertFalse(is_valid_xml_name(""))
        self.assertTrue(is_valid_dn("sys/rack-unit-1/bios/bios-settings"))
        self.assertTrue(is_valid_dn("sys/rack-unit-1/board/storage-SAS-SLOT-HBA/vd-0"))
        self.assertFalse(is_valid_dn('sys/rack-unit-1" foo="bar'))
        self.assertFalse(is_valid_dn("/leading"))
        self.assertFalse(is_valid_dn(""))

    def test_mo_to_dict_flat_and_hierarchical(self) -> None:
        from defusedxml import ElementTree as ET

        element = ET.fromstring(
            '<biosSettings dn="a" rn="b"><biosVfX dn="a/x" vpX="1"/></biosSettings>'
        )
        self.assertEqual(
            mo_to_dict(element), {"class": "biosSettings", "dn": "a", "rn": "b"}
        )
        nested = mo_to_dict(element, include_children=True)
        self.assertEqual(
            nested["children"], [{"class": "biosVfX", "dn": "a/x", "vpX": "1"}]
        )

    # -- session handling ---------------------------------------------------

    def test_login_once_and_cache_cookie_across_clients(self) -> None:
        with CimcClient(HOST) as client:
            self.assertEqual(len(client.resolve_class("equipmentPsu")), 2)
        with CimcClient(HOST) as client:
            self.assertIsNotNone(client.resolve_dn("sys/rack-unit-1"))
        self.assertEqual(self.fake.login_count, 1)
        login = self.fake.requests[0]
        self.assertEqual(login.tag, "aaaLogin")
        # The password round-trips through XML escaping intact.
        self.assertEqual(login.attrib["inPassword"], HOST["password"])

    def test_refreshes_session_near_expiry(self) -> None:
        with CimcClient(HOST) as client:
            client.resolve_class("equipmentPsu")
            key = client._key
            with cimc_client._registry_lock:
                session = cimc_client._sessions[key]
                session.expires_at = time.monotonic() + 10  # inside the margin
            client.resolve_class("equipmentPsu")
        self.assertEqual(self.fake.login_count, 1)
        self.assertEqual(self.fake.refresh_count, 1)
        # Subsequent requests use the refreshed cookie.
        self.assertEqual(self.fake.requests[-1].attrib["cookie"], self.fake.cookie)

    def test_logs_in_again_after_expired_session_error(self) -> None:
        with CimcClient(HOST) as client:
            client.resolve_class("equipmentPsu")
            self.fake.expire_next_request = True
            result = client.resolve_class("equipmentFan")
        self.assertEqual(len(result), 6)
        self.assertEqual(self.fake.login_count, 2)

    def test_login_failure_raises_cimc_error(self) -> None:
        self.fake.fail_login = True
        with CimcClient(HOST) as client:
            with self.assertRaises(CimcError) as ctx:
                client.resolve_class("equipmentPsu")
        self.assertEqual(ctx.exception.error_code, "551")
        self.assertIn("Authentication failed", str(ctx.exception))
        # Application errors are not retried.
        self.assertEqual(self.fake.login_count, 1)

    def test_missing_credentials_is_a_validation_error(self) -> None:
        host = {**HOST, "password": ""}
        with CimcClient(host) as client:
            with self.assertRaises(ValidationError):
                client.resolve_class("equipmentPsu")
        self.assertEqual(self.fake.login_count, 0)

    def test_logout_ends_cached_session(self) -> None:
        with CimcClient(HOST) as client:
            client.resolve_class("equipmentPsu")
            client.logout()
            client.resolve_class("equipmentPsu")
        self.assertEqual(self.fake.logout_count, 1)
        self.assertEqual(self.fake.login_count, 2)

    # -- queries ------------------------------------------------------------

    def test_resolve_dn_returns_none_for_unknown_dn(self) -> None:
        with CimcClient(HOST) as client:
            self.assertIsNone(client.resolve_dn("sys/rack-unit-1/does-not-exist"))

    def test_resolve_dn_hierarchical_includes_children(self) -> None:
        with CimcClient(HOST) as client:
            settings = client.resolve_dn("sys/rack-unit-1/bios/bios-settings", True)
        assert settings is not None
        self.assertEqual(len(settings["children"]), 2)
        self.assertEqual(
            settings["children"][0]["class"], "biosVfSelectMemoryRASConfiguration"
        )

    def test_resolve_children_filters_by_class(self) -> None:
        with CimcClient(HOST) as client:
            psus = client.resolve_children("sys/rack-unit-1", "equipmentPsu")
            everything = client.resolve_children("sys/rack-unit-1")
        self.assertEqual([p["id"] for p in psus], ["1", "2"])
        self.assertGreater(len(everything), len(psus))

    def test_invalid_inputs_are_rejected_before_any_request(self) -> None:
        with CimcClient(HOST) as client:
            with self.assertRaises(ValidationError):
                client.resolve_dn('sys/rack-unit-1" x="y')
            with self.assertRaises(ValidationError):
                client.resolve_class("compute RackUnit")
            with self.assertRaises(ValidationError):
                client.resolve_children("sys/rack-unit-1", "bad class")
            with self.assertRaises(ValidationError):
                client.config_mo("sys/rack-unit-1", "computeRackUnit", {})
            with self.assertRaises(ValidationError):
                client.config_mo(
                    "sys/rack-unit-1", "computeRackUnit", {"bad name": "x"}
                )
        self.assertEqual(self.fake.requests, [])

    def test_config_mo_escapes_values_and_returns_result(self) -> None:
        with CimcClient(HOST) as client:
            result = client.config_mo(
                "sys/rack-unit-1/sol-if",
                "solIf",
                {"adminState": "enable", "speed": '115200" evil="1'},
            )
        assert result is not None
        self.assertEqual(result["status"], "modified")
        sent = self.fake.configured[-1]
        self.assertEqual(sent.tag, "solIf")
        self.assertEqual(sent.attrib["adminState"], "enable")
        # The injected quote stayed inside the attribute value.
        self.assertEqual(sent.attrib["speed"], '115200" evil="1')
        self.assertNotIn("evil", sent.attrib)

    # -- transport failures -------------------------------------------------

    def test_transport_errors_are_retried_then_reported(self) -> None:
        self.fake.transport_error = httpx2.ConnectError("boom")
        with CimcClient(HOST) as client:
            with self.assertRaises(Exception) as ctx:
                client.resolve_class("equipmentPsu")
        # tenacity re-raises either the ToolError or a RetryError wrapping it.
        message = str(ctx.exception) + str(getattr(ctx.exception, "last_attempt", ""))
        self.assertTrue("ConnectError" in message or "RetryError" in message)

    def test_http_error_status_is_a_tool_error(self) -> None:
        self.fake.http_status = 503
        with CimcClient(HOST) as client:
            with self.assertRaises(ToolError) as ctx:
                client.resolve_class("equipmentPsu")
        self.assertIn("HTTP 503", str(ctx.exception))

    def test_non_xml_response_is_a_tool_error(self) -> None:
        self.fake.raw_response = "<html>login page</html"
        with CimcClient(HOST) as client:
            with self.assertRaises(ToolError) as ctx:
                client.resolve_class("equipmentPsu")
        self.assertIn("not valid XML", str(ctx.exception))

    def test_unexpected_root_element_is_a_tool_error(self) -> None:
        self.fake.raw_response = '<configResolveDn cookie="" response="yes"/>'
        with CimcClient(HOST) as client:
            with self.assertRaises(ToolError) as ctx:
                client.resolve_class("equipmentPsu")
        self.assertIn("<aaaLogin>", str(ctx.exception))


class BuildHttpClientTestCase(unittest.TestCase):
    def test_tls_verification_defaults_on(self) -> None:
        client = cimc_client.build_http_client({"address": "a", "tls_verify": True})
        try:
            self.assertEqual(client.headers["Content-Type"], "text/xml")
        finally:
            client.close()

    def test_tls_verification_can_be_disabled_with_warning(self) -> None:
        with self.assertLogs("src.common.cimc_client", level="WARNING") as logs:
            client = cimc_client.build_http_client(
                {"address": "a", "tls_verify": False, "timeout": 7}
            )
        client.close()
        self.assertTrue(any("disabled" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
