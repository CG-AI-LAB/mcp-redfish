# Copyright 2026 CG-AI-LAB
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""
MCP tools for Cisco CIMC (standalone UCS C-Series) via the Cisco XML API.

These tools complement the Redfish tools for servers whose CIMC firmware has
no Redfish support (CIMC 2.x) or where the XML API exposes more detail. Every
tool takes the ``address`` of a host configured in CIMC_HOSTS. Read-only tools
are always registered; tools that change server state are registered only when
CIMC_ALLOW_WRITES=true.

The tool set is ported from the schwarztim/cimc-mcp project (MIT).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ValidationError

from .. import common
from ..common.cimc_client import CimcClient
from ..common.server import mcp

logger = logging.getLogger(__name__)

PowerAction = Literal[
    "on",
    "off",
    "shutdown",
    "cycle",
    "reset",
    "bmc-reset",
    "bmc-reset-default",
    "diagnostic-interrupt",
]
FaultSeverity = Literal["critical", "major", "minor", "warning", "info", "cleared"]
BaudRate = Literal["9600", "19200", "38400", "57600", "115200"]

_POWER_ACTION_MAP: dict[str, str] = {
    "on": "up",
    "off": "down",
    "shutdown": "soft-shut-down",
    "cycle": "cycle-immediate",
    "reset": "hard-reset-immediate",
    "bmc-reset": "bmc-reset-immediate",
    "bmc-reset-default": "bmc-reset-default",
    "diagnostic-interrupt": "diagnostic-interrupt",
}
_DESTRUCTIVE_POWER_ACTIONS = {"off", "cycle", "reset", "bmc-reset", "bmc-reset-default"}
ToolFunction = Callable[..., Coroutine[Any, Any, Any]]

_SEVERITY_ORDER = {
    "critical": 0,
    "major": 1,
    "minor": 2,
    "warning": 3,
    "info": 4,
    "cleared": 5,
}


# -- helpers -----------------------------------------------------------------


def _host_config(address: str) -> dict[str, Any]:
    host = common.config.CIMC_CONFIG.get_host(address)
    if host is None:
        logger.error("CIMC %s not found in CIMC_HOSTS", address)
        raise ValidationError(f"CIMC server {address} is not configured in CIMC_HOSTS")
    return host


async def _with_client[T](address: str, fn: Callable[[CimcClient], T]) -> T:
    """Run ``fn`` with a client for ``address`` off the event loop."""
    host = _host_config(address)

    def runner() -> T:
        with CimcClient(host) as client:
            return fn(client)

    return await asyncio.to_thread(runner)


def _pick(mo: dict[str, Any], **fields: str) -> dict[str, Any]:
    """Build a result dict from selected attributes of a managed object."""
    return {out: mo.get(src) for out, src in fields.items()}


def _equipped(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Components physically present.

    Older firmware reports ``presence="equipped"``. Newer firmware omits
    ``presence`` on some classes (M.2 disks report ``driveSlotStatus``), so an
    object without a presence attribute counts as present.
    """
    present = []
    for item in items:
        presence = item.get("presence")
        if presence is None:
            if item.get("driveSlotStatus", "Present") != "Absent":
                present.append(item)
        elif presence == "equipped":
            present.append(item)
    return present


def _oper(item: dict[str, Any]) -> str | None:
    """Operational state; CIMC 2.x says ``operability``, newer says ``operState``."""
    return item.get("operability") or item.get("operState")


def _not_operable(item: dict[str, Any]) -> bool:
    state = _oper(item)
    return state is not None and state != "operable"


def _first_class(
    client: CimcClient, *class_ids: str, hierarchical: bool = False
) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve the first of ``class_ids`` that exists on this firmware.

    Returns the objects and the class name used, or ``([], None)`` when the
    firmware has none of them.
    """
    for class_id in class_ids:
        objects = client.resolve_class_optional(class_id, hierarchical)
        if objects is not None:
            return objects, class_id
    return [], None


def _optional_class(client: CimcClient, class_id: str) -> list[dict[str, Any]]:
    return client.resolve_class_optional(class_id) or []


def _without_class(mo: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mo.items() if k not in ("class", "children")}


_OK_DISK_STATES = {
    "Online",
    "Unconfigured Good",
    "JBOD",
    "Global Hot Spare",
    "Dedicated Hot Spare",
}


def _server_mo_class(server_dn: str) -> str:
    # Standalone rack servers are computeRackUnit; nodes inside a chassis
    # (for example S3260) are computeServerNode.
    return "computeRackUnit" if "rack-unit" in server_dn else "computeServerNode"


# -- read-only tool implementations -------------------------------------------


def _server_summary(client: CimcClient) -> dict[str, Any]:
    server = client.resolve_dn(client.server_dn)
    if server is None:
        return {"address": client.address, "error": "No server data returned"}
    return {
        "address": client.address,
        **_pick(
            server,
            name="name",
            model="model",
            vendor="vendor",
            serial="serial",
            serverId="serverId",
            uuid="uuid",
            powerState="operPower",
            adminPower="adminPower",
            presence="presence",
            biosPostState="biosPostState",
            assetTag="assetTag",
            userLabel="usrLbl",
            cpus="numOfCpus",
            cores="numOfCores",
            threads="numOfThreads",
            totalMemoryMB="totalMemory",
            availableMemoryMB="availableMemory",
            memorySpeedMHz="memorySpeed",
            dn="dn",
        ),
        "operability": _oper(server),
    }


def _temps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _pick(i, dn="dn", description="description", temperature="temperature")
        for i in items
    ]


