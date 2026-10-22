# Copyright 2026 CG-AI-LAB
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""An in-memory Cisco CIMC XML API endpoint served through httpx2.MockTransport."""

from __future__ import annotations

from typing import Any
from xml.etree.ElementTree import Element  # nosec B405

import httpx2
from defusedxml import ElementTree as SafeElementTree

SERVER_DN = "sys/rack-unit-1"


def _attrs(attributes: dict[str, str]) -> str:
    return "".join(f' {k}="{v}"' for k, v in attributes.items())


def mo_xml(class_name: str, attributes: dict[str, str], children: str = "") -> str:
    if children:
        return f"<{class_name}{_attrs(attributes)}>{children}</{class_name}>"
    return f"<{class_name}{_attrs(attributes)}/>"


class FakeCimc:
    """Answers XML API requests the way a standalone C-Series CIMC does."""

    def __init__(self) -> None:
        self.requests: list[Element] = []
        self.login_count = 0
        self.refresh_count = 0
        self.logout_count = 0
        self.cookie = "1700000000/aaaa-bbbb"
        self.refresh_period = "600"
        self.fail_login = False
        self.expire_next_request = False
        self.http_status: int | None = None
        self.raw_response: str | None = None
        self.transport_error: Exception | None = None
        # class name -> list of (attributes, children-xml)
        self.classes: dict[str, list[tuple[dict[str, str], str]]] = {}
        # dn -> (class name, attributes, children-xml)
        self.dns: dict[str, tuple[str, dict[str, str], str]] = {}
        self.configured: list[Element] = []
        # Classes that exist on this firmware but have no instances.
        self.empty_classes: set[str] = {
            "adaptorUnit",
            "adaptorExtEthIf",
            "adaptorHostEthIf",
            "networkAdapterEthIf",
            "gpuInventory",
            "powerMonitor",
            "computeRackUnitMbTempStats",
        }

    # -- data population -------------------------------------------------------

    def add(
        self, class_name: str, attributes: dict[str, str], children: str = ""
    ) -> None:
        attributes = dict(attributes)
        self.classes.setdefault(class_name, []).append((attributes, children))
        if "dn" in attributes:
            self.dns[attributes["dn"]] = (class_name, attributes, children)

    def add_rack_unit(self, **overrides: str) -> None:
        attributes = {
            "dn": SERVER_DN,
            "name": "UCS C220 M4S",
            "model": "UCSC-C220-M4S",
            "vendor": "Cisco Systems Inc",
            "serial": "FCH1234ABCD",
            "serverId": "1",
            "uuid": "11111111-2222-3333-4444-555555555555",
            "operPower": "on",
            "adminPower": "policy",
            "operability": "operable",
            "presence": "equipped",
            "numOfCpus": "2",
            "numOfCores": "24",
            "numOfThreads": "48",
            "totalMemory": "131072",
            "availableMemory": "131072",
            "ambientTemp": "23.0",
        }
        attributes.update(overrides)
        self.add("computeRackUnit", attributes)

    # -- transport -------------------------------------------------------------

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handler))

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if self.transport_error is not None:
            error = self.transport_error
            raise error
        if self.http_status is not None:
            return httpx2.Response(self.http_status, text="error")
        if self.raw_response is not None:
            return httpx2.Response(200, text=self.raw_response)
        root = SafeElementTree.fromstring(request.content.decode("utf-8"))
        self.requests.append(root)
        method = getattr(self, f"_handle_{root.tag}", None)
        if method is None:
            return self._error(root.tag, "100", f"unsupported operation {root.tag}")
        return httpx2.Response(200, text=method(root))

    def _error(self, tag: str, code: str, descr: str) -> httpx2.Response:
        return httpx2.Response(200, text=self._error_xml(tag, code, descr))

    @staticmethod
    def _error_xml(tag: str, code: str, descr: str) -> str:
        return (
            f'<{tag} cookie="" response="yes" errorCode="{code}" '
            f'invocationResult="unidentified-fail" errorDescr="{descr}"> </{tag}>'
        )

    def _check_cookie(self, root: Element) -> str | None:
        if self.expire_next_request:
            self.expire_next_request = False
            return self._error_xml(root.tag, "552", "Authorization required")
        if root.attrib.get("cookie") != self.cookie:
            return self._error_xml(root.tag, "552", "Authorization required")
        return None

    # -- operations ------------------------------------------------------------

    def _handle_aaaLogin(self, root: Element) -> str:  # noqa: N802
        self.login_count += 1
        if self.fail_login:
            return self._error_xml("aaaLogin", "551", "Authentication failed")
        return (
            f'<aaaLogin cookie="" response="yes" outCookie="{self.cookie}" '
            f'outRefreshPeriod="{self.refresh_period}" outPriv="admin" '
            f'outSessionId="17" outVersion="2.0(10l)"> </aaaLogin>'
        )

    def _handle_aaaRefresh(self, root: Element) -> str:  # noqa: N802
        self.refresh_count += 1
        self.cookie = self.cookie + "r"
        return (
            f'<aaaRefresh cookie="{root.attrib.get("cookie")}" response="yes" '
            f'outCookie="{self.cookie}" outRefreshPeriod="{self.refresh_period}"> '
            "</aaaRefresh>"
        )

    def _handle_aaaLogout(self, root: Element) -> str:  # noqa: N802
        self.logout_count += 1
        return '<aaaLogout cookie="" response="yes" outStatus="success"> </aaaLogout>'

    def _render(
        self,
        class_name: str,
        attributes: dict[str, str],
        children: str,
        hierarchical: bool,
    ) -> str:
        return mo_xml(class_name, attributes, children if hierarchical else "")

    def _handle_configResolveClass(self, root: Element) -> str:  # noqa: N802
        error = self._check_cookie(root)
        if error:
            return error
        class_id = root.attrib.get("classId", "")
        hierarchical = root.attrib.get("inHierarchical") == "true"
        if class_id not in self.classes and class_id not in self.empty_classes:
            return self._error_xml(
                "configResolveClass",
                "ERR-xml-parse-error",
                f"XML PARSING ERROR: no class named {class_id}",
            )
        body = "".join(
            self._render(class_id, attributes, children, hierarchical)
            for attributes, children in self.classes.get(class_id, [])
        )
        return (
            f'<configResolveClass cookie="{self.cookie}" response="yes" '
            f'classId="{class_id}"><outConfigs>{body}</outConfigs></configResolveClass>'
        )

    def _handle_configResolveDn(self, root: Element) -> str:  # noqa: N802
        error = self._check_cookie(root)
        if error:
            return error
        dn = root.attrib.get("dn", "")
        hierarchical = root.attrib.get("inHierarchical") == "true"
        entry = self.dns.get(dn)
        body = self._render(*entry, hierarchical) if entry else ""
        return (
            f'<configResolveDn cookie="{self.cookie}" response="yes" dn="{dn}">'
            f"<outConfig>{body}</outConfig></configResolveDn>"
        )

    def _handle_configResolveChildren(self, root: Element) -> str:  # noqa: N802
        error = self._check_cookie(root)
        if error:
            return error
        parent = root.attrib.get("inDn", "")
        class_id = root.attrib.get("classId")
        body = ""
        for class_name, entries in self.classes.items():
            if class_id and class_name != class_id:
                continue
            for attributes, _children in entries:
                dn = attributes.get("dn", "")
                if dn.startswith(parent + "/") and "/" not in dn[len(parent) + 1 :]:
                    body += mo_xml(class_name, attributes)
        return (
            f'<configResolveChildren cookie="{self.cookie}" response="yes" '
            f'inDn="{parent}"><outConfigs>{body}</outConfigs></configResolveChildren>'
        )

    def _handle_configConfMo(self, root: Element) -> str:  # noqa: N802
        error = self._check_cookie(root)
        if error:
            return error
        in_config = root.find("inConfig")
        mo = list(in_config)[0] if in_config is not None and len(in_config) else None
        if mo is None:
            return self._error_xml("configConfMo", "150", "missing inConfig")
        self.configured.append(mo)
        dn = mo.attrib.get("dn", "")
        entry = self.dns.get(dn)
        if entry:
            class_name, attributes, children = entry
            attributes.update({k: v for k, v in mo.attrib.items() if k != "dn"})
        else:
            class_name, attributes = mo.tag, dict(mo.attrib)
        rendered = mo_xml(class_name, {**attributes, "status": "modified"})
        return (
            f'<configConfMo dn="{dn}" cookie="{self.cookie}" response="yes">'
            f"<outConfig>{rendered}</outConfig></configConfMo>"
        )


def populate_healthy_server(fake: FakeCimc) -> None:
    """Load a plausible healthy C220 M4 inventory into ``fake``."""
    fake.add_rack_unit()
    for i in (1, 2):
        fake.add(
            "processorUnit",
            {
                "dn": f"{SERVER_DN}/board/cpu-{i}",
                "id": str(i),
                "model": "Intel(R) Xeon(R) CPU E5-2680 v3",
                "vendor": "Intel(R) Corporation",
                "arch": "Xeon",
                "cores": "12",
                "coresEnabled": "12",
                "threads": "24",
                "speed": "2.5",
                "operability": "operable",
                "presence": "equipped",
            },
        )
        fake.add(
            "processorEnvStats",
            {"dn": f"{SERVER_DN}/board/cpu-{i}/env-stats", "temperature": "45.0"},
        )
    for i in range(1, 5):
        fake.add(
            "memoryUnit",
            {
                "dn": f"{SERVER_DN}/board/memarray-1/mem-{i}",
                "id": str(i),
                "model": "M393A4K40BB1-CRC",
                "vendor": "0xCE00",
                "serial": f"SER{i}",
                "capacity": "32768",
                "clock": "2133",
                "type": "DDR4",
                "formFactor": "DIMM",
                "location": f"DIMM_A{i}",
                "operState": "operable",
                "presence": "equipped",
            },
        )
    fake.add(
        "memoryUnit",
        {
            "dn": f"{SERVER_DN}/board/memarray-1/mem-5",
            "id": "5",
            "presence": "missing",
            "operState": "unknown",
        },
    )
    fake.add(
        "memoryEnvStats",
        {
            "dn": f"{SERVER_DN}/board/memarray-1/mem-1/dimm-env-stats",
            "temperature": "31.0",
        },
    )
    for i in (1, 2):
        fake.add(
            "equipmentPsu",
            {
                "dn": f"{SERVER_DN}/psu-{i}",
                "id": str(i),
                "model": "UCSC-PSU1-770W",
                "vendor": "Cisco Systems Inc",
                "serial": f"PSU{i}",
                "operability": "operable",
                "presence": "equipped",
                "voltage": "12.0",
                "power": "120",
                "thermal": "ok",
            },
        )
    for i in range(1, 7):
        fake.add(
            "equipmentFan",
            {
                "dn": f"{SERVER_DN}/fan-module-1-{i}/fan-1",
                "id": "1",
                "model": "",
                "operability": "operable",
                "presence": "equipped",
                "speed": "6200",
            },
        )
    fake.add(
        "computeMbPowerStats",
        {
            "dn": f"{SERVER_DN}/board/power-stats",
            "consumedPower": "210",
            "inputCurrent": "1.8",
            "inputVoltage": "118",
        },
    )
    fake.add(
        "storageController",
        {
            "dn": f"{SERVER_DN}/board/storage-SAS-SLOT-HBA",
            "id": "SLOT-HBA",
            "model": "UCSC-MRAID12G",
            "vendor": "Cisco Systems Inc",
            "serial": "RAID1",
            "raidSupport": "RAID0, RAID1",
            "type": "SAS",
            "presence": "equipped",
            "pciSlot": "HBA",
            "firmwareVersion": "4.620.00-7259",
        },
    )
    fake.add(
        "storageVirtualDrive",
        {
            "dn": f"{SERVER_DN}/board/storage-SAS-SLOT-HBA/vd-0",
            "id": "0",
            "name": "boot",
            "size": "952720 MB",
            "raidLevel": "RAID 1",
            "health": "Good",
            "driveState": "Optimal",
            "stripSize": "64 KB",
            "accessPolicy": "Read-Write",
            "readPolicy": "No Read Ahead",
            "writePolicy": "Write Through",
        },
    )
    for i in (1, 2):
        fake.add(
            "storageLocalDisk",
            {
                "dn": f"{SERVER_DN}/board/storage-SAS-SLOT-HBA/pd-{i}",
                "id": str(i),
                "vendor": "SEAGATE",
                "model": "ST1000NX0453",
                "serial": f"DISK{i}",
                "coercedSize": "952720 MB",
                "pdStatus": "Online",
                "linkSpeed": "12.0 Gb/s",
                "mediaType": "HDD",
                "predictiveFailureCount": "0",
                "driveState": "Online",
                "presence": "equipped",
            },
        )
    fake.add(
        "storageRaidBattery",
        {
            "dn": f"{SERVER_DN}/board/storage-SAS-SLOT-HBA/raid-battery",
            "type": "SuperCap",
            "present": "true",
            "status": "Optimal",
            "chargeStatus": "Charged",
            "health": "Good",
            "learnMode": "Auto",
        },
    )
    fake.add(
        "firmwareRunning",
        {
            "dn": f"{SERVER_DN}/mgmt/fw-system",
            "type": "blade-controller",
            "version": "2.0(10l)",
            "deployment": "system",
            "packageVersion": "",
        },
    )
    fake.add(
        "firmwareRunning",
        {
            "dn": f"{SERVER_DN}/bios/fw-boot-loader",
            "type": "blade-bios",
            "version": "C220M4.2.0.10c",
            "deployment": "boot-loader",
            "packageVersion": "",
        },
    )
    fake.add(
        "adaptorUnit",
        {
            "dn": f"{SERVER_DN}/adaptor-MLOM",
            "id": "MLOM",
            "model": "UCSC-MLOM-CSC-02",
            "vendor": "Cisco Systems Inc",
            "serial": "VIC1",
            "presence": "equipped",
        },
    )
    fake.add(
        "adaptorExtEthIf",
        {
            "dn": f"{SERVER_DN}/adaptor-MLOM/ext-eth-0",
            "id": "0",
            "mac": "00:11:22:33:44:55",
            "linkState": "up",
            "operSpeed": "10gbps",
            "adminSpeed": "auto",
        },
    )
    fake.add(
        "adaptorHostEthIf",
        {
            "dn": f"{SERVER_DN}/adaptor-MLOM/host-eth-eth0",
            "name": "eth0",
            "mac": "00:11:22:33:44:56",
            "mtu": "1500",
            "uplinkPort": "0",
        },
    )
    fake.add(
        "networkAdapterUnit",
        {"dn": f"{SERVER_DN}/network-adapter-L", "model": "Intel X540", "numIntf": "2"},
    )
    fake.add(
        "pciEquipSlot",
        {
            "dn": f"{SERVER_DN}/equipped-slot-1",
            "id": "1",
            "model": "UCSC-PCIE-IRJ45",
            "vendor": "Intel",
            "controllerReported": "1",
        },
    )
    fake.add(
        "powerBudget",
        {
            "dn": f"{SERVER_DN}/budget",
            "powerConsumed": "210",
            "powerAvailable": "770",
            "powerCap": "0",
            "powerCharacterization": "Disabled",
        },
    )
    fake.add(
        "equipmentLocatorLed",
        {"dn": f"{SERVER_DN}/locator-led", "adminState": "off", "operState": "off"},
    )
    fake.add(
        "solIf",
        {"dn": f"{SERVER_DN}/sol-if", "adminState": "disable", "speed": "115200"},
    )
    bios_children = mo_xml(
        "biosVfSelectMemoryRASConfiguration",
        {
            "dn": f"{SERVER_DN}/bios/bios-settings/SelectMemory-RAS-configuration",
            "rn": "SelectMemory-RAS-configuration",
            "vpSelectMemoryRASConfiguration": "Maximum Performance",
        },
    ) + mo_xml(
        "biosVfIntelHyperThreadingTech",
        {
            "dn": f"{SERVER_DN}/bios/bios-settings/Intel-HyperThreading-Tech",
            "rn": "Intel-HyperThreading-Tech",
            "vpIntelHyperThreadingTech": "Enabled",
        },
    )
    fake.add(
        "biosSettings",
        {"dn": f"{SERVER_DN}/bios/bios-settings", "rn": "bios-settings"},
        bios_children,
    )
    fake.add(
        "biosVfSelectMemoryRASConfiguration",
        {
            "dn": f"{SERVER_DN}/bios/bios-settings/SelectMemory-RAS-configuration",
            "vpSelectMemoryRASConfiguration": "Maximum Performance",
        },
    )
    for i, (sev, code) in enumerate(
        [("major", "F0185"), ("critical", "F0181"), ("info", "F0999")], start=1
    ):
        fake.add(
            "faultInst",
            {
                "dn": f"sys/rack-unit-1/fault-F{i}",
                "severity": sev,
                "code": code,
                "descr": f"Fault {i}",
                "cause": "equipment-degraded",
                "type": "equipment",
                "created": f"2026-10-0{i}T00:00:00",
                "lastTransition": f"2026-10-0{i}T00:00:00",
                "ack": "no",
            },
        )
    fake.add(
        "syseventLog",
        {
            "dn": "sys/rack-unit-1/mgmt/log-SEL-1",
            "id": "1",
            "severity": "informational",
            "descr": "System Boot Initiated",
            "created": "2026-10-01T00:00:00",
            "type": "System Event",
            "sensorType": "System Boot",
        },
    )
    fake.add(
        "syseventLog",
        {
            "dn": "sys/rack-unit-1/mgmt/log-SEL-2",
            "id": "2",
            "severity": "informational",
            "descr": "Power Unit: Power Off",
            "created": "2026-10-02T00:00:00",
            "type": "System Event",
            "sensorType": "Power Unit",
        },
    )


__all__: list[str] = ["FakeCimc", "SERVER_DN", "mo_xml", "populate_healthy_server"]


def _unused(_: Any) -> None:  # pragma: no cover - keeps typing import used
    return None
