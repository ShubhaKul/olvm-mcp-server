# olvm-mcp over Streamable HTTP. Configuration comes from the environment and
# mounted files; see deploy/README.md. Nothing secret is baked into the image.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't rebuild them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
# Fixed ids: secrets on the host are shared with group 10001 (see deploy/README.md).
RUN groupadd --system --gid 10001 olvm \
 && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/olvm-mcp --create-home olvm
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    OLVM_TRANSPORT=http \
    OLVM_HTTP_HOST=0.0.0.0 \
    OLVM_HTTP_PORT=8000 \
    OLVM_AUDIT_LOG=/var/lib/olvm-mcp/audit.jsonl
USER olvm
VOLUME ["/var/lib/olvm-mcp"]
EXPOSE 8000
ENTRYPOINT ["olvm-mcp"]