def _board_temps(client: CimcClient) -> dict[str, Any] | None:
    boards = _optional_class(client, "computeRackUnitMbTempStats")
    return _without_class(boards[0]) if boards else None


def _sensors(client: CimcClient) -> dict[str, Any]:
    fans = client.resolve_class("equipmentFan")
    psus = client.resolve_class("equipmentPsu")
    cpu_env = _optional_class(client, "processorEnvStats")
    mem_env, _ = _first_class(client, "memoryEnvStats", "memoryUnitEnvStats")
    power = _optional_class(client, "computeMbPowerStats")
    return {
        "address": client.address,
        "board": _board_temps(client),
        "fans": [
            _pick(
                f,
                dn="dn",
                id="id",
                model="model",
                operability="operability",
                presence="presence",
                speed="speed",
            )
            for f in fans
        ],
        "powerSupplies": [
            _pick(
                p,
                dn="dn",
                id="id",
                model="model",
                vendor="vendor",
                serial="serial",
                operability="operability",
                presence="presence",
                voltage="voltage",
                power="power",
                thermal="thermal",
            )
            for p in psus
        ],
        "cpuTemperatures": _temps(cpu_env),
        "memoryTemperatures": _temps(mem_env),
        "gpuTemperatures": _temps(_optional_class(client, "gpuInventory")),
        "powerStats": [
            _pick(
                p,
                dn="dn",
                consumedPower="consumedPower",
                inputCurrent="inputCurrent",
                inputVoltage="inputVoltage",
            )
            for p in power
        ],
    }


def _server_health(client: CimcClient) -> dict[str, Any]:
    server = client.resolve_dn(client.server_dn) or {}
    faults = client.resolve_class("faultInst")
    dimms = client.resolve_class("memoryUnit")
    psus = client.resolve_class("equipmentPsu")
    fans = client.resolve_class("equipmentFan")
    vdrives = client.resolve_class("storageVirtualDrive")
    pdisks = client.resolve_class("storageLocalDisk")

    critical = [f for f in faults if f.get("severity") == "critical"]
    major = [f for f in faults if f.get("severity") == "major"]
    dimm_issues = [d for d in _equipped(dimms) if _not_operable(d)]
    psu_issues = [p for p in _equipped(psus) if _not_operable(p)]
    fan_issues = [f for f in _equipped(fans) if _not_operable(f)]
    vdrive_issues = [
        v for v in vdrives if v.get("health") and v.get("health") != "Good"
    ]
    pdisk_issues = [
        p
        for p in _equipped(pdisks)
        if (p.get("pdStatus") or "Online") not in _OK_DISK_STATES
        or p.get("health") not in (None, "", "Good")
    ]
    # Newer firmware has no operability attribute on the server object; its
    # absence is not a fault.
    healthy = (
        not critical
        and not major
        and not dimm_issues
        and not psu_issues
        and not fan_issues
        and not vdrive_issues
        and not pdisk_issues
        and not _not_operable(server)
        and server.get("operPower", "on") == "on"
    )
    return {
        "address": client.address,
        "overall": "HEALTHY" if healthy else "DEGRADED",
        "powerState": server.get("operPower", "unknown"),
        "operability": _oper(server) or "not-reported",
        "faults": {
            "critical": len(critical),
            "major": len(major),
            "total": len(faults),
            "details": [
                _pick(
                    f,
                    severity="severity",
                    code="code",
                    description="descr",
                    dn="dn",
                    created="created",
                )
                for f in faults[:20]
            ],
        },
        "components": {
            "dimms": {"total": len(_equipped(dimms)), "issues": len(dimm_issues)},
            "psus": {"total": len(_equipped(psus)), "issues": len(psu_issues)},
            "fans": {"total": len(_equipped(fans)), "issues": len(fan_issues)},
            "virtualDrives": {"total": len(vdrives), "issues": len(vdrive_issues)},
            "physicalDisks": {
                "total": len(_equipped(pdisks)),
                "issues": len(pdisk_issues),
            },
        },
    }


