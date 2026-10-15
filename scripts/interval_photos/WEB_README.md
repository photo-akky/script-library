# NAS で動く写真分類・コピー・移動 Web GUI

Docker 内で JPG の Exif を読み取り、インターバル撮影候補をブラウザに表示します。セットの先頭・中央・末尾と直前・直後のサムネイルを表示し、クリックで拡大できます。選択したセットの JPG / RAW を NAS 内でコピーし、SHA-256 で内容を照合します。コピーでは元写真を保持します。移動を選ぶと、全件の照合後に元ファイルを削除します。Mac の作成日時と NAS の作成日時が異なることを確認したため、NAS 版では Exif の `DateTimeOriginal` を使用します。

## UGREEN DXP4800 Plus への配置

このフォルダのファイルを NAS のアプリ用フォルダへコピーし、SSH でそのフォルダに移動してください。写真ルートは `.env` の `PHOTO_SOURCE` で指定し、`.env` の `APP_UID` / `APP_GID` の権限で実行します。

```sh
cp .env.example .env
```

`.env` の `PHOTO_SOURCE` を NAS 上の写真ルートの絶対パスに変更してください。実在のパスは Git 管理対象外の `.env` にだけ保存します。未設定なら Compose は起動せず、存在しないフォルダも自動作成しません。コンテナ内の参照先 `/photos` は共通です。

NAS の SSH で `id -u` と `id -g` を実行し、その数値を `.env` の `APP_UID` と `APP_GID` に設定してください。両方とも必須です。Docker イメージの既定ユーザーと実行時のユーザーに反映され、更新スクリプトも同じユーザーを使います。

`.env` の `PHOTO_DESTINATION` に、既存のコピー先ルートの絶対パスを設定してください。`APP_UID` / `APP_GID` のユーザーがそのフォルダに書き込める必要があります。移動に対応するため、写真ルートと保存先の両方を書き込み可能でマウントします。コピーを選んだ場合は元写真を書き換えません。

`.env` の `NAS_BIND_IP` を NAS の LAN IP に変更してください。そのアドレスのポート8088で待ち受けます。認証機能はまだないため、家庭内の信頼できる LAN で候補確認に使い、インターネットには公開しないでください。既定の `127.0.0.1` のままなら SSH 転送で使えます。

```sh
sudo docker compose up -d --build
sudo docker compose logs --tail=50
```

Mac のブラウザで `http://NASのLAN-IP:8088` を開きます。既定のループバック設定で使う場合は、Mac で次を実行し、ブラウザで `http://localhost:8088` を開きます。

```sh
ssh -N -L 8088:127.0.0.1:8088 nas-user@NASのLAN-IP
```

画面で `2026/10/11` などの撮影フォルダを選び、解析します。選択フォルダ直下の JPG/JPEG のみが対象です。RAW は同じフォルダと直下の `RAW` フォルダで同名のファイルを探します。

- 既定は3〜30秒、許容幅±1秒、5枚以上。欠落分を補完せず、候補を重複させません。
- ExifTool が対応する秒未満情報とタイムゾーンを読み取ります。タイムゾーンがない写真には `PHOTO_TIMEZONE`（既定 `Asia/Tokyo`）を使用し、画面に件数を表示します。
- 撮影日時を読み取れない JPG は除外し、ファイル名を表示します。更新日時・作成日時には切り替えません。
- サムネイルは最大360×270、拡大画像は最大1600×1200。JPG の縮小デコードと向き補正を使い、RAW は画像生成に使いません。Sony の JPG などが MPO と認識される場合も、先頭の主画像からプレビューを生成します。全枚ギャラリーは開くまで生成せず、画面に近い画像から読み込みます。
- 直前・直後は撮影日時を取得できた JPG の時刻順で選び、セット端との時間差を表示します。日時なしの写真はここでも対象外です。
- 画像キャッシュはコンテナの `/tmp` 内、上限32MiB・256件。再起動で消えます。画像生成は1件ずつ行い、元写真の変更を検知した場合は再解析を求めます。
- シンボリックリンクをたどるフォルダ一覧と写真は対象外です。写真ルート外のパスは拒否します。
- 解析は1件ずつ、Exif 読み取りの制限時間は180秒です。タイムアウト時は小さいフォルダで試してください。
- 移動時は元写真がある各フォルダに削除権限が必要です。元ファイルを含むフォルダ自体は削除しません。

