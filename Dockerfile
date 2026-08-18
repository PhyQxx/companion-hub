FROM node:20-bookworm-slim AS web-build
WORKDIR /web
RUN corepack enable
COPY web ./
RUN pnpm install --no-frozen-lockfile && pnpm -r build

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY alembic.ini ./
COPY config ./config
COPY server ./server
COPY --from=web-build /web/apps/chat/dist ./web/apps/chat/dist
COPY --from=web-build /web/apps/admin/dist ./web/apps/admin/dist
COPY deploy/hub-entrypoint.sh /usr/local/bin/hub-entrypoint
RUN chmod +x /usr/local/bin/hub-entrypoint

EXPOSE 8000
ENTRYPOINT ["hub-entrypoint"]
