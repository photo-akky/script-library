#!/bin/sh
# Update a Git-cloned NAS deployment. Code is bind-mounted, so only image inputs trigger a rebuild.
# Usage: sh deploy-nas.sh [--build]
set -eu
cd "$(dirname "$0")"
if [ ! -f compose.yaml ] || [ ! -f web_app.py ]; then
    echo 'scripts/interval_photos のフォルダ内に置いて実行してください。' >&2
    exit 1
fi
force_build=0
case "${1:-}" in
    '') ;;
    --build) force_build=1 ;;
    *) echo "使い方: sh deploy-nas.sh [--build]" >&2; exit 2 ;;
esac

docker_cmd() {
    if docker info >/dev/null 2>&1; then docker "$@"; else sudo docker "$@"; fi
}

running=$(docker_cmd compose ps --status running -q interval-preview 2>/dev/null || true)

# A restart drops in-memory job progress, so never interrupt a copy or move.
if [ -n "$running" ]; then
    state=$(docker_cmd compose exec -T interval-preview python -c '
import json, urllib.request
job = json.load(urllib.request.urlopen("http://127.0.0.1:8080/api/copy/status", timeout=5))["job"]
print(job["state"] if job else "")
' 2>/dev/null || true)
    if [ "$state" = running ]; then
        echo 'コピー・移動の処理中です。完了してから実行してください。' >&2
        exit 1
    fi
fi

before=$(git rev-parse HEAD)
git pull --ff-only
after=$(git rev-parse HEAD)
changed=$(git diff --name-only --relative "$before" "$after" -- .)

if [ -n "$changed" ]; then
    echo '更新されたファイル:'
    echo "$changed" | sed 's/^/  /'
fi

if [ "$force_build" = 1 ] || echo "$changed" | grep -qxE 'Dockerfile|requirements\.lock|\.dockerignore'; then
    echo 'イメージを再ビルドして起動します。'
    docker_cmd compose up -d --build
elif [ -z "$running" ] || echo "$changed" | grep -qx 'compose\.yaml'; then
    echo 'コンテナを作り直して起動します（再ビルドなし）。'
    docker_cmd compose up -d
elif [ -n "$changed" ]; then
    echo 'コンテナを再起動します。'
    docker_cmd compose restart interval-preview
else
    echo '変更はありません。'
    exit 0
fi

# Wait until the app answers inside the container.
i=0
until docker_cmd compose exec -T interval-preview python -c '
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8080/api/copy/status", timeout=2)
' >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then
        echo '起動を確認できませんでした。docker compose logs で確認してください。' >&2
        exit 1
    fi
    sleep 1
done
echo "更新しました（$(git rev-parse --short HEAD)）。"