## セットをコピー / 移動

解析後、対象のセットにチェックし、処理方法で「コピー」または「移動」を選んで実行します。既定はコピーです。確認画面のフォルダ一覧を確認すると処理が始まります。初期状態で選択されるセットはありません。直前・直後の比較写真はコピーしません。

保存先は写真ルートからの階層を維持し、末尾のフォルダ名に `-{セット番号}` を付けます。例えば `2026/10/11` のセット1は `PHOTO_DESTINATION` 配下の `2026/10/11-1`、セット2は `2026/10/11-2` です。番号は画面のセット番号（1始まり、ゼロ埋めなし）です。写真ルート直下を解析した場合は `root-1` などに保存します。親フォルダは必要に応じて作成し、シンボリックリンクの階層には書き込みません。JPG と同名 RAW をコピーし、RAW サブフォルダの構成を保持します。コピーでは同名の保存先フォルダがあれば開始前に停止し、上書きしません。解析後に元ファイルが変わっていても再解析を求めます。解析結果は最大8件・1時間まで保持します。

処理は NAS 内で実行し、ブラウザを閉じても継続します。再度開くと現在の処理状況を取得できます。転送済み容量と、内容検証まで完了したファイル数を分けて表示します。コピー後に保存先を読み直して SHA-256 を照合するため、単純コピーより読み取りが1回増えます。ファイルの更新日時を引き継ぎます。

各セットに `copy-log.jsonl` を保存します。コピーが終わっていないセットには `COPY_INCOMPLETE`、未完了ファイルには `.part` が残ります。失敗時は自動再試行・上書きをせず、保存先とログを確認してください。コピーでは元写真を常に保持します。コンテナを再起動すると進捗情報はリセットされるため、処理中の更新・再起動は避けてください。保存先ファイルの確定には同一ファイルシステム内のハードリンクを使います。

### 移動の手順と既存コピー

「移動」では、選択した全セットの保存先を準備し、元データと保存先を全件照合してから削除に進みます。各削除の直前にも SHA-256 とファイルの変更を確認し、保存先のデータ・親フォルダを同期します。元ファイルを削除する前後を `move-log.jsonl` に記録します。

すでに同名の保存先フォルダがある場合は、移動に限り既存データを照合して利用できます。全対象ファイルがそろい、SHA-256 が一致する必要があります。既存ファイルは上書きしません。`COPY_INCOMPLETE` / `MOVE_INCOMPLETE` または以前の `move-log.jsonl` がある保存先は自動再利用しません。内容が不一致なら元ファイルは削除しません。

コピー・全件照合の段階で失敗した場合は元ファイルを残します。削除開始後の失敗では一部が移動済みになる場合があるため、画面の「元ファイル削除済み件数」、`MOVE_INCOMPLETE` と `move-log.jsonl`、元・保存先の実ファイルを確認してください。元ファイルの自動復元や中断後の自動再開は行いません。

移動中は他のアプリで対象写真・保存先を変更せず、NAS のコンテナを再起動しないでください。移動後はセット番号が変わる場合があるため、再解析してから次の操作を行います。既存の移動完了フォルダへ別セットを上書きすることはありません。

停止と再起動:

```sh
sudo docker compose down
sudo docker compose up -d
```

## Docker Compose が利用できない場合

```sh
export PHOTO_SOURCE=/path/to/photos
export PHOTO_DESTINATION=/path/to/timelapses
export APP_UID="$(id -u)" APP_GID="$(id -g)"
sudo docker build --build-arg APP_UID="$APP_UID" --build-arg APP_GID="$APP_GID" -t interval-photos-preview:local .
sudo docker run -d --name interval-preview \
  --user "$APP_UID:$APP_GID" --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,size=64m,mode=1777 \
  --mount "type=bind,src=${PHOTO_SOURCE:?Set PHOTO_SOURCE},dst=/photos" \
  --mount "type=bind,src=${PHOTO_DESTINATION:?Set PHOTO_DESTINATION},dst=/copies" \
  -e COPY_ROOT=/copies -e ALLOW_MOVE=1 \
  -p 127.0.0.1:8088:8080 \
  --restart unless-stopped interval-photos-preview:local
```

