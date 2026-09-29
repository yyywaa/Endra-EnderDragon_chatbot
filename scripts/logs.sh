#!/usr/bin/env bash
# 看日志的便捷入口：不管当前在哪个目录都能用。
#
#   ./logs.sh                # 默认跟 connector 日志
#   ./logs.sh buddy -f       # 跟 alive-buddy（大脑）日志
#   ./logs.sh connector --since=10m
#   ./logs.sh ml             # ML sidecar
#
# 等价于 cd 到本目录后执行 docker compose logs <选项> <服务>。
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-connector}" in
  connector|conn|endra-connector) service=endra-connector ;;
  buddy|alive|alive-buddy)        service=alive-buddy ;;
  ml|sidecar|ml-sidecar)          service=ml-sidecar ;;
  -*)                             service=endra-connector ;;   # 直接以选项开头
  *)                              service="$1"; shift ;;
esac
[ "${1:-}" != "" ] && shift || true

exec docker compose logs "$@" "$service"
