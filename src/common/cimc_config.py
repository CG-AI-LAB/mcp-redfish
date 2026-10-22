# Copyright 2026 CG-AI-LAB
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""
Configuration for Cisco CIMC (Integrated Management Controller) endpoints.

Standalone Cisco UCS C-Series servers expose Cisco's XML API at
``https://<cimc>/nuova`` in addition to Redfish. CIMC_HOSTS mirrors
REDFISH_HOSTS: a JSON array of host objects with optional per-host overrides.
Validation is fail-closed, matching the Redfish configuration: a value this
module cannot validate aborts startup rather than being replaced by a guess.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .validation import ConfigurationError, ConfigValidator

logger = logging.getLogger(__name__)

DEFAULT_SERVER_DN = "sys/rack-unit-1"

# A CIMC distinguished name: path segments of letters, digits and a small set
# of punctuation. Rejecting anything else keeps user input out of XML markup.
_DN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-/\[\]]*$")


def is_valid_dn(value: str) -> bool:
    """Return True when ``value`` looks like a CIMC distinguished name."""
    return bool(value) and len(value) <= 512 and _DN_PATTERN.match(value) is not None


@dataclass
class CimcHostConfig:
    """Configuration for a single CIMC endpoint."""

    address: str
    port: int | None = None
    username: str | None = None
    password: str | None = None
    tls_server_ca_cert: str | None = None
    tls_verify: bool | None = None
    server_dn: str = DEFAULT_SERVER_DN

    def __post_init__(self) -> None:
        if not self.address or not isinstance(self.address, str):
            raise ValueError("Host address cannot be empty")
        if self.port is not None and (
            not isinstance(self.port, int) or self.port < 1 or self.port > 65535
        ):
            raise ValueError(f"Port must be between 1 and 65535, got: {self.port}")
        if self.tls_verify is not None and not isinstance(self.tls_verify, bool):
            raise ValueError("tls_verify must be a boolean")
        if not isinstance(self.server_dn, str) or not is_valid_dn(self.server_dn):
            raise ValueError(f"server_dn is not a valid CIMC DN: {self.server_dn!r}")


@dataclass
class CimcConfig:
    """Complete CIMC configuration."""

    hosts: list[CimcHostConfig] = field(default_factory=list)
    port: int = 443
    username: str = ""
    password: str = ""
    tls_server_ca_cert: str | None = None
    tls_verify: bool = True
    allow_writes: bool = False
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if self.port < 1 or self.port > 65535:
            raise ValueError(f"Port must be between 1 and 65535, got: {self.port}")
        if not isinstance(self.tls_verify, bool):
            raise ValueError("tls_verify must be a boolean")
        if not isinstance(self.allow_writes, bool):
            raise ValueError("allow_writes must be a boolean")
        if self.timeout <= 0:
            raise ValueError(f"Timeout must be positive, got: {self.timeout}")
        addresses = [host.address for host in self.hosts]
        if len(addresses) != len(set(addresses)):
            raise ValueError("CIMC_HOSTS contains duplicate addresses")

    def host_dict(self, host: CimcHostConfig) -> dict[str, Any]:
        """Return a host entry with global defaults applied."""
        return {
            "address": host.address,
            "port": host.port or self.port,
            "username": host.username if host.username is not None else self.username,
            "password": host.password if host.password is not None else self.password,
            "tls_server_ca_cert": host.tls_server_ca_cert or self.tls_server_ca_cert,
            "tls_verify": host.tls_verify
            if host.tls_verify is not None
            else self.tls_verify,
            "server_dn": host.server_dn,
            "timeout": self.timeout,
        }

    def get_host(self, address: str) -> dict[str, Any] | None:
        """Return the resolved host entry for ``address`` or None."""
        for host in self.hosts:
            if host.address == address:
                return self.host_dict(host)
        return None


def parse_cimc_hosts(hosts_json: str) -> list[CimcHostConfig]:
    """Parse and validate CIMC_HOSTS."""
    try:
        hosts_data = json.loads(hosts_json)
    except json.JSONDecodeError as e:
        raise ConfigurationError(f"Invalid JSON in CIMC_HOSTS: {e}") from e

    if not isinstance(hosts_data, list):
        raise ConfigurationError("CIMC_HOSTS must be a JSON array")

    hosts: list[CimcHostConfig] = []
    for i, host_data in enumerate(hosts_data):
        if not isinstance(host_data, dict):
            raise ConfigurationError(f"CIMC host {i} must be a JSON object")
        try:
            hosts.append(CimcHostConfig(**host_data))
        except (TypeError, ValueError) as e:
            raise ConfigurationError(
                f"Invalid CIMC host configuration at index {i}: {e}"
            ) from e
    return hosts


def _get_env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as e:
        raise ConfigurationError(f"{key} must be a number, got: {raw!r}") from e
    if value <= 0:
        raise ConfigurationError(f"{key} must be positive, got: {raw!r}")
    return value


def load_cimc_config() -> CimcConfig:
    """Load and validate CIMC configuration from the environment.

    CIMC support is optional: an unset CIMC_HOSTS yields an empty host list and
    the CIMC tools report that no servers are configured.
    """
    try:
        hosts = parse_cimc_hosts(os.getenv("CIMC_HOSTS", "[]"))
        config = CimcConfig(
            hosts=hosts,
            port=ConfigValidator.get_env_int("CIMC_PORT", 443, 1, 65535),
            username=os.getenv("CIMC_USERNAME", ""),
            password=os.getenv("CIMC_PASSWORD", ""),
            tls_server_ca_cert=os.getenv("CIMC_SERVER_CA_CERT") or None,
            tls_verify=ConfigValidator.get_env_bool("CIMC_TLS_VERIFY", True),
            allow_writes=ConfigValidator.get_env_bool("CIMC_ALLOW_WRITES", False),
            timeout=_get_env_float("CIMC_TIMEOUT", 30.0),
        )
    except ConfigurationError:
        raise
    except Exception as e:
        raise ConfigurationError(f"Failed to load CIMC configuration: {e}") from e

    if config.hosts and not any(
        (h.password if h.password is not None else config.password)
        for h in config.hosts
    ):
        logger.warning(
            "CIMC hosts are configured but no CIMC password is set; "
            "set CIMC_PASSWORD or a per-host password in CIMC_HOSTS"
        )
    logger.info(
        "CIMC configuration loaded: %d hosts, writes %s",
        len(config.hosts),
        "enabled" if config.allow_writes else "disabled",
    )
    return config
