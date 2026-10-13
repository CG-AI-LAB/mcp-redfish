# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

FROM python:3.14.6-slim@sha256:7bec7ddcddeff7975d6ba9b4be7dd6f6b2f55e7491539145e2978f7f97ce9144
ARG UV_VERSION=0.12.7
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

# Install dependencies and activate virtual environment
RUN uv sync --locked --no-dev

# Add virtual environment to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Default CMD is stdio-shaped (no MCP HTTP auth). If you override
# MCP_TRANSPORT to sse or streamable-http, set MCP_AUTH_MODE (and MCP_AUTH_*)
# or MCP_HTTP_AUTH=false. Non-loopback streamable-http also requires exact
# FASTMCP_HTTP_ALLOWED_HOSTS. See docs/MCP_AUTH.md and
# docs/MCP_HTTP_DEPLOYMENT.md.
CMD ["uv", "run", "python", "-m", "src.main"]