この例の `/path/to/photos` と `/path/to/timelapses` は実際の写真ルート・コピー先に置き換えてください。`docker run` は `.env` を自動で読みません。この場合も上記の SSH 転送で操作できます。既存の同名コンテナは自動で削除しません。

## Git で clone 済みの NAS を更新

アプリのフォルダで実行します。

```sh
git pull --ff-only
```

コピー機能の初回導入時は `.env` に `PHOTO_DESTINATION` を追加し、既存の書き込み可能な保存先ルートを指定してください。移動機能を導入する際は、更新された Compose が写真ルートを `read_only: false` でマウントし、環境変数 `ALLOW_MOVE: "1"` を設定していることを確認してください。その後に再ビルドします。

```sh
sudo docker compose up -d --build
```

`.env` は Git 管理対象外なので設定は保持されます。Compose にローカル変更がある場合、`git pull` が止まることがあります。ローカル変更を確認してから更新し、上書きで消さないでください。

## アーカイブで配置した NAS を更新

起動中のサービスを停止せず、まずコードを更新します。既存の `compose.yaml` と `.env` は保持するため、UGOS 用に設定したアクセス方法を維持できます。

```sh
cd /path/to/app/scripts/interval_photos
```

上の `/path/to/app` は実際の配置先に置き換えてください。

初回だけ更新スクリプトを取得します:

```sh
sudo docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/app" \
  alpine:3.21 wget -O /app/update-nas.sh \
  https://raw.githubusercontent.com/photo-akky/script-library/nas-interval-preview/scripts/interval_photos/update-nas.sh
```

続いて更新・再ビルドします。2つ目のコマンドは更新成功後に実行してください。

```sh
sh update-nas.sh
sudo docker compose up -d --build
```

再ビルド完了後、ブラウザを再読み込みしてもう一度解析してください。公開リポジトリから取得する手順です。非公開に変更した場合は認証を使う取得方法が別途必要です。

アーカイブ配置用の更新スクリプトは既存の `compose.yaml` を保持します。コピー機能を有効にする場合は `.env` に `PHOTO_DESTINATION` を設定し、Compose の `environment` に `COPY_ROOT: /copies`、`volumes` に次のマウントを追加してください。

```yaml
- type: bind
  source: "${PHOTO_DESTINATION:?Set PHOTO_DESTINATION in .env to the copy destination directory}"
  target: /copies
  read_only: false
  bind:
    create_host_path: false
```

アーカイブ配置で移動も有効にする場合は、さらに写真ルートのマウントを `read_only: false` にし、Compose の `environment` に `ALLOW_MOVE: "1"` を追加してください。

## 以前の固定パス設定から移行する場合

更新スクリプトはローカルの `compose.yaml` を保持します。そのため、既存環境では `.env` に `PHOTO_SOURCE` を追加したうえで、`compose.yaml` の写真マウントの `source:` を次のように変更してください。他の設定はそのまま保持します。

```yaml
source: "${PHOTO_SOURCE:?Set PHOTO_SOURCE in .env to the NAS photo directory}"
```

UID/GID が固定されている既存環境では、さらに `.env` に `APP_UID` / `APP_GID` を追加し、Compose の `user` と `build` を変更します。

```yaml
build:
  context: .
  args:
    APP_UID: "${APP_UID:?Set APP_UID in .env}"
    APP_GID: "${APP_GID:?Set APP_GID in .env}"
user: "${APP_UID:?Set APP_UID in .env}:${APP_GID:?Set APP_GID in .env}"
```

`.env` を丸ごと上書きせず、既存の `NAS_BIND_IP` などの値を保持してください。変更後に `sudo docker compose up -d --build` で再作成します。

## 開発時の検証

リポジトリのルートから:

```sh
cd scripts/interval_photos
python3 -m venv /tmp/interval-web-venv
/tmp/interval-web-venv/bin/pip install -r requirements.lock
/tmp/interval-web-venv/bin/python -m unittest discover -s tests -v
```

Web のテストは Flask が必要です。CLI だけを利用する場合は引き続き外部パッケージ不要です。

BuildKit / buildx がない NAS でも通常の Docker ビルドで利用できます。証明書付きプロキシを使う開発環境では別途、信頼済み CA の設定が必要です。TLS とパッケージ署名の検証は無効にしません。
