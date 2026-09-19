#!/bin/bash
# Aria Hub Postgres 每日逻辑备份：pg_dump 自定义格式 + 按天滚动清理（BK-01）。
# 由 docker-compose 的 backup 服务挂载运行（profile: backup），与数据库同镜像，
# 保证 pg_dump 与服务端版本一致。恢复步骤见 docs/07「数据备份与恢复（BK-01）」。
# 环境变量（均可在 .env 覆盖）：
#   ARIA_BACKUP_AT          每日备份时刻 HH:MM（默认 03:30，时区见 ARIA_BACKUP_TZ）
#   ARIA_BACKUP_KEEP_DAYS   保留天数（默认 14，按文件 mtime 滚动删除）
#   ARIA_BACKUP_RETRY_SECONDS  备份失败重试间隔（默认 600）
#   PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE  由 compose 注入
# 时区：容器默认 UTC；compose 注入 TZ（默认 Asia/Shanghai）保证 03:30 是本地凌晨。
set -u

AT="${ARIA_BACKUP_AT:-03:30}"
KEEP_DAYS="${ARIA_BACKUP_KEEP_DAYS:-14}"
RETRY_SECONDS="${ARIA_BACKUP_RETRY_SECONDS:-600}"
BACKUP_DIR="${ARIA_BACKUP_DIR:-/backups}"

at_minutes=$((10#${AT%%:*} * 60 + 10#${AT##*:}))
mkdir -p "$BACKUP_DIR"

log() { echo "[backup] $(date '+%F %T') $*"; }

last_run=""
while :; do
  today=$(date +%F)
  now_minutes=$((10#$(date +%H) * 60 + 10#$(date +%M)))
  # 容器启动晚于当日时刻也立即补跑一次，保证部署即有备份；之后每日一次
  if [ "$last_run" = "$today" ] || [ "$now_minutes" -lt "$at_minutes" ]; then
    sleep 60
    continue
  fi
  ts=$(date +%Y%m%d_%H%M%S)
  target="$BACKUP_DIR/aria_${ts}.dump"
  if pg_dump --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" \
      --dbname="$PGDATABASE" --format=custom --file="$target"; then
    log "wrote $target"
    last_run="$today"
    find "$BACKUP_DIR" -name 'aria_*.dump' -type f -mtime +"$KEEP_DAYS" -print -delete |
      while IFS= read -r removed; do log "pruned $removed"; done
  else
    log "pg_dump failed; retry in ${RETRY_SECONDS}s"
    sleep "$RETRY_SECONDS"
    continue
  fi
  sleep 60
done
