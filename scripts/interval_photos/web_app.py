"""NAS photo classification and verified copying; source photos remain unchanged."""
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeSerializer
from image_preview import ImageCache, ImagePreviewError, file_version, resolve_image
from interval_photos import RAW_EXTENSIONS, detect_sets
from copy_jobs import CopyError, CopyManager


class PreviewError(Exception):
    pass


def resolve_folder(root, relative):
    if not isinstance(relative, str) or '\x00' in relative:
        raise PreviewError('フォルダの指定が不正です。')
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise PreviewError('写真ルートの外にはアクセスできません。')
    if not path.is_dir():
        raise PreviewError('フォルダが存在しないか、参照できません。')
    return path


def parse_date(record, default_zone):
    value = record.get('DateTimeOriginal')
    if not isinstance(value, str):
        raise ValueError('撮影日時なし')
    date = datetime.strptime(value, '%Y:%m:%d %H:%M:%S')
    subseconds = str(record.get('SubSecTimeOriginal', ''))
    if subseconds:
        if not subseconds.isascii() or not subseconds.isdigit():
            raise ValueError('秒未満の値が不正')
        date = date.replace(microsecond=int((subseconds + '000000')[:6]))
    offset = record.get('OffsetTimeOriginal')
    zone = default_zone
    if offset:
        if not isinstance(offset, str) or len(offset) != 6 or offset[0] not in '+-' or offset[3] != ':':
            raise ValueError('タイムゾーンが不正')
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
            raise ValueError('タイムゾーンが不正')
        zone = timezone(timedelta(minutes=(hours * 60 + minutes) * (1 if offset[0] == '+' else -1)))
    return date.replace(tzinfo=zone)


def read_exif(paths, executable):
    if not paths:
        return []
    # Absolute paths cannot be interpreted as switches in ExifTool's argument file.
    if any('\n' in str(path) or '\r' in str(path) for path in paths):
        raise PreviewError('改行を含むファイル名には対応していません。')
    try:
        result = subprocess.run(
            [executable, '-config', '', '-j', '-DateTimeOriginal',
             '-SubSecTimeOriginal', '-OffsetTimeOriginal', '-charset', 'filename=UTF8', '-@', '-'],
            input='\n'.join(str(path) for path in paths) + '\n',
            capture_output=True, text=True, encoding='utf-8', timeout=180,
        )
    except FileNotFoundError as exc:
        raise PreviewError('ExifTool が見つかりません。Docker イメージを確認してください。') from exc
    except subprocess.TimeoutExpired as exc:
        raise PreviewError('Exif の読み取りが180秒を超えました。小さいフォルダで試してください。') from exc
    try:
        records = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise PreviewError('ExifTool の出力を読み取れませんでした。') from exc
    if result.returncode not in (0, 1) or not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise PreviewError('ExifTool の読み取りに失敗しました。')
    return records


def scan_folder(folder, zone, executable, minimum, maximum, tolerance, count):
    entries = list(folder.iterdir())
    jpgs = sorted((p for p in entries if not p.is_symlink() and p.is_file()
                   and p.suffix.lower() in {'.jpg', '.jpeg'}), key=lambda p: p.name)
    raw_dirs = [folder] + [p for p in entries if not p.is_symlink() and p.is_dir() and p.name.lower() == 'raw']
    partners = {}
    for directory in raw_dirs:
        for path in directory.iterdir():
            if not path.is_symlink() and path.is_file() and path.suffix.lower() in RAW_EXTENSIONS:
                partners.setdefault(path.stem.casefold(), []).append(path.relative_to(folder).as_posix())
    records = {r.get('SourceFile'): r for r in read_exif(jpgs, executable)}
    photos, dates, excluded = [], {}, []
    assumed_zone = 0
    for path in jpgs:
        record = records.get(str(path), {})
        try:
            if 'Error' in record:
                raise ValueError('Exif を読み取れません')
            date = parse_date(record, zone)
            photos.append((path, date.timestamp()))
            dates[path] = date
            if not record.get('OffsetTimeOriginal'):
                assumed_zone += 1
        except (ValueError, TypeError, OverflowError, OSError):
            excluded.append(path.name)
    ordered = sorted(photos, key=lambda item: (item[1], item[0].name))
    positions = {path: index for index, (path, _) in enumerate(ordered)}

    def describe_file(path):
        return {'jpg': path.name, 'time': dates[path].isoformat(),
                'raw': sorted(partners.get(path.stem.casefold(), []))}

    groups = []
    for number, (group, interval) in enumerate(detect_sets(photos, minimum, maximum, tolerance, count), 1):
        gaps = [b[1] - a[1] for a, b in zip(group, group[1:])]
        files = [describe_file(p) for p, _ in group]
        first_index = positions[group[0][0]]
        last_index = positions[group[-1][0]]
        before = describe_file(ordered[first_index - 1][0]) if first_index > 0 else None
        after = describe_file(ordered[last_index + 1][0]) if last_index + 1 < len(ordered) else None
        if before:
            before['gap'] = group[0][1] - ordered[first_index - 1][1]
        if after:
            after['gap'] = ordered[last_index + 1][1] - group[-1][1]
        groups.append({'number': number, 'count': len(group), 'interval': interval,
                       'gap_min': min(gaps), 'gap_max': max(gaps),
                       'start': dates[group[0][0]].isoformat(), 'end': dates[group[-1][0]].isoformat(),
                       'paired_count': sum(bool(f['raw']) for f in files), 'files': files,
                       'before': before, 'after': after})
    return {'jpg_count': len(jpgs), 'dated_count': len(photos), 'excluded': excluded,
            'assumed_zone_count': assumed_zone, 'timezone': str(zone), 'sets': groups}


