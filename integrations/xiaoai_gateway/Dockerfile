FROM node:22-alpine AS build
WORKDIR /app
RUN corepack enable
# 依赖清单先行 COPY：源码改动时 install 层命中缓存；--frozen-lockfile 保证构建可复现
COPY package.json pnpm-lock.yaml tsconfig.json ./
RUN pnpm install --frozen-lockfile
COPY src ./src
RUN pnpm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --prod
COPY --from=build /app/dist ./dist
CMD ["node", "dist/index.js"]
