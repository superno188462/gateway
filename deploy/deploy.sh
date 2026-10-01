#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ $# -gt 0 ]]; then
  echo "用法：$0" >&2
  exit 2
fi

if [[ ! -f .env ]]; then
  echo "缺少 .env。请先 cp .env.server.example .env 并填写数据库地址和强密钥。" >&2
  exit 1
fi

if ! docker network inspect gateway-proxy >/dev/null 2>&1; then
  docker network create gateway-proxy >/dev/null
fi

docker compose config >/dev/null
docker compose build --pull
docker compose run --rm api uv run alembic upgrade head
docker compose up -d --remove-orphans
docker compose ps
