"""Copy immutable scan selections in one worker, verifying each file with SHA-256."""
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import threading
import time

from image_preview import file_version


class CopyError(Exception):
    pass


def checked_source(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise CopyError('写真ルート外のファイルはコピーできません。')
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise CopyError('シンボリックリンクはコピーできません。')
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise CopyError('元ファイルを確認できません。もう一度解析してください。')
    return path


@dataclass(frozen=True)
class CopyFile:
    source: Path
    target: Path
    version: tuple


@dataclass(frozen=True)
class CopySet:
    name: str
    files: tuple


class CopyManager:
    def __init__(self, source_root, destination_root):
        self.source_root = Path(source_root).resolve()
        self.destination_root = Path(destination_root).resolve() if destination_root else None
        self.lock = threading.Lock()
        self.scans = OrderedDict()
        self.job = None

    def register(self, folder, groups):
        plans = {}
        for group in groups:
            files, targets, sources = [], set(), set()
            stamp = datetime.fromisoformat(group['start']).strftime('%Y%m%d_%H%M%S')
            group['target_folder'] = f'interval_{stamp}_{group["number"]:03d}'
            try:
                for photo in group['files']:
                    for relative in [photo['jpg'], *photo['raw']]:
                        path = checked_source(self.source_root, (folder / relative).relative_to(self.source_root))
                        target = Path(relative)
                        folded = target.as_posix().casefold()
                        if folded in targets or path in sources:
                            raise CopyError('同名ファイルや JPG/RAW の重複があるためコピーできません。')
                        sources.add(path)
                        targets.add(folded)
                        files.append(CopyFile(path, target, tuple(file_version(path))))
                plans[group['number']] = CopySet(group['target_folder'], tuple(files))
                group['copy_file_count'] = len(files)
                group['copy_bytes'] = sum(file.version[2] for file in files)
            except (CopyError, OSError) as error:
                group['copy_error'] = str(error)
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.scans[token] = (time.monotonic(), plans)
            while len(self.scans) > 8:
                self.scans.popitem(last=False)
        return token

    def status(self):
        with self.lock:
            return dict(self.job) if self.job else None

    def start(self, scan_id, selected):
        if not isinstance(scan_id, str) or not isinstance(selected, list) or not selected or any(type(n) is not int for n in selected) or len(set(selected)) != len(selected):
            raise CopyError('コピーするセットを選んでください。')
        with self.lock:
            if self.job and self.job['state'] == 'running':
                raise CopyError('コピーが実行中です。完了後に試してください。')
            snapshot = self.scans.get(scan_id)
            if not snapshot or time.monotonic() - snapshot[0] > 3600:
                raise CopyError('解析結果が期限切れです。もう一度解析してください。')
            plans = snapshot[1]
            if any(number not in plans for number in selected):
                raise CopyError('選択したセットをコピーできません。もう一度解析してください。')
            root = self.destination_root
            if root is None or not root.is_dir():
                raise CopyError('コピー先が未設定です。PHOTO_DESTINATION とマウントを確認してください。')
            if root.samefile(self.source_root):
                raise CopyError('コピー先ルートは写真ルートと別のフォルダにしてください。')
            sets = tuple(plans[n] for n in sorted(selected))
            all_sources = set()
            for group in sets:
                destination = root / group.name
                if destination.exists() or destination.is_symlink():
                    raise CopyError(f'コピー先が既に存在します: {group.name}。上書きはしません。')
                for file in group.files:
                    if file.source in all_sources:
                        raise CopyError('選択したセット間で同じファイルが重複しています。')
                    all_sources.add(file.source)
                    path = checked_source(self.source_root, file.source.relative_to(self.source_root))
                    if tuple(file_version(path)) != file.version:
                        raise CopyError(f'解析後に元ファイルが変更されました: {path.name}。再解析してください。')
            total_bytes = sum(file.version[2] for group in sets for file in group.files)
            if shutil.disk_usage(root).free < total_bytes:
                raise CopyError('コピー先の空き容量が不足しています。')
            # Reserve every set directory exclusively before starting work.
            created = []
            try:
                for group in sets:
                    path = root / group.name
                    path.mkdir(exist_ok=False)
                    created.append(path)
            except OSError:
                for path in reversed(created):
                    path.rmdir()  # Only the empty directories created by this request.
                raise
            self.job = {'id': secrets.token_urlsafe(18), 'state': 'running', 'phase': '準備',
                        'files_done': 0, 'files_total': sum(len(group.files) for group in sets),
                        'bytes_copied': 0, 'bytes_total': total_bytes, 'current_file': '',
                        'destinations': [group.name for group in sets], 'error': None}
            job = dict(self.job)
            worker = threading.Thread(target=self._run, args=(sets,), daemon=True)
            try:
                worker.start()
            except Exception:
                self.job = None
                for path in reversed(created):
                    path.rmdir()
                raise
            return job

    def _update(self, **values):
        with self.lock:
            self.job.update(values)

    @staticmethod
    def _record(log, event, **values):
        log.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'event': event, **values}, ensure_ascii=False) + '\n')
        log.flush()
        os.fsync(log.fileno())

    def _copy_file(self, file, target, log):
        source = checked_source(self.source_root, file.source.relative_to(self.source_root))
        self._update(current_file=file.target.as_posix(), phase='コピー中')
        partial = target.with_name(target.name + '.part')
        digest = hashlib.sha256()
        self._record(log, 'file_started', source=source.relative_to(self.source_root).as_posix(), target=file.target.as_posix(), size=file.version[2])
        with source.open('rb') as reader:
            info = os.fstat(reader.fileno())
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != file.version or not stat.S_ISREG(info.st_mode):
                raise CopyError('元ファイルが変更されました。再解析してください。')
            with partial.open('xb') as writer:
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
                    digest.update(chunk)
                    with self.lock:
                        self.job['bytes_copied'] += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != file.version:
                raise CopyError('コピー中に元ファイルが変更されました。')
        if tuple(file_version(source)) != file.version:
            raise CopyError('コピー中に元ファイルが置き換えられました。')
        self._update(phase='内容を検証中')
        verified = hashlib.sha256()
        with partial.open('rb') as reader:
            while chunk := reader.read(1024 * 1024):
                verified.update(chunk)
        if partial.stat().st_size != file.version[2] or verified.digest() != digest.digest():
            raise CopyError('コピー先の内容が一致しません。未完了ファイルを確認してください。')
        if tuple(file_version(source)) != file.version:
            raise CopyError('検証中に元ファイルが変更されました。')
        # Preserve mtime without changing the source (copystat can copy a read-only mode).
        os.utime(partial, ns=(file.version[3], file.version[3]))
        os.link(partial, target)  # Atomic publication without replacing an existing file.
        partial.unlink()
        self._record(log, 'file_verified', target=file.target.as_posix(), size=file.version[2], sha256=digest.hexdigest())
        with self.lock:
            self.job['files_done'] += 1

    def _run(self, sets):
        root = self.destination_root
        try:
            for group in sets:
                folder = root / group.name
                (folder / 'COPY_INCOMPLETE').write_text('コピー未完了。copy-log.jsonl を確認してください。\n', encoding='utf-8')
                with (folder / 'copy-log.jsonl').open('x', encoding='utf-8') as log:
                    self._record(log, 'set_started', files=[file.target.as_posix() for file in group.files])
            for group in sets:
                folder = root / group.name
                with (folder / 'copy-log.jsonl').open('a', encoding='utf-8') as log:
                    for file in group.files:
                        target = folder / file.target
                        target.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_file(file, target, log)
                    self._record(log, 'set_completed')
                (folder / 'COPY_INCOMPLETE').unlink()
            self._update(state='completed', phase='完了', current_file='')
        except Exception as error:
            message = str(error)
            for group in sets:
                log_path = root / group.name / 'copy-log.jsonl'
                try:
                    if (log_path.parent / 'COPY_INCOMPLETE').exists():
                        with log_path.open('a', encoding='utf-8') as log:
                            self._record(log, 'job_failed', error=message)
                except OSError:
                    pass
            self._update(state='failed', phase='失敗', error=message)
