FROM node:22-bookworm-slim AS web-build
WORKDIR /web
RUN corepack enable
COPY web ./
RUN pnpm install --no-frozen-lockfile && pnpm -r build

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
# 国内网络下 pypi 官方源大包（rpds-py 等）易超时：放宽 HTTP 超时并走清华镜像
ENV UV_HTTP_TIMEOUT=120 \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev

COPY alembic.ini ./
COPY config ./config
COPY server ./server
COPY --from=web-build /web/apps/dist ./web/apps/dist
COPY deploy/hub-entrypoint.sh /usr/local/bin/hub-entrypoint
RUN chmod +x /usr/local/bin/hub-entrypoint

EXPOSE 8000
ENTRYPOINT ["hub-entrypoint"]