def _faults(client: CimcClient, severity: str | None) -> dict[str, Any]:
    faults = client.resolve_class("faultInst")
    if severity:
        faults = [f for f in faults if f.get("severity") == severity]
    faults.sort(
        key=lambda f: (
            _SEVERITY_ORDER.get(f.get("severity", ""), 99),
            # newest first within a severity
            tuple(-ord(ch) for ch in f.get("created", "")),
        )
    )
    return {
        "address": client.address,
        "total": len(faults),
        "faults": [
            _pick(
                f,
                severity="severity",
                code="code",
                description="descr",
                cause="cause",
                dn="dn",
                type="type",
                created="created",
                lastTransition="lastTransition",
                acknowledged="ack",
            )
            for f in faults
        ],
    }


def _event_log(client: CimcClient, limit: int) -> dict[str, Any]:
    entries, class_used = _first_class(client, "syseventLog", "logEntry")
    if class_used is None:
        # CIMC 4.x and later do not expose SEL entries as managed objects; the
        # XML API only reports the log container. Redfish does expose them.
        logs = _optional_class(client, "sysdebugMEpLog")
        sel = [_without_class(entry) for entry in logs if entry.get("type") == "SEL"]
        return {
            "address": client.address,
            "total": 0,
            "showing": 0,
            "entries": [],
            "logs": sel,
            "message": (
                "This CIMC firmware does not expose System Event Log entries "
                "through the XML API. Read them over Redfish with "
                "get_resource_data on https://"
                f"{client.address}/redfish/v1/Managers/CIMC/LogServices/SEL/Entries "
                "or use the CIMC CLI 'show sel'."
            ),
        }
    shown = entries[:limit]
    return {
        "address": client.address,
        "total": len(entries),
        "showing": len(shown),
        "entries": [
            {
                "dn": e.get("dn"),
                "id": e.get("id"),
                "severity": e.get("severity"),
                "description": e.get("descr") or e.get("description"),
                "created": e.get("created") or e.get("timeStamp"),
                "type": e.get("type"),
                "sensorType": e.get("sensorType"),
            }
            for e in shown
        ],
    }


def _inventory(client: CimcClient) -> dict[str, Any]:
    cpus = client.resolve_class("processorUnit")
    dimms = client.resolve_class("memoryUnit")
    pci = _optional_class(client, "pciEquipSlot")
    adapters = _optional_class(client, "adaptorUnit")
    controllers = _optional_class(client, "storageController")
    firmware = client.resolve_class("firmwareRunning")
    return {
        "address": client.address,
        "cpus": [
            _pick(
                c,
                dn="dn",
                id="id",
                model="model",
                vendor="vendor",
                arch="arch",
                cores="cores",
                coresEnabled="coresEnabled",
                threads="threads",
                speed="speed",
                presence="presence",
            )
            | {"operability": _oper(c)}
            for c in cpus
        ],
        "memory": [
            _pick(
                d,
                dn="dn",
                id="id",
                model="model",
                vendor="vendor",
                serial="serial",
                capacityMB="capacity",
                clockMHz="clock",
                type="type",
                formFactor="formFactor",
                location="location",
            )
            | {"operState": _oper(d)}
            for d in _equipped(dimms)
        ],
        "pciSlots": [
            _pick(
                p,
                dn="dn",
                id="id",
                model="model",
                vendor="vendor",
                controllerReported="controllerReported",
            )
            for p in pci
        ],
        "networkAdapters": [
            _pick(
                a,
                dn="dn",
                id="id",
                model="model",
                vendor="vendor",
                serial="serial",
                presence="presence",
            )
            for a in adapters
        ],
        "storageControllers": [
            _pick(
                c,
                dn="dn",
                id="id",
                model="model",
                vendor="vendor",
                serial="serial",
                raidSupport="raidSupport",
                type="type",
                presence="presence",
            )
            for c in controllers
        ],
        "firmwareVersions": [
            _pick(f, dn="dn", type="type", version="version", deployment="deployment")
            for f in firmware
        ],
    }


