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


def destination_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise CopyError('コピー先のパスが不正です。')
    path = root
    for index, part in enumerate(relative.parts):
        path = path / part
        if path.is_symlink():
            raise CopyError('コピー先の階層にシンボリックリンクがあります。')
        if index < len(relative.parts) - 1 and path.exists() and not path.is_dir():
            raise CopyError('コピー先の親階層に同名のファイルがあります。')
    if not path.resolve().is_relative_to(root):
        raise CopyError('コピー先ルート外には保存できません。')
    return path


def remove_empty_created(paths):
    for path in reversed(paths):
        try:
            path.rmdir()
        except OSError:
            pass  # Never remove files or directories populated by another process.


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
    def __init__(self, source_root, destination_root, allow_move=False):
        self.source_root = Path(source_root).resolve()
        self.destination_root = Path(destination_root).resolve() if destination_root else None
        self.allow_move = allow_move
        self.lock = threading.Lock()
        self.scans = OrderedDict()
        self.job = None

    def register(self, folder, groups):
        plans = {}
        relative_folder = Path(folder).resolve().relative_to(self.source_root)
        for group in groups:
            files, targets, sources = [], set(), set()
            target_folder = relative_folder.with_name(f'{relative_folder.name}-{group["number"]}') if relative_folder.parts else Path(f'root-{group["number"]}')
            group['target_folder'] = target_folder.as_posix()
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

    def start(self, scan_id, selected, operation="copy"):
        if operation not in ('copy', 'move'):
            raise CopyError('処理方法が不正です。')
        if operation == 'move' and not self.allow_move:
            raise CopyError('移動が有効になっていません。設定と写真ルートの書き込み権限を確認してください。')
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
            existing = set()
            for group in sets:
                destination = destination_path(root, group.name)
                if destination.exists():
                    if operation != 'move':
                        raise CopyError(f'コピー先が既に存在します: {group.name}。上書きはしません。')
                    if not destination.is_dir():
                        raise CopyError('保存先に同名のファイルがあります。')
                    if any((destination / name).exists() for name in ('COPY_INCOMPLETE', 'MOVE_INCOMPLETE', 'move-log.jsonl')):
                        raise CopyError('保存先に未完了または以前の移動記録があります。先にログとファイルを確認してください。')
                    for file in group.files:
                        target = destination_path(root, Path(group.name) / file.target)
                        if not target.is_file():
                            raise CopyError(f'既存の保存先に必要なファイルがありません: {file.target}')
                    existing.add(group.name)
                for file in group.files:
                    if file.source in all_sources:
                        raise CopyError('選択したセット間で同じファイルが重複しています。')
                    all_sources.add(file.source)
                    path = checked_source(self.source_root, file.source.relative_to(self.source_root))
                    if operation == 'move' and not os.access(path.parent, os.W_OK | os.X_OK):
                        raise CopyError('元フォルダから削除する権限がありません。')
                    if tuple(file_version(path)) != file.version:
                        raise CopyError(f'解析後に元ファイルが変更されました: {path.name}。再解析してください。')
            total_bytes = sum(file.version[2] for group in sets for file in group.files)
            new_bytes = sum(file.version[2] for group in sets if group.name not in existing for file in group.files)
            if shutil.disk_usage(root).free < new_bytes:
                raise CopyError('コピー先の空き容量が不足しています。')
            # Reserve every set directory exclusively before starting work.
            created = []
            try:
                for group in sets:
                    if group.name in existing:
                        continue
                    path = destination_path(root, group.name)
                    parent = root
                    for part in Path(group.name).parent.parts:
                        parent = parent / part
                        if not parent.exists():
                            parent.mkdir(exist_ok=False)
                            created.append(parent)
                    path = destination_path(root, group.name)
                    path.mkdir(exist_ok=False)
                    created.append(path)
            except (OSError, CopyError):
                remove_empty_created(created)
                raise
            self.job = {'id': secrets.token_urlsafe(18), 'operation': operation, 'state': 'running', 'phase': '準備',
                        'files_done': 0, 'files_total': sum(len(group.files) for group in sets),
                        'bytes_copied': 0, 'bytes_reused': 0, 'sources_removed': 0, 'bytes_total': total_bytes, 'current_file': '',
                        'destinations': [group.name for group in sets], 'error': None}
            job = dict(self.job)
            worker = threading.Thread(target=self._run, args=(sets, operation, existing), daemon=True)
            try:
                worker.start()
            except Exception:
                self.job = None
                remove_empty_created(created)
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
        return digest.hexdigest()

    @staticmethod
    def _hash_checked(path, expected=None):
        version = tuple(file_version(path))
        if expected is not None and version != expected:
            raise CopyError('ファイルが変更されました。元ファイルの削除を停止します。')
        digest = hashlib.sha256()
        with path.open('rb') as reader:
            info = os.fstat(reader.fileno())
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != version:
                raise CopyError('ファイルが置き換えられました。')
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(reader.fileno())
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != version:
                raise CopyError('照合中にファイルが変更されました。')
        if tuple(file_version(path)) != version:
            raise CopyError('照合中にファイルが置き換えられました。')
        return digest.hexdigest(), version

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _verify_move(self, group, file, digest, target_version):
        source = checked_source(self.source_root, file.source.relative_to(self.source_root))
        target = destination_path(self.destination_root, Path(group.name) / file.target)
        source_digest, _ = self._hash_checked(source, file.version)
        target_digest, _ = self._hash_checked(target, target_version)
        if source_digest != digest or target_digest != digest:
            raise CopyError('元データと保存先の内容が一致しません。元ファイルは削除しません。')
        return source, target

    def _run(self, sets, operation='copy', existing=None):
        root = self.destination_root
        existing = existing or set()
        moving = operation == 'move'
        marker = 'MOVE_INCOMPLETE' if moving else 'COPY_INCOMPLETE'
        log_name = 'move-log.jsonl' if moving else 'copy-log.jsonl'
        verified = []
        try:
            for group in sets:
                folder = root / group.name
                with (folder / marker).open('x', encoding='utf-8') as flag:
                    flag.write('処理未完了。ログと元・保存先のファイルを確認してください。\n')
                    flag.flush()
                    os.fsync(flag.fileno())
                with (folder / log_name).open('x', encoding='utf-8') as log:
                    self._record(log, 'set_started', operation=operation, reuse=group.name in existing,
                                 files=[file.target.as_posix() for file in group.files])
            for group in sets:
                folder = root / group.name
                with (folder / log_name).open('a', encoding='utf-8') as log:
                    for file in group.files:
                        target = destination_path(root, Path(group.name) / file.target)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if group.name in existing:
                            self._update(phase='既存の保存先を照合中', current_file=file.target.as_posix())
                            source = checked_source(self.source_root, file.source.relative_to(self.source_root))
                            digest, _ = self._hash_checked(source, file.version)
                            target_digest, target_version = self._hash_checked(target)
                            if digest != target_digest:
                                raise CopyError('既存の保存先と内容が一致しません。元ファイルは削除しません。')
                            self._record(log, 'file_verified', source=source.relative_to(self.source_root).as_posix(),
                                         target=file.target.as_posix(), sha256=digest, reused=True)
                            with self.lock:
                                self.job['files_done'] += 1
                                self.job['bytes_reused'] += file.version[2]
                        else:
                            digest = self._copy_file(file, target, log)
                            target_version = tuple(file_version(target))
                        if moving:
                            verified.append((group, file, digest, target_version))
                    if not moving:
                        self._record(log, 'set_completed')
                if not moving:
                    (folder / marker).unlink()
            if moving:
                # Verify ALL selected pairs before the first source deletion.
                self._update(phase='削除前に全件を再照合中')
                for group, file, digest, target_version in verified:
                    self._update(current_file=(Path(group.name) / file.target).as_posix())
                    self._verify_move(group, file, digest, target_version)
                    target = root / group.name / file.target
                    with target.open('rb') as data:
                        os.fsync(data.fileno())
                    directory = target.parent
                    while directory.is_relative_to(root):
                        self._sync_directory(directory)
                        if directory == root:
                            break
                        directory = directory.parent
                for group in sets:
                    with (root / group.name / log_name).open('a', encoding='utf-8') as log:
                        self._record(log, 'all_selected_files_verified', sets=[item.name for item in sets])
                for group, file, digest, target_version in verified:
                    self._update(phase='検証済みの元ファイルを削除中', current_file=file.target.as_posix())
                    source, target = self._verify_move(group, file, digest, target_version)
                    with (root / group.name / log_name).open('a', encoding='utf-8') as log:
                        relative = source.relative_to(self.source_root).as_posix()
                        self._record(log, 'source_delete_started', source=relative, target=file.target.as_posix(), sha256=digest)
                        # Recheck identity after logging and immediately before unlink.
                        checked_source(self.source_root, source.relative_to(self.source_root))
                        destination_path(root, Path(group.name) / file.target)
                        if tuple(file_version(source)) != file.version or tuple(file_version(target)) != target_version:
                            raise CopyError('削除直前にファイルが変更されました。処理を停止します。')
                        source.unlink()
                        with self.lock:
                            self.job['sources_removed'] += 1
                        self._sync_directory(source.parent)
                        self._record(log, 'source_removed', source=relative, target=file.target.as_posix(), sha256=digest)
                for group in sets:
                    folder = root / group.name
                    with (folder / log_name).open('a', encoding='utf-8') as log:
                        self._record(log, 'set_completed')
                    (folder / marker).unlink()
                    self._sync_directory(folder)
            self._update(state='completed', phase='完了', current_file='')
        except Exception as error:
            message = str(error)
            for group in sets:
                log_path = root / group.name / log_name
                try:
                    if (log_path.parent / marker).exists():
                        with log_path.open('a', encoding='utf-8') as log:
                            self._record(log, 'job_failed', error=message)
                except OSError:
                    pass
            self._update(state='failed', phase='失敗', error=message)
