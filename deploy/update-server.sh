#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/vol1/1000/docker/companion-hub}"
REPOSITORY="${REPOSITORY:-git@github.com:PhyQxx/companion-hub.git}"
BRANCH="${1:-${DEPLOY_BRANCH:-main}}"
EXPORT_IMAGE="${EXPORT_IMAGE:-companion-hub-hub}"
LOCK_DIR="${APP_DIR}/.update-lock"
STATE_DIR="${APP_DIR}/.deploy-state"
COMPOSE_FILES=(-f "${APP_DIR}/docker-compose.yml" -f "${APP_DIR}/docker-compose.server.yml")

log() {
  printf '[update] %s\n' "$*"
}

fail() {
  printf '[update] ERROR: %s\n' "$*" >&2
  exit 1
}

for command in git rsync docker curl; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command: ${command}"
done

[[ -d "${APP_DIR}" ]] || fail "app directory does not exist: ${APP_DIR}"
[[ -f "${APP_DIR}/.env" ]] || fail "missing server environment file: ${APP_DIR}/.env"
[[ -f "${APP_DIR}/Dockerfile.server" ]] || fail "missing server Dockerfile"
[[ -f "${APP_DIR}/docker-compose.server.yml" ]] || fail "missing server Compose override"
docker image inspect "${EXPORT_IMAGE}" >/dev/null 2>&1 || fail "missing export image: ${EXPORT_IMAGE}"

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  fail "another update is already running (${LOCK_DIR})"
fi

WORK_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "${WORK_DIR}"
  rmdir "${LOCK_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

log "fetching ${REPOSITORY} branch ${BRANCH}"
git clone --quiet --depth 1 --single-branch --branch "${BRANCH}" "${REPOSITORY}" "${WORK_DIR}/source"
NEW_COMMIT="$(git -C "${WORK_DIR}/source" rev-parse HEAD)"

if [[ -f "${STATE_DIR}/commit" ]] && [[ "$(<"${STATE_DIR}/commit")" == "${NEW_COMMIT}" ]]; then
  log "already at ${NEW_COMMIT}; rebuilding to apply server configuration"
fi

log "syncing source at ${NEW_COMMIT}"
rsync -a --delete \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude '.env.*' \
  --exclude '.deploy-state/' \
  --exclude '.update-lock/' \
  --exclude 'deploy/update-server.sh' \
  --exclude 'Dockerfile.server' \
  --exclude 'docker-compose.server.yml' \
  --exclude 'requirements-server.txt' \
  "${WORK_DIR}/source/" "${APP_DIR}/"

log "exporting locked production dependencies"
docker run --rm --entrypoint uv \
  -v "${APP_DIR}:/src:ro" \
  "${EXPORT_IMAGE}" \
  export --project /src --frozen --no-dev --no-hashes --no-emit-project \
  --format requirements-txt > "${WORK_DIR}/requirements-server.txt"
install -m 0644 "${WORK_DIR}/requirements-server.txt" "${APP_DIR}/requirements-server.txt"

log "building application image"
BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}" \
  docker compose "${COMPOSE_FILES[@]}" --project-directory "${APP_DIR}" build hub

log "starting services"
docker compose "${COMPOSE_FILES[@]}" --project-directory "${APP_DIR}" up -d --remove-orphans

HUB_CONTAINER="$(docker compose "${COMPOSE_FILES[@]}" --project-directory "${APP_DIR}" ps -q hub)"
[[ -n "${HUB_CONTAINER}" ]] || fail "hub container was not created"

log "waiting for health check"
for _ in $(seq 1 60); do
  HEALTH="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${HUB_CONTAINER}")"
  if [[ "${HEALTH}" == 'healthy' ]]; then
    break
  fi
  if [[ "${HEALTH}" == 'unhealthy' || "${HEALTH}" == 'exited' || "${HEALTH}" == 'dead' ]]; then
    docker compose "${COMPOSE_FILES[@]}" --project-directory "${APP_DIR}" logs --tail=120 hub >&2
    fail "hub entered state: ${HEALTH}"
  fi
  sleep 2
done

HEALTH="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${HUB_CONTAINER}")"
[[ "${HEALTH}" == 'healthy' ]] || fail "hub did not become healthy (state: ${HEALTH})"
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8010/healthz >/dev/null

mkdir -p "${STATE_DIR}"
printf '%s\n' "${NEW_COMMIT}" > "${STATE_DIR}/commit"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "${STATE_DIR}/updated-at"

log "deployment complete: ${NEW_COMMIT}"
docker compose "${COMPOSE_FILES[@]}" --project-directory "${APP_DIR}" ps