def _storage_summary(client: CimcClient) -> dict[str, Any]:
    controllers = _optional_class(client, "storageController")
    vdrives = _optional_class(client, "storageVirtualDrive")
    pdisks = _optional_class(client, "storageLocalDisk")
    batteries = _optional_class(client, "storageRaidBattery")
    return {
        "address": client.address,
        "controllers": [
            _pick(
                c,
                dn="dn",
                id="id",
                model="model",
                vendor="vendor",
                serial="serial",
                raidSupport="raidSupport",
                type="type",
                presence="presence",
                pciSlot="pciSlot",
                firmwareVersion="firmwareVersion",
            )
            for c in controllers
        ],
        "virtualDrives": [
            _pick(
                v,
                dn="dn",
                id="id",
                name="name",
                size="size",
                raidLevel="raidLevel",
                health="health",
                driveState="driveState",
                stripSize="stripSize",
                accessPolicy="accessPolicy",
                readPolicy="readPolicy",
                writePolicy="writePolicy",
            )
            for v in vdrives
        ],
        "physicalDisks": [
            _pick(
                p,
                dn="dn",
                id="id",
                vendor="vendor",
                model="model",
                productId="productId",
                serial="serial",
                driveSerialNumber="driveSerialNumber",
                size="coercedSize",
                status="pdStatus",
                health="health",
                linkSpeed="linkSpeed",
                mediaType="mediaType",
                predictiveFailureCount="predictiveFailureCount",
                driveState="driveState",
            )
            for p in pdisks
        ],
        "raidBatteries": [
            _pick(
                b,
                dn="dn",
                type="type",
                present="present",
                status="status",
                chargeStatus="chargeStatus",
                health="health",
                learnMode="learnMode",
            )
            for b in batteries
        ],
    }


def _network_adapters(client: CimcClient) -> dict[str, Any]:
    adapters = _optional_class(client, "adaptorUnit")
    ext_eth = _optional_class(client, "adaptorExtEthIf")
    host_eth = _optional_class(client, "adaptorHostEthIf")
    network_adapters = _optional_class(client, "networkAdapterUnit")
    adapter_ports = _optional_class(client, "networkAdapterEthIf")
    return {
        "address": client.address,
        "adapters": [
            _pick(
                a,
                dn="dn",
                id="id",
                model="model",
                vendor="vendor",
                serial="serial",
                presence="presence",
            )
            for a in adapters
        ],
        "externalPorts": [
            _pick(
                e,
                dn="dn",
                id="id",
                mac="mac",
                linkState="linkState",
                operSpeed="operSpeed",
                adminSpeed="adminSpeed",
            )
            for e in ext_eth
        ],
        "hostInterfaces": [
            _pick(
                h, dn="dn", name="name", mac="mac", mtu="mtu", uplinkPort="uplinkPort"
            )
            for h in host_eth
        ],
        "networkAdapters": [
            _pick(n, dn="dn", model="model", slot="slot", numIntf="numIntf")
            for n in network_adapters
        ],
        "networkAdapterPorts": [
            _pick(p, dn="dn", id="id", name="name", mac="mac") for p in adapter_ports
        ],
    }


def _firmware_versions(client: CimcClient) -> dict[str, Any]:
    firmware = client.resolve_class("firmwareRunning")
    return {
        "address": client.address,
        "total": len(firmware),
        "firmware": [
            _pick(
                f,
                dn="dn",
                type="type",
                version="version",
                deployment="deployment",
                packageVersion="packageVersion",
            )
            for f in firmware
        ],
    }


