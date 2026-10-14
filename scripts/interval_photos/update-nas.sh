#!/bin/sh
# Run from the existing app directory. Preserve local Compose and environment settings.
set -eu
if [ ! -f compose.yaml ] || [ ! -f web_app.py ]; then
    echo 'scripts/interval_photos のフォルダ内で実行してください。' >&2
    exit 1
fi
# Let Compose resolve .env without executing it as a shell script.
updater_user=$(python3 -c '
import json, re, subprocess
config = json.loads(subprocess.check_output(["sudo", "docker", "compose", "config", "--format", "json"], text=True))
user = config["services"]["interval-preview"]["user"]
if not isinstance(user, str) or re.fullmatch(r"[0-9]+:[0-9]+", user) is None:
    raise SystemExit("Compose の user は数値の UID:GID で設定してください。")
print(user)
')
sudo docker run --rm --user "$updater_user" \
  --mount "type=bind,src=$PWD,dst=/app" \
  alpine:3.21 sh -eu -c '
    wget -O /tmp/source.tar.gz https://codeload.github.com/photo-akky/script-library/tar.gz/refs/heads/nas-interval-preview
    tar -xzf /tmp/source.tar.gz -C /tmp
    source_dir=/tmp/script-library-nas-interval-preview/scripts/interval_photos
    for file in Dockerfile requirements.txt requirements.lock interval_photos.py web_app.py image_preview.py README.md WEB_README.md .dockerignore update-nas.sh; do
      test -f "$source_dir/$file"
    done
    test -d "$source_dir/templates"
    test -d "$source_dir/static"
    for file in Dockerfile requirements.txt requirements.lock interval_photos.py web_app.py image_preview.py README.md WEB_README.md .dockerignore update-nas.sh; do
      cp "$source_dir/$file" "/app/$file"
    done
    cp -R "$source_dir/templates" "$source_dir/static" /app/
    echo "コードを更新しました。compose.yaml と .env は保持しています。"
  '
