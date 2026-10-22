# Copyright 2026 CG-AI-LAB
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for the cimc_* MCP tools, driven through the FastMCP client."""

import os
import sys
import unittest
from unittest.mock import patch

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src"))
)

import src.common.server  # noqa: E402
import src.tools  # noqa: E402  # registers tools
from src.common import cimc_client  # noqa: E402
from src.common.cimc_client import reset_sessions  # noqa: E402
from src.tools import cimc as cimc_tools  # noqa: E402

from test.cimc.fake_cimc import SERVER_DN, FakeCimc, populate_healthy_server  # noqa: E402
from test.utils import extract_call_tool_result  # noqa: E402

ADDRESS = "cimc-test.example.com"  # configured in test/conftest.py


class CimcToolsTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def call(self, name: str, **arguments):
        async with Client(src.common.server.mcp) as client:
            result = await client.call_tool(name, arguments)
        data = extract_call_tool_result(result)
        # FastMCP wraps non-object results as {"result": ...} in structured content.
        if isinstance(data, dict) and set(data) == {"result"}:
            return data["result"]
        return data

    # -- registration -------------------------------------------------------

    async def test_all_tools_registered_with_writes_enabled(self) -> None:
        tools = {t.name for t in await src.common.server.mcp.list_tools()}
        for fn in cimc_tools.READ_TOOLS + cimc_tools.WRITE_TOOLS:
            self.assertIn(fn.__name__, tools)
        # The Redfish tools are still there.
        self.assertIn("get_resource_data", tools)

    async def test_write_tools_absent_when_writes_disabled(self) -> None:
        server = FastMCP("cimc-readonly-test")
        cimc_tools.register(server, allow_writes=False)
        tools = {t.name for t in await server.list_tools()}
        for fn in cimc_tools.READ_TOOLS:
            self.assertIn(fn.__name__, tools)
        for fn in cimc_tools.WRITE_TOOLS:
            self.assertNotIn(fn.__name__, tools)

    # -- read tools ---------------------------------------------------------

    async def test_list_servers(self) -> None:
        self.assertEqual(await self.call("cimc_list_servers"), [ADDRESS])

    async def test_unknown_address_is_rejected_without_a_request(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            await self.call("cimc_get_server_summary", address="not-configured")
        self.assertIn("not configured", str(ctx.exception))
        self.assertEqual(self.fake.login_count, 0)

    async def test_server_summary(self) -> None:
        data = await self.call("cimc_get_server_summary", address=ADDRESS)
        self.assertEqual(data["model"], "UCSC-C220-M4S")
        self.assertEqual(data["powerState"], "on")
        self.assertEqual(data["cpus"], "2")
        self.assertEqual(data["totalMemoryMB"], "131072")

    async def test_server_health_healthy(self) -> None:
        # Remove the critical/major faults so the server reads healthy.
        self.fake.classes["faultInst"] = [
            e for e in self.fake.classes["faultInst"] if e[0]["severity"] == "info"
        ]
        data = await self.call("cimc_get_server_health", address=ADDRESS)
        self.assertEqual(data["overall"], "HEALTHY")
        self.assertEqual(data["components"]["dimms"], {"total": 4, "issues": 0})
        self.assertEqual(data["components"]["psus"]["total"], 2)
        self.assertEqual(data["components"]["physicalDisks"]["total"], 2)

    async def test_server_health_degraded_by_faults_and_disk(self) -> None:
        self.fake.classes["storageLocalDisk"][0][0]["pdStatus"] = "Failed"
        data = await self.call("cimc_get_server_health", address=ADDRESS)
        self.assertEqual(data["overall"], "DEGRADED")
        self.assertEqual(data["faults"]["critical"], 1)
        self.assertEqual(data["faults"]["major"], 1)
        self.assertEqual(data["components"]["physicalDisks"]["issues"], 1)

    async def test_faults_sorted_and_filtered(self) -> None:
        data = await self.call("cimc_get_faults", address=ADDRESS)
        self.assertEqual(
            [f["severity"] for f in data["faults"]], ["critical", "major", "info"]
        )
        data = await self.call("cimc_get_faults", address=ADDRESS, severity="major")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["faults"][0]["code"], "F0185")

    async def test_event_log_limit(self) -> None:
        data = await self.call("cimc_get_event_log", address=ADDRESS, limit=1)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["showing"], 1)
        self.assertEqual(data["entries"][0]["description"], "System Boot Initiated")
        with self.assertRaises(ToolError):
            await self.call("cimc_get_event_log", address=ADDRESS, limit=0)

    async def test_sensors_inventory_storage_network_firmware(self) -> None:
        sensors = await self.call("cimc_get_sensors", address=ADDRESS)
        self.assertEqual(len(sensors["fans"]), 6)
        self.assertEqual(sensors["cpuTemperatures"][0]["temperature"], "45.0")

        inventory = await self.call("cimc_get_inventory", address=ADDRESS)
        self.assertEqual(len(inventory["cpus"]), 2)
        self.assertEqual(len(inventory["memory"]), 4)  # the missing DIMM is dropped
        self.assertEqual(inventory["storageControllers"][0]["model"], "UCSC-MRAID12G")

        storage = await self.call("cimc_get_storage_summary", address=ADDRESS)
        self.assertEqual(storage["virtualDrives"][0]["raidLevel"], "RAID 1")
        self.assertEqual(storage["physicalDisks"][1]["status"], "Online")
        self.assertEqual(storage["raidBatteries"][0]["health"], "Good")

        network = await self.call("cimc_get_network_adapters", address=ADDRESS)
        self.assertEqual(network["externalPorts"][0]["mac"], "00:11:22:33:44:55")
        self.assertEqual(network["hostInterfaces"][0]["name"], "eth0")

        firmware = await self.call("cimc_get_firmware_versions", address=ADDRESS)
        self.assertEqual(firmware["total"], 2)
        self.assertIn("2.0(10l)", [f["version"] for f in firmware["firmware"]])

    async def test_power_and_thermal_stats(self) -> None:
        power = await self.call("cimc_get_power_stats", address=ADDRESS)
        self.assertEqual(power["motherboardPower"][0]["consumedPower"], "210")
        self.assertEqual(power["powerBudget"][0]["powerAvailable"], "770")
        thermal = await self.call("cimc_get_thermal_stats", address=ADDRESS)
        self.assertEqual(thermal["ambientTemp"], "23.0")
        self.assertEqual(len(thermal["fans"]), 6)

    async def test_bios_settings_flattened(self) -> None:
        data = await self.call("cimc_get_bios_settings", address=ADDRESS)
        self.assertEqual(data["total"], 2)
        token = data["tokens"][0]
        self.assertEqual(token["className"], "biosVfSelectMemoryRASConfiguration")
        self.assertEqual(
            token["attributes"]["vpSelectMemoryRASConfiguration"], "Maximum Performance"
        )
        self.assertNotIn("class", token["attributes"])

    async def test_generic_resolve_class_and_dn(self) -> None:
        data = await self.call(
            "cimc_resolve_class", address=ADDRESS, class_id="equipmentPsu"
        )
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["objects"][0]["class"], "equipmentPsu")

        data = await self.call("cimc_resolve_dn", address=ADDRESS, dn=SERVER_DN)
        self.assertTrue(data["found"])
        self.assertEqual(data["object"]["serial"], "FCH1234ABCD")

        data = await self.call("cimc_resolve_dn", address=ADDRESS, dn="sys/nothing")
        self.assertFalse(data["found"])

        with self.assertRaises(ToolError):
            await self.call("cimc_resolve_class", address=ADDRESS, class_id="bad class")

    # -- write tools --------------------------------------------------------

    async def test_power_control_requires_confirmation_for_destructive_actions(
        self,
    ) -> None:
        data = await self.call("cimc_power_control", address=ADDRESS, action="off")
        self.assertFalse(data["success"])
        self.assertIn("confirm=true", data["message"])
        self.assertEqual(self.fake.configured, [])

        data = await self.call(
            "cimc_power_control", address=ADDRESS, action="off", confirm=True
        )
        self.assertTrue(data["success"])
        sent = self.fake.configured[-1]
        self.assertEqual(sent.tag, "computeRackUnit")
        self.assertEqual(sent.attrib["adminPower"], "down")

    async def test_power_on_needs_no_confirmation(self) -> None:
        data = await self.call("cimc_power_control", address=ADDRESS, action="on")
        self.assertTrue(data["success"])
        self.assertEqual(self.fake.configured[-1].attrib["adminPower"], "up")

    async def test_power_control_rejects_unknown_action(self) -> None:
        with self.assertRaises(ToolError):
            await self.call("cimc_power_control", address=ADDRESS, action="explode")

    async def test_set_bios_setting(self) -> None:
        token_dn = f"{SERVER_DN}/bios/bios-settings/SelectMemory-RAS-configuration"
        args = {
            "address": ADDRESS,
            "token_dn": token_dn,
            "class_name": "biosVfSelectMemoryRASConfiguration",
            "attribute_name": "vpSelectMemoryRASConfiguration",
            "value": "Mirroring",
        }
        data = await self.call("cimc_set_bios_setting", **args)
        self.assertFalse(data["success"])
        self.assertEqual(self.fake.configured, [])

        data = await self.call("cimc_set_bios_setting", confirm=True, **args)
        self.assertTrue(data["success"])
        self.assertIn("Reboot", data["note"])
        sent = self.fake.configured[-1]
        self.assertEqual(sent.attrib["vpSelectMemoryRASConfiguration"], "Mirroring")

        with self.assertRaises(ToolError):
            await self.call(
                "cimc_set_bios_setting",
                confirm=True,
                **{**args, "attribute_name": "vp Bad"},
            )

    async def test_locator_led(self) -> None:
        data = await self.call("cimc_set_locator_led", address=ADDRESS, state="on")
        self.assertTrue(data["success"])
        self.assertEqual(data["requestedState"], "on")
        # FakeCimc applies the change, and the tool reads it back.
        self.assertEqual(
            data["currentState"], "off"
        )  # operState is not updated by the fake
        self.assertEqual(self.fake.configured[-1].attrib["adminState"], "on")

    async def test_sol_configure(self) -> None:
        data = await self.call(
            "cimc_sol_configure", address=ADDRESS, enabled=True, baud_rate="9600"
        )
        self.assertTrue(data["success"])
        self.assertEqual(data["adminState"], "enable")
        self.assertEqual(data["speed"], "9600")
        sent = self.fake.configured[-1]
        self.assertEqual(sent.tag, "solIf")

    async def test_cimc_errors_surface_as_tool_errors(self) -> None:
        self.fake.fail_login = True
        with self.assertRaises(ToolError) as ctx:
            await self.call("cimc_get_server_summary", address=ADDRESS)
        self.assertIn("551", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