def _power_stats(client: CimcClient) -> dict[str, Any]:
    mb_power = _optional_class(client, "computeMbPowerStats")
    psus = client.resolve_class("equipmentPsu")
    budget = _optional_class(client, "powerBudget")
    monitors = _optional_class(client, "powerMonitor")
    return {
        "address": client.address,
        "motherboardPower": [
            _pick(
                p,
                dn="dn",
                consumedPower="consumedPower",
                inputCurrent="inputCurrent",
                inputVoltage="inputVoltage",
            )
            for p in mb_power
        ],
        "powerSupplies": [
            _pick(
                p,
                dn="dn",
                id="id",
                power="power",
                voltage="voltage",
                thermal="thermal",
                operability="operability",
            )
            for p in psus
        ],
        # Budget attributes differ between CIMC generations; pass them through.
        "powerBudget": [_without_class(b) for b in budget],
        "powerMonitor": [
            _pick(
                m,
                dn="dn",
                domain="domain",
                currentW="current",
                averageW="average",
                minimumW="minimum",
                maximumW="maximum",
            )
            for m in monitors
        ],
    }


def _thermal_stats(client: CimcClient) -> dict[str, Any]:
    cpu_env = _optional_class(client, "processorEnvStats")
    mem_env, _ = _first_class(client, "memoryEnvStats", "memoryUnitEnvStats")
    fans = client.resolve_class("equipmentFan")
    board = _board_temps(client)
    server = client.resolve_dn(client.server_dn) or {}
    ambient = (board or {}).get("ambientTemp") or server.get("ambientTemp") or "unknown"
    return {
        "address": client.address,
        "ambientTemp": ambient,
        "board": board,
        "cpuTemperatures": _temps(cpu_env),
        "memoryTemperatures": _temps(mem_env),
        "gpuTemperatures": _temps(_optional_class(client, "gpuInventory")),
        "fans": [
            _pick(f, dn="dn", id="id", speed="speed", operability="operability")
            for f in fans
        ],
    }


def _bios_settings(client: CimcClient) -> dict[str, Any]:
    settings = client.resolve_dn(f"{client.server_dn}/bios/bios-settings", True)
    if settings is None:
        return {"address": client.address, "total": 0, "tokens": []}
    parent_dn = settings.get("dn") or f"{client.server_dn}/bios/bios-settings"
    tokens = []
    for child in settings.get("children", []):
        attributes = {k: v for k, v in child.items() if k not in ("class", "children")}
        # Newer firmware returns only the relative name (rn) on each token.
        dn = child.get("dn") or (
            f"{parent_dn}/{child['rn']}" if child.get("rn") else ""
        )
        tokens.append(
            {
                "className": child.get("class"),
                "dn": dn,
                "attributes": attributes,
            }
        )
    return {"address": client.address, "total": len(tokens), "tokens": tokens}


# -- read-only tools -----------------------------------------------------------


async def cimc_list_servers() -> list[str]:
    """
    List the Cisco CIMC (UCS C-Series XML API) servers configured in CIMC_HOSTS.

    Returns:
        list: Addresses that the cimc_* tools accept as ``address``.
    """
    logger.info("Listing configured CIMC servers.")
    return [host.address for host in common.config.CIMC_CONFIG.hosts]


async def cimc_get_server_summary(address: str) -> dict:
    """
    Get overall status of a Cisco CIMC-managed server: power state, model,
    serial number, CPU/memory counts and operability.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _server_summary)


async def cimc_get_server_health(address: str) -> dict:
    """
    Composite health check of a CIMC-managed server: active faults plus
    DIMM, PSU, fan and storage status. Answers "Is this server healthy?".

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _server_health)


async def cimc_get_sensors(address: str) -> dict:
    """
    Get all sensor readings from a CIMC-managed server: fans, power supplies,
    CPU and memory temperatures and motherboard power.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _sensors)


async def cimc_get_faults(address: str, severity: FaultSeverity | None = None) -> dict:
    """
    List active faults on a CIMC-managed server, most severe first.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
        severity (str, optional): Only return faults of this severity
            (critical, major, minor, warning, info, cleared).
    """
    return await _with_client(address, lambda c: _faults(c, severity))


async def cimc_get_event_log(address: str, limit: int = 50) -> dict:
    """
    Get System Event Log (SEL) entries from a CIMC-managed server.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
        limit (int): Maximum number of entries to return (default 50).
    """
    if limit < 1 or limit > 10000:
        raise ValidationError("limit must be between 1 and 10000")
    return await _with_client(address, lambda c: _event_log(c, limit))


async def cimc_get_inventory(address: str) -> dict:
    """
    Complete hardware inventory of a CIMC-managed server: CPUs, DIMMs, PCI
    slots, network adapters, storage controllers and firmware versions.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _inventory)


