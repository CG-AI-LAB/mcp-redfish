# Copyright 2026 CG-AI-LAB
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for CIMC configuration loading."""

import importlib
import os
import sys
import unittest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src"))
)

import src.common.config as config  # noqa: E402
from src.common.cimc_config import (  # noqa: E402
    CimcConfig,
    CimcHostConfig,
    load_cimc_config,
    parse_cimc_hosts,
)
from src.common.validation import ConfigurationError  # noqa: E402

from test.utils import MockEnvironment  # noqa: E402


class ParseHostsTestCase(unittest.TestCase):
    def test_parses_hosts_with_overrides(self) -> None:
        hosts = parse_cimc_hosts(
            '[{"address": "10.0.0.1"}, '
            '{"address": "10.0.0.2", "port": 8443, "username": "u", '
            '"password": "p", "tls_verify": false, "server_dn": "sys/chassis-1/server-1"}]'
        )
        self.assertEqual(len(hosts), 2)
        self.assertEqual(hosts[0].server_dn, "sys/rack-unit-1")
        self.assertEqual(hosts[1].port, 8443)
        self.assertFalse(hosts[1].tls_verify)
        self.assertEqual(hosts[1].server_dn, "sys/chassis-1/server-1")

    def test_rejects_invalid_json_and_shapes(self) -> None:
        for bad in ("not json", '{"address": "x"}', "[1]", '[{"port": 443}]'):
            with self.assertRaises(ConfigurationError):
                parse_cimc_hosts(bad)

    def test_rejects_bad_port_tls_and_dn(self) -> None:
        with self.assertRaises(ValueError):
            CimcHostConfig(address="a", port=70000)
        with self.assertRaises(ValueError):
            CimcHostConfig(address="a", tls_verify="yes")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            CimcHostConfig(address="a", server_dn='sys/rack-unit-1" x="y')

    def test_rejects_duplicate_addresses(self) -> None:
        with self.assertRaises(ValueError):
            CimcConfig(hosts=[CimcHostConfig("a"), CimcHostConfig("a")])


class LoadConfigTestCase(unittest.TestCase):
    def test_defaults_without_cimc_hosts(self) -> None:
        with MockEnvironment({"CIMC_HOSTS": "[]", "CIMC_ALLOW_WRITES": "false"}):
            cfg = load_cimc_config()
        self.assertEqual(cfg.hosts, [])
        self.assertFalse(cfg.allow_writes)
        self.assertTrue(cfg.tls_verify)
        self.assertEqual(cfg.timeout, 30.0)

    def test_global_defaults_apply_to_hosts(self) -> None:
        env = {
            "CIMC_HOSTS": '[{"address": "a"}, {"address": "b", "username": "bob", "password": "pb"}]',
            "CIMC_USERNAME": "admin",
            "CIMC_PASSWORD": "secret",
            "CIMC_PORT": "8443",
            "CIMC_TLS_VERIFY": "false",
            "CIMC_TIMEOUT": "12.5",
            "CIMC_ALLOW_WRITES": "true",
        }
        with MockEnvironment(env):
            cfg = load_cimc_config()
        a = cfg.get_host("a")
        b = cfg.get_host("b")
        assert a is not None and b is not None
        self.assertEqual(
            (a["username"], a["password"], a["port"]), ("admin", "secret", 8443)
        )
        self.assertEqual((b["username"], b["password"]), ("bob", "pb"))
        self.assertFalse(a["tls_verify"])
        self.assertEqual(a["timeout"], 12.5)
        self.assertTrue(cfg.allow_writes)
        self.assertIsNone(cfg.get_host("missing"))

    def test_invalid_values_raise_configuration_error(self) -> None:
        cases = [
            {"CIMC_HOSTS": "nope"},
            {"CIMC_HOSTS": "[]", "CIMC_ALLOW_WRITES": "maybe"},
            {"CIMC_HOSTS": "[]", "CIMC_TIMEOUT": "fast"},
            {"CIMC_HOSTS": "[]", "CIMC_TIMEOUT": "0"},
            {"CIMC_HOSTS": "[]", "CIMC_PORT": "0"},
        ]
        for env in cases:
            with self.subTest(env=env), MockEnvironment(env):
                with self.assertRaises(ConfigurationError):
                    load_cimc_config()

    def test_warns_when_hosts_have_no_password(self) -> None:
        env = {"CIMC_HOSTS": '[{"address": "a"}]', "CIMC_PASSWORD": ""}
        with MockEnvironment(env):
            with self.assertLogs("src.common.cimc_config", level="WARNING") as logs:
                load_cimc_config()
        self.assertTrue(any("password" in line for line in logs.output))


class ConfigModuleAbortsTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        importlib.reload(config)

    def test_invalid_cimc_hosts_aborts_startup(self) -> None:
        with MockEnvironment({"CIMC_HOSTS": "not-json"}):
            with self.assertLogs("src.common.validation", level="ERROR") as logs:
                with self.assertRaises(SystemExit) as ctx:
                    importlib.reload(config)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("CIMC_HOSTS", "\n".join(logs.output))

    def test_valid_environment_exposes_cimc_config(self) -> None:
        importlib.reload(config)
        self.assertEqual(
            [h.address for h in config.CIMC_CONFIG.hosts], ["cimc-test.example.com"]
        )
        self.assertTrue(config.CIMC_CONFIG.allow_writes)


if __name__ == "__main__":
    unittest.main()
