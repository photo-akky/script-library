"""Generate bounded JPEG previews without changing source photos."""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import threading
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

SIZES = {'thumb': (360, 270), 'preview': (1600, 1200)}
MAX_PIXELS = 80_000_000


class ImagePreviewError(Exception):
    pass


def file_version(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def resolve_image(root, relative):
    if not isinstance(relative, str) or '\x00' in relative:
        raise ImagePreviewError('写真の指定が不正です。')
    relative_path = Path(relative)
    if relative_path.is_absolute() or '..' in relative_path.parts:
        raise ImagePreviewError('写真ルートの外にはアクセスできません。')
    candidate = root
    for part in relative_path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ImagePreviewError('シンボリックリンクの写真は表示できません。')
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file() or resolved.suffix.lower() not in {'.jpg', '.jpeg'}:
        raise ImagePreviewError('JPG/JPEG の写真を指定してください。')
    return resolved


class ImageCache:
    def __init__(self, directory, max_bytes=32 * 1024 * 1024, max_entries=256):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        # Serialize decoding to avoid loading many full-resolution photos at once.
        self.lock = threading.Lock()

    def get(self, path, size, version):
        if size not in SIZES:
            raise ImagePreviewError('画像サイズの指定が不正です。')
        with self.lock:
            if file_version(path) != version:
                raise ImagePreviewError('写真が変更されています。もう一度解析してください。')
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            key = sha256(json.dumps([str(path), version, size, 1]).encode()).hexdigest()
            cached = self.directory / (key + '.jpg')
            if cached.is_file():
                data = cached.read_bytes()
                cached.touch()
                return data
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(path) as original:
                        if original.format not in {'JPEG', 'MPO'}:
                            raise ImagePreviewError(f'対応していない画像形式です: {original.format}（JPEG / MPO に対応）。')
                        # Sony JPGs can contain MPF data and are recognized as MPO by Pillow.
                        # Only the primary image is used; secondary frames are not decoded.
                        original.seek(0)
                        if original.width * original.height > MAX_PIXELS:
                            raise ImagePreviewError(f'写真が大きすぎます: {original.width}×{original.height}（上限8000万画素）。')
                        # libjpeg downsamples during decoding; no RAW decoding or ExifTool process here.
                        original.draft('RGB', SIZES[size])
                        image = ImageOps.exif_transpose(original)
                        image.thumbnail(SIZES[size], Image.Resampling.LANCZOS)
                        image = image.convert('RGB')
                        buffer = BytesIO()
                        # No source EXIF is copied into the generated preview.
                        image.save(buffer, format='JPEG', quality=78)
                        data = buffer.getvalue()
            except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError) as exc:
                raise ImagePreviewError('この写真のプレビューを作成できませんでした。') from exc
            if file_version(path) != version:
                raise ImagePreviewError('画像生成中に写真が変更されました。もう一度解析してください。')
            self._prune(len(data))
            if len(data) <= self.max_bytes and self.max_entries > 0:
                # All access is serialized and only complete files become cache entries.
                temporary = cached.with_suffix('.tmp')
                try:
                    temporary.write_bytes(data)
                    temporary.replace(cached)
                finally:
                    temporary.unlink(missing_ok=True)
            return data

    def _prune(self, incoming):
        entries = sorted(self.directory.glob('*.jpg'), key=lambda p: p.stat().st_mtime_ns)
        total = sum(p.stat().st_size for p in entries)
        while entries and (total + incoming > self.max_bytes or len(entries) + 1 > self.max_entries):
            oldest = entries.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink()