async def cimc_get_storage_summary(address: str) -> dict:
    """
    Storage overview of a CIMC-managed server: RAID controllers, virtual
    drives, physical disks and RAID batteries with health and status.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _storage_summary)


async def cimc_get_network_adapters(address: str) -> dict:
    """
    Network adapters of a CIMC-managed server with MAC addresses, link state
    and host interface details.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _network_adapters)


async def cimc_get_firmware_versions(address: str) -> dict:
    """
    Running firmware versions of every component: CIMC, BIOS, storage
    controllers, network adapters and more.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _firmware_versions)


async def cimc_get_power_stats(address: str) -> dict:
    """
    Power statistics of a CIMC-managed server: motherboard input voltage,
    current and consumed power, PSU readings and the power budget.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _power_stats)


async def cimc_get_thermal_stats(address: str) -> dict:
    """
    Thermal readings of a CIMC-managed server: ambient, CPU and memory
    temperatures and fan speeds.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _thermal_stats)


async def cimc_get_bios_settings(address: str) -> dict:
    """
    Current BIOS token values of a CIMC-managed server. Each token lists its
    class name, DN and attributes, which cimc_set_bios_setting needs.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
    """
    return await _with_client(address, _bios_settings)


async def cimc_resolve_class(
    address: str, class_id: str, hierarchical: bool = False
) -> dict:
    """
    Generic CIMC XML API query: return every managed object of a class
    (configResolveClass), e.g. 'computeRackUnit', 'memoryUnit', 'faultInst'.
    Use this for data the dedicated cimc_* tools do not expose.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
        class_id (str): CIMC class name.
        hierarchical (bool): Include child objects (default false).
    """
    objects = await _with_client(
        address, lambda c: c.resolve_class(class_id, hierarchical)
    )
    return {
        "address": address,
        "classId": class_id,
        "total": len(objects),
        "objects": objects,
    }


async def cimc_resolve_dn(address: str, dn: str, hierarchical: bool = False) -> dict:
    """
    Generic CIMC XML API query: return the managed object at a distinguished
    name (configResolveDn), e.g. 'sys/rack-unit-1' or 'sys/rack-unit-1/mgmt/if-1'.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
        dn (str): Distinguished name of the object.
        hierarchical (bool): Include child objects (default false).
    """
    mo = await _with_client(address, lambda c: c.resolve_dn(dn, hierarchical))
    return {"address": address, "dn": dn, "found": mo is not None, "object": mo}


# -- write tools (registered only with CIMC_ALLOW_WRITES=true) ------------------


async def cimc_power_control(
    address: str, action: PowerAction, confirm: bool = False
) -> dict:
    """
    Control server power on a CIMC-managed server: on, off, graceful shutdown,
    power cycle, hard reset, BMC reset or diagnostic interrupt. WARNING: 'off',
    'cycle' and 'reset' are immediate and can cause data loss; they, and the
    BMC resets, require confirm=true.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
        action (str): on | off | shutdown | cycle | reset | bmc-reset |
            bmc-reset-default | diagnostic-interrupt.
        confirm (bool): Must be true for destructive actions.
    """
    if action in _DESTRUCTIVE_POWER_ACTIONS and not confirm:
        return {
            "address": address,
            "action": action,
            "success": False,
            "message": (
                f"Destructive action '{action}' requires confirm=true. "
                "It may cause data loss or service interruption."
            ),
        }
    admin_power = _POWER_ACTION_MAP[action]

    def run(client: CimcClient) -> dict[str, Any]:
        result = client.config_mo(
            client.server_dn,
            _server_mo_class(client.server_dn),
            {"adminPower": admin_power},
        )
        return {
            "address": client.address,
            "action": action,
            "adminPower": admin_power,
            "success": True,
            "result": result or "Command accepted",
        }

    logger.warning("CIMC power action %s requested for %s", action, address)
    return await _with_client(address, run)


async def cimc_set_bios_setting(
    address: str,
    token_dn: str,
    class_name: str,
    attribute_name: str,
    value: str,
    confirm: bool = False,
) -> dict:
    """
    Modify one BIOS token on a CIMC-managed server. Use cimc_get_bios_settings
    first to find the token DN, class name and attribute name. The change takes
    effect after the next server reboot.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
        token_dn (str): Token DN, e.g.
            'sys/rack-unit-1/bios/bios-settings/Select-Memory-RAS-configuration'.
        class_name (str): Token class, e.g. 'biosVfSelectMemoryRASConfiguration'.
        attribute_name (str): Attribute to set, e.g. 'vpSelectMemoryRASConfiguration'.
        value (str): New value.
        confirm (bool): Must be true to apply the change.
    """
    if not confirm:
        return {
            "address": address,
            "success": False,
            "message": "BIOS modification requires confirm=true. Changes apply after reboot.",
        }

    def run(client: CimcClient) -> dict[str, Any]:
        result = client.config_mo(token_dn, class_name, {attribute_name: value})
        return {
            "address": client.address,
            "success": True,
            "tokenDn": token_dn,
            "attribute": attribute_name,
            "newValue": value,
            "note": "Reboot required for BIOS changes to take effect",
            "result": result or "Command accepted",
        }

    logger.warning("CIMC BIOS change requested for %s: %s", address, token_dn)
    return await _with_client(address, run)


async def cimc_set_locator_led(address: str, state: Literal["on", "off"]) -> dict:
    """
    Turn the chassis locator LED of a CIMC-managed server on or off to
    identify it physically in the rack.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
        state (str): 'on' or 'off'.
    """

    def run(client: CimcClient) -> dict[str, Any]:
        dn = f"{client.server_dn}/locator-led"
        result = client.config_mo(dn, "equipmentLocatorLed", {"adminState": state})
        current = client.resolve_dn(dn) or {}
        return {
            "address": client.address,
            "success": True,
            "requestedState": state,
            "currentState": current.get("operState")
            or current.get("adminState")
            or "unknown",
            "result": result or "Command accepted",
        }

    return await _with_client(address, run)


async def cimc_sol_configure(
    address: str, enabled: bool, baud_rate: BaudRate | None = None
) -> dict:
    """
    Enable, disable or configure Serial over LAN (SOL) on a CIMC-managed
    server. SOL provides remote serial console access.

    Args:
        address (str): CIMC address as configured in CIMC_HOSTS.
        enabled (bool): Enable (true) or disable (false) SOL.
        baud_rate (str, optional): 9600, 19200, 38400, 57600 or 115200.
    """

    def run(client: CimcClient) -> dict[str, Any]:
        dn = f"{client.server_dn}/sol-if"
        attributes = {"adminState": "enable" if enabled else "disable"}
        if baud_rate:
            attributes["speed"] = baud_rate
        client.config_mo(dn, "solIf", attributes)
        current = client.resolve_dn(dn) or {}
        return {
            "address": client.address,
            "success": True,
            "adminState": current.get("adminState", "unknown"),
            "speed": current.get("speed", "unknown"),
            "dn": dn,
        }

    return await _with_client(address, run)


READ_TOOLS: tuple[ToolFunction, ...] = (
    cimc_list_servers,
    cimc_get_server_summary,
    cimc_get_server_health,
    cimc_get_sensors,
    cimc_get_faults,
    cimc_get_event_log,
    cimc_get_inventory,
    cimc_get_storage_summary,
    cimc_get_network_adapters,
    cimc_get_firmware_versions,
    cimc_get_power_stats,
    cimc_get_thermal_stats,
    cimc_get_bios_settings,
    cimc_resolve_class,
    cimc_resolve_dn,
)
WRITE_TOOLS: tuple[ToolFunction, ...] = (
    cimc_power_control,
    cimc_set_bios_setting,
    cimc_set_locator_led,
    cimc_sol_configure,
)


def register(server: FastMCP, allow_writes: bool) -> None:
    """Register the CIMC tools on ``server``.

    Write tools are left out unless ``allow_writes`` is true, so a server that
    was not explicitly configured for changes cannot be talked into making one.
    """
    for read_tool in READ_TOOLS:
        server.tool(read_tool)
    if allow_writes:
        for write_tool in WRITE_TOOLS:
            server.tool(write_tool)
        logger.warning(
            "CIMC write tools are enabled (CIMC_ALLOW_WRITES=true): "
            "power, BIOS, locator LED and SOL changes are possible"
        )
    else:
        logger.info("CIMC write tools are disabled (CIMC_ALLOW_WRITES is not true)")


register(mcp, common.config.CIMC_CONFIG.allow_writes)