def scan_options(data):
    if not isinstance(data, dict):
        raise PreviewError('条件の指定が不正です。')
    values = []
    for key, default in [('min_interval', 3), ('max_interval', 30), ('tolerance', 1)]:
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PreviewError('間隔・許容幅は数値で指定してください。')
        values.append(value)
    minimum, maximum, tolerance = values
    count = data.get('min_count', 5)
    if isinstance(count, bool) or not isinstance(count, int) or count < 5:
        raise PreviewError('最小枚数は5以上の整数にしてください。')
    if not 0 < minimum <= maximum or not 0 <= tolerance < minimum / 2:
        raise PreviewError('間隔は正の数、許容幅は最小間隔の半分未満にしてください。')
    return minimum, maximum, tolerance, count


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(PHOTO_ROOT=os.environ.get('PHOTO_ROOT', '/photos'),
                      PHOTO_TIMEZONE=os.environ.get('PHOTO_TIMEZONE', 'Asia/Tokyo'),
                      COPY_ROOT=os.environ.get('COPY_ROOT'),
                      ALLOW_MOVE=os.environ.get('ALLOW_MOVE') == '1',
                      EXIFTOOL=os.environ.get('EXIFTOOL', 'exiftool'),
                      PREVIEW_CACHE=os.environ.get('PREVIEW_CACHE', '/tmp/interval-preview-cache'),
                      MAX_CONTENT_LENGTH=16 * 1024,
                      SCAN_TOKEN=secrets.token_urlsafe(32))
    if config:
        app.config.update(config)
    root = Path(app.config['PHOTO_ROOT']).resolve()
    zone = ZoneInfo(app.config['PHOTO_TIMEZONE'])
    copy_manager = CopyManager(root, app.config['COPY_ROOT'], allow_move=app.config['ALLOW_MOVE'])
    app.extensions['copy_manager'] = copy_manager
    scan_lock = threading.Lock()
    image_cache = ImageCache(app.config['PREVIEW_CACHE'])
    image_signer = URLSafeSerializer(app.config['SCAN_TOKEN'], salt='photo-preview')

    @app.after_request
    def response_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(PreviewError)
    def bad_preview(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(CopyError)
    def bad_copy(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(ImagePreviewError)
    def bad_image(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(OSError)
    def inaccessible(error):
        app.logger.warning('Filesystem operation failed: %s', type(error).__name__)
        return jsonify(error='フォルダの読み取りに失敗しました。接続とアクセス権を確認してください。'), 400

    @app.get('/')
    def index():
        return render_template('index.html', scan_token=app.config['SCAN_TOKEN'])

    @app.get('/api/folders')
    def folders():
        folder = resolve_folder(root, request.args.get('path', ''))
        relative = folder.relative_to(root)
        children = [{'name': p.name, 'path': p.relative_to(root).as_posix()}
                    for p in folder.iterdir() if not p.is_symlink() and p.is_dir() and not p.name.startswith('.')]
        return jsonify(path='' if folder == root else relative.as_posix(),
                       parent=None if folder == root else ('' if folder.parent == root else relative.parent.as_posix()),
                       folders=sorted(children, key=lambda p: p['name'].casefold()))

    @app.get('/api/image/<token>')
    def preview_image(token):
        try:
            payload = image_signer.loads(token)
        except BadSignature:
            return jsonify(error='画像リンクが無効です。もう一度解析してください。'), 404
        if not isinstance(payload, dict) or not isinstance(payload.get('version'), list):
            raise ImagePreviewError('画像リンクが不正です。')
        path = resolve_image(root, payload.get('path'))
        data = image_cache.get(path, request.args.get('size', 'thumb'), payload['version'])
        return Response(data, mimetype='image/jpeg')

    @app.get('/api/copy/status')
    def copy_status():
        return jsonify(copy_enabled=copy_manager.destination_root is not None, move_enabled=copy_manager.allow_move, job=copy_manager.status())

    @app.post('/api/copy')
    def start_copy():
        if not hmac.compare_digest(request.headers.get('X-Scan-Token', '').encode('utf-8'), app.config['SCAN_TOKEN'].encode('utf-8')):
            return jsonify(error='ページを再読み込みしてください。'), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise CopyError('コピー条件の指定が不正です。')
        job = copy_manager.start(data.get('scan_id'), data.get('sets'), data.get('operation', 'copy'))
        return jsonify(job=job), 202

    @app.post('/api/scan')
    def scan():
        if not hmac.compare_digest(request.headers.get('X-Scan-Token', '').encode('utf-8'), app.config['SCAN_TOKEN'].encode('utf-8')):
            return jsonify(error='ページを再読み込みしてください。'), 403
        data = request.get_json(silent=True)
        options = scan_options(data)
        folder = resolve_folder(root, data.get('folder', ''))
        if not scan_lock.acquire(blocking=False):
            return jsonify(error='別の解析が実行中です。完了後に試してください。'), 409
        try:
            result = scan_folder(folder, zone, app.config['EXIFTOOL'], *options)
            result['folder'] = folder.relative_to(root).as_posix()
            for group in result['sets']:
                for file in group['files'] + [p for p in (group['before'], group['after']) if p is not None]:
                    path = folder / file['jpg']
                    token = image_signer.dumps({'path': path.relative_to(root).as_posix(), 'version': file_version(path)})
                    file['thumbnail_url'] = url_for('preview_image', token=token, size='thumb')
                    file['preview_url'] = url_for('preview_image', token=token, size='preview')
            result['scan_id'] = copy_manager.register(folder, result['sets'])
            return jsonify(result)
        finally:
            scan_lock.release()

    return app


if __name__ == '__main__':
    from waitress import serve
    serve(create_app(), host='0.0.0.0', port=8080, threads=4)
