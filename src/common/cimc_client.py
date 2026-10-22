# Copyright 2026 CG-AI-LAB
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""
Client for the Cisco CIMC XML API.

Standalone UCS C-Series servers accept XML API requests as HTTP POSTs to
``https://<cimc>/nuova``. Every request except ``aaaLogin`` carries a session
cookie. Sessions are cached per host for the life of the process and refreshed
before they expire, because a CIMC allows only a handful of concurrent sessions
and logging in on every tool call would exhaust them.

Requests to one host are serialized: the BMC is a small embedded system and
concurrent XML API requests are a reliable way to make it return errors.
"""

from __future__ import annotations

import logging
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any

# Element is only used for type annotations; parsing goes through defusedxml.
from xml.etree.ElementTree import Element  # nosec B405

import certifi
import httpx2
from defusedxml import ElementTree as SafeElementTree
from fastmcp.exceptions import ToolError, ValidationError
from tenacity import after_log, before_sleep_log, retry

from .cimc_config import is_valid_dn
from .client import get_retry_configuration

logger = logging.getLogger(__name__)

# Refresh the session this many seconds before the CIMC would expire it.
SESSION_REFRESH_MARGIN_SECONDS = 100
DEFAULT_REFRESH_PERIOD_SECONDS = 600

# Error codes the CIMC returns when the session cookie is no longer valid.
_SESSION_EXPIRED_CODES = {"552", "555"}

_XML_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&apos;",
}


def escape_xml(value: str) -> str:
    """Escape a string for use inside a double-quoted XML attribute value."""
    return "".join(_XML_ESCAPES.get(ch, ch) for ch in str(value))


def is_valid_xml_name(value: str) -> bool:
    """Return True for a CIMC class or attribute name (letters, digits, '_')."""
    return (
        bool(value)
        and len(value) <= 128
        and value[0].isalpha()
        and all(ch.isalnum() or ch == "_" for ch in value)
    )


class CimcError(ToolError):
    """Raised when the CIMC answers with an XML API error element."""

    def __init__(self, error_code: str, description: str, operation: str):
        self.error_code = error_code
        self.description = description
        self.operation = operation
        super().__init__(f"CIMC {operation} failed ({error_code}): {description}")


@dataclass
class _Session:
    cookie: str
    expires_at: float
    refresh_period: int


# Per-host state. The registry lock guards the dictionaries; each host lock
# serializes XML API traffic to that host.
_registry_lock = threading.Lock()
_sessions: dict[str, _Session] = {}
_host_locks: dict[str, threading.RLock] = {}


def _host_key(host_cfg: dict[str, Any]) -> str:
    return f"{host_cfg.get('address')}:{host_cfg.get('port') or 443}"


def _host_lock(key: str) -> threading.RLock:
    with _registry_lock:
        lock = _host_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _host_locks[key] = lock
        return lock


def reset_sessions() -> None:
    """Forget every cached session. Intended for tests."""
    with _registry_lock:
        _sessions.clear()


def _should_retry_cimc(retry_state) -> bool:
    """Retry transport failures only; CIMC application errors are final."""
    outcome = getattr(retry_state, "outcome", None)
    if outcome is None or outcome.exception() is None:
        return False
    exception = outcome.exception()
    if isinstance(exception, ValidationError | CimcError):
        return False
    if isinstance(exception, httpx2.TransportError):
        return True
    cause = getattr(exception, "__cause__", None)
    return isinstance(cause, httpx2.TransportError)


def _read_retry_configuration() -> dict[str, Any]:
    configuration: dict[str, Any] = get_retry_configuration()
    configuration["retry"] = _should_retry_cimc
    return configuration


def build_http_client(host_cfg: dict[str, Any]) -> httpx2.Client:
    """Create the HTTP client for one CIMC. Tests replace this function."""
    tls_verify = host_cfg.get("tls_verify", True)
    verify: bool | ssl.SSLContext
    if tls_verify:
        verify = ssl.create_default_context(
            cafile=host_cfg.get("tls_server_ca_cert") or certifi.where()
        )
    else:
        logger.warning(
            "TLS certificate verification is disabled for CIMC %s",
            host_cfg.get("address"),
        )
        verify = False
    timeout = float(host_cfg.get("timeout") or 30.0)
    return httpx2.Client(
        verify=verify,
        timeout=httpx2.Timeout(timeout),
        headers={"Content-Type": "text/xml"},
        trust_env=True,
    )


def mo_to_dict(element: Element, include_children: bool = False) -> dict[str, Any]:
    """Convert a managed-object element to a flat dict of its attributes.

    The element tag (the CIMC class name) is stored under ``class``. With
    ``include_children`` nested managed objects are converted recursively under
    ``children``.
    """
    result: dict[str, Any] = {"class": element.tag}
    result.update(element.attrib)
    if include_children:
        children = [mo_to_dict(child, True) for child in element]
        if children:
            result["children"] = children
    return result


def _parse_response(body: str, operation: str) -> Element:
    root: Element
    try:
        root = SafeElementTree.fromstring(body)
    except Exception as e:  # defusedxml raises several ParseError subclasses
        raise ToolError(
            f"CIMC {operation} returned a response that is not valid XML"
        ) from e
    error_code = root.attrib.get("errorCode")
    if error_code and error_code != "0":
        raise CimcError(
            error_code, root.attrib.get("errorDescr", "Unknown error"), operation
        )
    if root.tag != operation:
        raise ToolError(f"CIMC returned <{root.tag}> where <{operation}> was expected")
    return root


def _out_configs(root: Element) -> list[Element]:
    """Return the managed-object elements of outConfig(s), if any."""
    elements: list[Element] = []
    for container_tag in ("outConfigs", "outConfig"):
        container = root.find(container_tag)
        if container is not None:
            elements.extend(list(container))
    return elements


class CimcClient:
    """XML API client bound to one configured CIMC host."""

    def __init__(self, host_cfg: dict[str, Any]) -> None:
        self.host_cfg = host_cfg
        self.address = host_cfg.get("address")
        if not self.address:
            raise ValidationError("CIMC host configuration is missing an address")
        self.server_dn = host_cfg.get("server_dn") or "sys/rack-unit-1"
        self.url = f"https://{self.address}:{host_cfg.get('port') or 443}/nuova"
        self._key = _host_key(host_cfg)
        self._lock = _host_lock(self._key)
        self._http: httpx2.Client | None = None

    # -- transport -----------------------------------------------------------

    def _http_client(self) -> httpx2.Client:
        if self._http is None:
            self._http = build_http_client(self.host_cfg)
        return self._http

    def close(self) -> None:
        """Release the HTTP connection. The CIMC session stays cached."""
        if self._http is not None:
            try:
                self._http.close()
            finally:
                self._http = None

    def __enter__(self) -> CimcClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _post(self, xml_body: str, operation: str) -> Element:
        try:
            response = self._http_client().post(self.url, content=xml_body)
        except httpx2.TransportError as e:
            logger.warning(
                "CIMC %s to %s failed: error_type=%s",
                operation,
                self.address,
                type(e).__name__,
            )
            raise ToolError(
                f"CIMC {operation} to {self.address} failed: {type(e).__name__}"
            ) from e
        if response.status_code >= 400:
            raise ToolError(
                f"CIMC {operation} to {self.address} returned HTTP {response.status_code}"
            )
        return _parse_response(response.text, operation)

    # -- session -------------------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        username = self.host_cfg.get("username") or ""
        password = self.host_cfg.get("password") or ""
        if not username or not password:
            raise ValidationError(
                f"CIMC {self.address} has no username/password configured; "
                "set CIMC_USERNAME/CIMC_PASSWORD or per-host credentials in CIMC_HOSTS"
            )
        return username, password

    def _store_session(self, root: Element, previous: _Session | None) -> _Session:
        cookie = root.attrib.get("outCookie") or (previous.cookie if previous else "")
        if not cookie:
            raise ToolError(f"CIMC {self.address} login response had no outCookie")
        try:
            refresh_period = int(
                root.attrib.get("outRefreshPeriod")
                or (previous.refresh_period if previous else 0)
                or DEFAULT_REFRESH_PERIOD_SECONDS
            )
        except ValueError:
            refresh_period = DEFAULT_REFRESH_PERIOD_SECONDS
        session = _Session(
            cookie=cookie,
            expires_at=time.monotonic() + refresh_period,
            refresh_period=refresh_period,
        )
        with _registry_lock:
            _sessions[self._key] = session
        return session

    def _login(self) -> _Session:
        username, password = self._credentials()
        logger.info("Logging in to CIMC %s", self.address)
        xml = (
            f'<aaaLogin inName="{escape_xml(username)}" '
            f'inPassword="{escape_xml(password)}"/>'
        )
        root = self._post(xml, "aaaLogin")
        return self._store_session(root, None)

    def _refresh(self, session: _Session) -> _Session:
        username, password = self._credentials()
        xml = (
            f'<aaaRefresh cookie="{escape_xml(session.cookie)}" '
            f'inCookie="{escape_xml(session.cookie)}" '
            f'inName="{escape_xml(username)}" inPassword="{escape_xml(password)}"/>'
        )
        try:
            root = self._post(xml, "aaaRefresh")
        except CimcError:
            logger.info(
                "CIMC %s session refresh rejected; logging in again", self.address
            )
            self._forget_session()
            return self._login()
        return self._store_session(root, session)

    def _forget_session(self) -> None:
        with _registry_lock:
            _sessions.pop(self._key, None)

    def _ensure_session(self) -> _Session:
        with _registry_lock:
            session = _sessions.get(self._key)
        now = time.monotonic()
        if session is None or now >= session.expires_at:
            return self._login()
        if now >= session.expires_at - SESSION_REFRESH_MARGIN_SECONDS:
            return self._refresh(session)
        return session

    def logout(self) -> None:
        """End the cached CIMC session, if there is one."""
        with self._lock:
            with _registry_lock:
                session = _sessions.get(self._key)
            if session is None:
                return
            xml = (
                f'<aaaLogout cookie="{escape_xml(session.cookie)}" '
                f'inCookie="{escape_xml(session.cookie)}"/>'
            )
            try:
                self._post(xml, "aaaLogout")
            except ToolError as e:
                logger.debug(
                    "CIMC %s logout ignored: %s", self.address, type(e).__name__
                )
            finally:
                self._forget_session()

    # -- requests ------------------------------------------------------------

    def _request(self, xml_template: str, operation: str) -> Element:
        """Send a cookie-bearing request, re-authenticating once on expiry."""
        with self._lock:
            session = self._ensure_session()
            try:
                return self._post(
                    xml_template.replace("{cookie}", escape_xml(session.cookie)),
                    operation,
                )
            except CimcError as e:
                if e.error_code not in _SESSION_EXPIRED_CODES:
                    raise
                logger.info("CIMC %s session expired; logging in again", self.address)
                self._forget_session()
                session = self._login()
                return self._post(
                    xml_template.replace("{cookie}", escape_xml(session.cookie)),
                    operation,
                )

    @staticmethod
    def _require_dn(dn: str) -> str:
        if not is_valid_dn(dn):
            raise ValidationError(f"Invalid CIMC DN: {dn!r}")
        return dn

    @staticmethod
    def _require_name(value: str, what: str) -> str:
        if not is_valid_xml_name(value):
            raise ValidationError(f"Invalid CIMC {what}: {value!r}")
        return value

    @retry(
        **_read_retry_configuration(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
    )
    def resolve_dn(self, dn: str, hierarchical: bool = False) -> dict[str, Any] | None:
        """Return the managed object at ``dn`` or None when it does not exist."""
        self._require_dn(dn)
        xml = (
            f'<configResolveDn cookie="{{cookie}}" dn="{escape_xml(dn)}" '
            f'inHierarchical="{"true" if hierarchical else "false"}"/>'
        )
        root = self._request(xml, "configResolveDn")
        elements = _out_configs(root)
        if not elements:
            return None
        return mo_to_dict(elements[0], include_children=hierarchical)

    @retry(
        **_read_retry_configuration(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
    )
    def resolve_class(
        self, class_id: str, hierarchical: bool = False
    ) -> list[dict[str, Any]]:
        """Return every managed object of class ``class_id``."""
        self._require_name(class_id, "class name")
        xml = (
            f'<configResolveClass cookie="{{cookie}}" classId="{class_id}" '
            f'inHierarchical="{"true" if hierarchical else "false"}"/>'
        )
        root = self._request(xml, "configResolveClass")
        return [
            mo_to_dict(el, include_children=hierarchical) for el in _out_configs(root)
        ]

    @retry(
        **_read_retry_configuration(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
    )
    def resolve_children(
        self, dn: str, class_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the direct children of ``dn``, optionally filtered by class."""
        self._require_dn(dn)
        class_attr = ""
        if class_id:
            self._require_name(class_id, "class name")
            class_attr = f' classId="{class_id}"'
        xml = (
            f'<configResolveChildren cookie="{{cookie}}" inDn="{escape_xml(dn)}"'
            f'{class_attr} inHierarchical="false"/>'
        )
        root = self._request(xml, "configResolveChildren")
        return [mo_to_dict(el) for el in _out_configs(root)]

    def config_mo(
        self, dn: str, class_name: str, attributes: dict[str, str]
    ) -> dict[str, Any] | None:
        """Modify managed object ``dn``. Mutations are never retried."""
        self._require_dn(dn)
        self._require_name(class_name, "class name")
        if not attributes:
            raise ValidationError("config_mo requires at least one attribute")
        attr_xml = "".join(
            f' {self._require_name(name, "attribute name")}="{escape_xml(value)}"'
            for name, value in attributes.items()
        )
        xml = (
            f'<configConfMo cookie="{{cookie}}" dn="{escape_xml(dn)}" '
            f'inHierarchical="false"><inConfig>'
            f'<{class_name} dn="{escape_xml(dn)}"{attr_xml}/>'
            f"</inConfig></configConfMo>"
        )
        root = self._request(xml, "configConfMo")
        elements = _out_configs(root)
        return mo_to_dict(elements[0]) if elements else None


__all__ = [
    "CimcClient",
    "CimcError",
    "build_http_client",
    "escape_xml",
    "is_valid_xml_name",
    "mo_to_dict",
    "reset_sessions",
]
