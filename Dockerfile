FROM node:22-bookworm-slim AS web-build
WORKDIR /web
RUN corepack enable
# 依赖清单先行 COPY：web 源码改动时 install 层命中缓存；--frozen-lockfile 保证构建可复现
COPY web/pnpm-workspace.yaml web/pnpm-lock.yaml ./
COPY web/package.json ./
COPY web/apps/package.json ./apps/
COPY web/packages/shared/package.json ./packages/shared/
RUN pnpm install --frozen-lockfile
COPY web/apps ./apps
COPY web/packages ./packages
RUN pnpm -r build

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

# Stamp after source/static assets so every changed image has a distinct identity.
ARG BUILD_REVISION=""
COPY deploy/write-build-info.py /tmp/write-build-info.py
RUN BUILD_REVISION="$BUILD_REVISION" /app/.venv/bin/python /tmp/write-build-info.py

EXPOSE 8000
ENTRYPOINT ["hub-entrypoint"]
