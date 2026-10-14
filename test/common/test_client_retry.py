# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""
Tests for Redfish client retry logic.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import certifi

# Patch sys.path to import from src
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src"))
)

from fastmcp.exceptions import ToolError, ValidationError
from tenacity import RetryError

from src.common.client import (
    RedfishClient,
    RedfishHTTPStatusError,
    get_retry_configuration,
)


class TestRedfishClientRetry(unittest.TestCase):
    """Test Redfish client retry logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.server_cfg = {
            "address": "test-server.example.com",
            "username": "testuser",
            "password": "testpass",
            "port": 443,
        }

        # Mock common config
        self.common_cfg = MagicMock()
        self.common_cfg.REDFISH_CFG = {
            "auth_method": "session",
            "port": 443,
            "username": "default_user",
            "password": "default_pass",
            "tls_verify": True,
            "tls_server_ca_cert": None,
        }

    @patch.dict(
        os.environ,
        {
            "REDFISH_MAX_RETRIES": "2",
            "REDFISH_INITIAL_DELAY": "0.01",
        },
    )
    @patch("redfish.redfish_client")
    def test_client_setup_with_retry_success(self, mock_redfish_client):
        """Test successful client setup after retry."""
        # First attempt fails, second succeeds
        mock_client = MagicMock()
        mock_redfish_client.side_effect = [
            ConnectionError("Network error"),
            mock_client,
        ]

        client = RedfishClient(self.server_cfg, self.common_cfg)

        # Should have called redfish_client twice (tenacity handles the retry)
        self.assertEqual(mock_redfish_client.call_count, 2)
        self.assertEqual(client.client, mock_client)

    @patch("redfish.redfish_client")
    def test_client_setup_verifies_tls_with_default_ca_bundle(
        self, mock_redfish_client
    ):
        """Test TLS verification uses a default CA bundle unless opted out."""
        mock_client = MagicMock()
        mock_redfish_client.return_value = mock_client

        RedfishClient(self.server_cfg, self.common_cfg)

        mock_redfish_client.assert_called_once_with(
            base_url="https://test-server.example.com:443",
            username="testuser",
            password="testpass",
            default_prefix="/redfish/v1",
            cafile=certifi.where(),
        )

    @patch("redfish.redfish_client")
    def test_client_setup_uses_configured_ca_certificate(self, mock_redfish_client):
        """Test TLS verification uses the configured CA certificate."""
        mock_client = MagicMock()
        mock_redfish_client.return_value = mock_client
        self.server_cfg["tls_server_ca_cert"] = "/path/to/ca-cert.pem"

        RedfishClient(self.server_cfg, self.common_cfg)

        mock_redfish_client.assert_called_once_with(
            base_url="https://test-server.example.com:443",
            username="testuser",
            password="testpass",
            default_prefix="/redfish/v1",
            cafile="/path/to/ca-cert.pem",
        )

    @patch("redfish.redfish_client")
    def test_client_setup_allows_explicit_tls_verification_opt_out(
        self, mock_redfish_client
    ):
        """Test explicit TLS verification opt-out leaves cafile unset."""
        mock_client = MagicMock()
        mock_redfish_client.return_value = mock_client
        self.server_cfg["tls_verify"] = False

        with self.assertLogs("src.common.client", level="WARNING") as log_context:
            RedfishClient(self.server_cfg, self.common_cfg)

        mock_redfish_client.assert_called_once_with(
            base_url="https://test-server.example.com:443",
            username="testuser",
            password="testpass",
            default_prefix="/redfish/v1",
            cafile=None,
        )
        self.assertIn(
            "TLS certificate verification is disabled", " ".join(log_context.output)
        )

    @patch("redfish.redfish_client")
    def test_client_setup_supports_legacy_verify_cert_opt_out(
        self, mock_redfish_client
    ):
        """Test legacy verify_cert=False still opts out of TLS verification."""
        mock_client = MagicMock()
        mock_redfish_client.return_value = mock_client
        self.server_cfg["verify_cert"] = False
        del self.common_cfg.REDFISH_CFG["tls_verify"]

        with self.assertLogs("src.common.client", level="WARNING"):
            RedfishClient(self.server_cfg, self.common_cfg)

        mock_redfish_client.assert_called_once_with(
            base_url="https://test-server.example.com:443",
            username="testuser",
            password="testpass",
            default_prefix="/redfish/v1",
            cafile=None,
        )

    @patch("redfish.redfish_client")
    def test_client_setup_rejects_invalid_tls_verify(self, mock_redfish_client):
        """Test raw client config rejects non-boolean tls_verify values."""
        self.server_cfg["tls_verify"] = "false"

        with self.assertRaises(ValidationError):
            RedfishClient(self.server_cfg, self.common_cfg)

        mock_redfish_client.assert_not_called()

    @patch.dict(
        os.environ, {"REDFISH_MAX_RETRIES": "1", "REDFISH_INITIAL_DELAY": "0.01"}
    )
    @patch("redfish.redfish_client")
    def test_client_setup_retry_exhausted(self, mock_redfish_client):
        """Test client setup failure after retries exhausted."""
        mock_redfish_client.side_effect = ConnectionError("Persistent network error")

        # Should retry the configured number of times then fail with RetryError
        with self.assertRaises(RetryError):
            RedfishClient(self.server_cfg, self.common_cfg)

        # Should have been called multiple times (retries are working)
        self.assertGreater(mock_redfish_client.call_count, 1)

    @patch.dict(
        os.environ, {"REDFISH_MAX_RETRIES": "2", "REDFISH_INITIAL_DELAY": "0.01"}
    )
    @patch("redfish.redfish_client")
    def test_get_operation_with_retry(self, mock_redfish_client):
        """Test GET operation with retry logic."""
        # Setup mock client
        mock_client = MagicMock()
        mock_redfish_client.return_value = mock_client

        # First GET fails, second succeeds
        mock_response = MagicMock()
        mock_response.dict = {"test": "data"}
        mock_client.get.side_effect = [ConnectionError("Timeout"), mock_response]

        client = RedfishClient(self.server_cfg, self.common_cfg)
        result = client.get("/redfish/v1/Systems")

        # Should have called get twice (tenacity handles retry)
        self.assertEqual(mock_client.get.call_count, 2)
        self.assertEqual(result, {"test": "data"})

    @patch.dict(
        os.environ, {"REDFISH_MAX_RETRIES": "2", "REDFISH_INITIAL_DELAY": "0.01"}
    )
    @patch("redfish.redfish_client")
    def test_get_with_headers_operation_with_retry(self, mock_redfish_client):
        """Test get_with_headers operation with retry logic."""
        # Setup mock client
        mock_client = MagicMock()
        mock_redfish_client.return_value = mock_client

        # First GET fails, second succeeds
        mock_response = MagicMock()
        mock_response.dict = {"test": "data"}
        mock_response.getheaders.return_value = [("ETag", "abc123")]
        mock_client.get.side_effect = [ConnectionError("Timeout"), mock_response]

        client = RedfishClient(self.server_cfg, self.common_cfg)
        result = client.get_with_headers("/redfish/v1/Systems")

        # Should have called get twice (tenacity handles retry)
        self.assertEqual(mock_client.get.call_count, 2)
        self.assertEqual(result["data"], {"test": "data"})
        self.assertEqual(result["headers"], {"ETag": "abc123"})

    @patch.dict(
        os.environ, {"REDFISH_MAX_RETRIES": "1", "REDFISH_INITIAL_DELAY": "0.01"}
    )
    @patch("redfish.redfish_client")
    def test_post_operation_with_retry(self, mock_redfish_client):
        """Test POST operation with retry logic."""
        # Setup mock client
        mock_client = MagicMock()
        mock_redfish_client.return_value = mock_client

        # First POST fails, second succeeds
        mock_response = MagicMock()
        mock_response.dict = {"created": "resource"}
        mock_client.post.side_effect = [OSError("Connection reset"), mock_response]

        client = RedfishClient(self.server_cfg, self.common_cfg)
        result = client.post("/redfish/v1/Systems", {"test": "data"})

        # Should have called post twice
        self.assertEqual(mock_client.post.call_count, 2)
        self.assertEqual(result, {"created": "resource"})

    @patch.dict(
        os.environ, {"REDFISH_MAX_RETRIES": "2", "REDFISH_INITIAL_DELAY": "0.01"}
    )
    @patch("redfish.redfish_client")
    def test_retry_configuration_from_env(self, mock_redfish_client):
        """Test that retry configuration uses environment variables."""
        mock_client = MagicMock()
        mock_redfish_client.return_value = mock_client

        # Force failures - using cycle to avoid StopIteration
        mock_client.get.side_effect = TimeoutError("Timeout error")

        client = RedfishClient(self.server_cfg, self.common_cfg)

        with self.assertRaises(RetryError):
            client.get("/redfish/v1/Systems")

        # Should have attempted multiple times (retries working)
        self.assertGreater(mock_client.get.call_count, 1)

    @patch("redfish.redfish_client")
    def test_retry_with_validation_error(self, mock_redfish_client):
        """Test that ValidationError is not retried."""
        # Setup mock client
        mock_client = MagicMock()
        mock_redfish_client.return_value = mock_client

        # Use invalid auth method to trigger ValidationError
        self.common_cfg.REDFISH_CFG["auth_method"] = "invalid_method"

        # ValidationError should not be retried - should raise immediately
        with self.assertRaises(ValidationError):
            RedfishClient(self.server_cfg, self.common_cfg)

        # Should not retry ValidationError - not even called due to validation
        self.assertEqual(
            mock_redfish_client.call_count, 0
        )  # Not even called due to validation

    @patch("redfish.redfish_client")
    def test_retry_logging_integration(self, mock_redfish_client):
        """Test that retry integrates properly with logging."""
        with patch.dict(
            os.environ, {"REDFISH_MAX_RETRIES": "1", "REDFISH_INITIAL_DELAY": "0.01"}
        ):
            # Setup mock client
            mock_client = MagicMock()
            mock_redfish_client.side_effect = [
                ConnectionError("Network error"),
                mock_client,
            ]

            with self.assertLogs(level="WARNING") as log_context:
                _client = RedfishClient(self.server_cfg, self.common_cfg)

            # Check that retry logged the retry attempt
            log_messages = " ".join(log_context.output)
            self.assertIn("Network error", log_messages)

    def test_retry_configuration_backoff_and_jitter(self):
        """Test REDFISH_BACKOFF_FACTOR and REDFISH_JITTER configuration."""
        with patch.dict(
            os.environ,
            {
                "REDFISH_MAX_RETRIES": "2",
                "REDFISH_INITIAL_DELAY": "0.5",
                "REDFISH_MAX_DELAY": "10.0",
                "REDFISH_BACKOFF_FACTOR": "3.0",
                "REDFISH_JITTER": "false",
            },
        ):
            config = get_retry_configuration()

            # Verify configuration structure
            self.assertIn("stop", config)
            self.assertIn("wait", config)
            self.assertIn("retry", config)

            # Test that config can be used to create a working retry decorator
            from tenacity import retry

            @retry(**config)
            def test_function():
                return "success"

            result = test_function()
            self.assertEqual(result, "success")

    def test_retry_configuration_with_jitter_enabled(self):
        """Test REDFISH_JITTER=true uses random exponential wait."""
        with patch.dict(
            os.environ, {"REDFISH_JITTER": "true", "REDFISH_BACKOFF_FACTOR": "2.5"}
        ):
            config = get_retry_configuration()

            # The configuration should be valid and contain wait strategy
            self.assertIn("wait", config)

            # Test that config works with retry decorator
            from tenacity import retry

            @retry(**config)
            def test_jitter_function():
                return "jitter_success"

            result = test_jitter_function()
            self.assertEqual(result, "jitter_success")


class FakeRestResponse:
    """Minimal stand-in for a redfish RestResponse."""

    def __init__(self, data, status=200, headers=None):
        self.dict = data
        self.status = status
        self._headers = headers or []

    def getheaders(self):
        return self._headers


class TestRedfishClientHTTPStatusHandling(unittest.TestCase):
    """Test HTTP error status handling: raise for >=400, retry only the
    documented transient statuses (408, 429, 502, 503, 504), preserve the
    Redfish error body, and never status-retry mutating operations."""

    def setUp(self):
        """Set up test fixtures."""
        self.server_cfg = {
            "address": "test-server.example.com",
            "username": "testuser",
            "password": "testpass",
            "port": 443,
        }
        self.common_cfg = MagicMock()
        self.common_cfg.REDFISH_CFG = {
            "auth_method": "session",
            "port": 443,
            "username": "default_user",
            "password": "default_pass",
            "tls_verify": True,
            "tls_server_ca_cert": None,
        }
        self.mock_client = MagicMock()
        with patch("redfish.redfish_client", return_value=self.mock_client):
            self.client = RedfishClient(self.server_cfg, self.common_cfg)

    @patch.dict(
        os.environ,
        {
            "REDFISH_MAX_RETRIES": "2",
            "REDFISH_INITIAL_DELAY": "0.01",
        },
    )
    def test_get_retries_on_429_and_returns_data(self):
        """A 429 response is retried, and a later success returns data."""
        error_response = FakeRestResponse({"error": "Too Many Requests"}, status=429)
        ok_response = FakeRestResponse({"Id": "1"}, status=200)
        self.client.client.get.side_effect = [error_response, ok_response]

        result = self.client.get("/redfish/v1/Systems/1")

        self.assertEqual(result, {"Id": "1"})
        self.assertEqual(self.client.client.get.call_count, 2)

    @patch.dict(
        os.environ,
        {
            "REDFISH_MAX_RETRIES": "2",
            "REDFISH_INITIAL_DELAY": "0.01",
        },
    )
    def test_get_retries_on_503_and_returns_data(self):
        """A 503 response is retried, and a later success returns data."""
        error_response = FakeRestResponse({"error": "Service Unavailable"}, status=503)
        ok_response = FakeRestResponse({"Id": "2"}, status=200)
        self.client.client.get.side_effect = [error_response, ok_response]

        result = self.client.get("/redfish/v1/Systems/2")

        self.assertEqual(result, {"Id": "2"})
        self.assertEqual(self.client.client.get.call_count, 2)

    @patch.dict(
        os.environ,
        {
            "REDFISH_MAX_RETRIES": "2",
            "REDFISH_INITIAL_DELAY": "0.01",
        },
    )
    def test_get_raises_on_404_after_one_attempt_preserving_body(self):
        """A 404 GET fails after one attempt, preserving the Redfish body."""
        error_response = FakeRestResponse(
            {
                "error": {
                    "code": "iDRAC.1.5.ResourceNotFound",
                    "message": "The resource could not be found",
                    "MessageId": "iDRAC.1.5.ResourceNotFound",
                    "Resolution": "Verify the resource URI",
                }
            },
            status=404,
        )
        ok_response = FakeRestResponse({"Id": "3"}, status=200)
        self.client.client.get.side_effect = [error_response, ok_response]

        with self.assertRaises(RedfishHTTPStatusError) as ctx:
            self.client.get("/redfish/v1/Systems/3")

        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("404", str(ctx.exception))
        self.assertIn("ResourceNotFound", str(ctx.exception))
        self.assertIn("Verify the resource URI", str(ctx.exception))
        self.assertEqual(
            ctx.exception.body["error"]["MessageId"], "iDRAC.1.5.ResourceNotFound"
        )
        self.assertEqual(self.client.client.get.call_count, 1)

    @patch.dict(
        os.environ,
        {
            "REDFISH_MAX_RETRIES": "2",
            "REDFISH_INITIAL_DELAY": "0.01",
        },
    )
    def test_get_with_headers_retries_on_429_and_returns_data(self):
        """A 429 response is retried in get_with_headers too."""
        error_response = FakeRestResponse({"error": "Too Many Requests"}, status=429)
        ok_response = FakeRestResponse({"Id": "4"}, status=200)
        self.client.client.get.side_effect = [error_response, ok_response]

        result = self.client.get_with_headers("/redfish/v1/Systems/4")

        self.assertEqual(result["data"], {"Id": "4"})
        self.assertEqual(self.client.client.get.call_count, 2)

    @patch.dict(
        os.environ,
        {
            "REDFISH_MAX_RETRIES": "2",
            "REDFISH_INITIAL_DELAY": "0.01",
        },
    )
    def test_repeated_503_gets_expose_final_body_not_retryerror(self):
        """Exhausted 503 GETs re-raise the final RedfishHTTPStatusError with its
        Redfish body instead of a generic RetryError."""
        error_body = {
            "error": {
                "code": "iDRAC.1.5.ServiceUnavailable",
                "message": "Service temporarily unavailable",
                "MessageId": "iDRAC.1.5.ServiceUnavailable",
                "Resolution": "Retry the operation later",
            },
            "@Message.ExtendedInfo": [{"MessageId": "iDRAC.1.5.ServiceUnavailable"}],
        }
        self.client.client.get.side_effect = lambda path: FakeRestResponse(
            error_body, status=503
        )

        with self.assertRaises(RedfishHTTPStatusError) as ctx:
            self.client.get("/redfish/v1/Systems/5")

        self.assertEqual(ctx.exception.status, 503)
        self.assertIn("503", str(ctx.exception))
        self.assertIn("Service temporarily unavailable", str(ctx.exception))
        self.assertIn("Retry the operation later", str(ctx.exception))
        self.assertIn("@Message.ExtendedInfo", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, RetryError)
        self.assertGreaterEqual(self.client.client.get.call_count, 3)

    @patch.dict(
        os.environ,
        {
            "REDFISH_MAX_RETRIES": "2",
            "REDFISH_INITIAL_DELAY": "0.01",
        },
    )
    def test_post_does_not_retry_503(self):
        """A 503 POST is reported as a failed ToolError, never retried."""
        error_response = FakeRestResponse(
            {
                "error": {
                    "code": "iDRAC.1.5.ServiceUnavailable",
                    "message": "Service temporarily unavailable",
                    "MessageId": "iDRAC.1.5.ServiceUnavailable",
                }
            },
            status=503,
        )
        self.client.client.post.side_effect = [error_response]

        with self.assertRaises(ToolError) as ctx:
            self.client.post(
                "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                data={"ResetType": "ForceRestart"},
            )

        self.assertIn("503", str(ctx.exception))
        self.assertIn("Service temporarily unavailable", str(ctx.exception))
        self.assertEqual(self.client.client.post.call_count, 1)

    @patch.dict(
        os.environ,
        {
            "REDFISH_MAX_RETRIES": "2",
            "REDFISH_INITIAL_DELAY": "0.01",
        },
    )
    def test_patch_does_not_retry_503(self):
        """A 503 PATCH is reported as a failed ToolError, never retried."""
        error_response = FakeRestResponse(
            {"error": {"message": "Service temporarily unavailable"}},
            status=503,
        )
        self.client.client.patch.side_effect = [error_response]

        with self.assertRaises(ToolError) as ctx:
            self.client.patch("/redfish/v1/Systems/1", data={"Name": "new"})

        self.assertIn("503", str(ctx.exception))
        self.assertEqual(self.client.client.patch.call_count, 1)

    @patch.dict(
        os.environ,
        {
            "REDFISH_MAX_RETRIES": "2",
            "REDFISH_INITIAL_DELAY": "0.01",
        },
    )
    def test_delete_does_not_retry_503(self):
        """A 503 DELETE is reported as a failed ToolError, never retried."""
        error_response = FakeRestResponse(
            {"error": {"message": "Service temporarily unavailable"}},
            status=503,
        )
        self.client.client.delete.side_effect = [error_response]

        with self.assertRaises(ToolError) as ctx:
            self.client.delete("/redfish/v1/Systems/1")

        self.assertIn("503", str(ctx.exception))
        self.assertEqual(self.client.client.delete.call_count, 1)


if __name__ == "__main__":
    unittest.main()
