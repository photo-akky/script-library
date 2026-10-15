#!/usr/bin/env python3
"""Find interval photo sets by macOS file birth time; preview by default."""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import sys
from datetime import datetime

RAW_EXTENSIONS = {'.3fr', '.ari', '.arw', '.bay', '.braw', '.cap', '.cr2', '.cr3', '.crw', '.dcr', '.dcs', '.dng', '.drf', '.eip', '.erf', '.fff', '.gpr', '.iiq', '.k25', '.kdc', '.mdc', '.mef', '.mos', '.mrw', '.nef', '.nrw', '.obm', '.orf', '.pef', '.ptx', '.pxn', '.r3d', '.raf', '.raw', '.rw2', '.rwl', '.rwz', '.sr2', '.srf', '.srw', '.x3f'}


def birth_time(path):
    value = getattr(path.stat(), 'st_birthtime', None)
    if value is None or not math.isfinite(value) or value <= 0:
        raise ValueError(f'作成日時を取得できません: {path}')
    return value


def detect_sets(photos, minimum=3, maximum=30, tolerance=1, count=5):
    """The range of observed gaps must not exceed the tolerance."""
    ordered = sorted(photos, key=lambda item: (item[1], item[0].name))
    result = []
    i = 0
    while i < len(ordered):
        low, high = float('inf'), float('-inf')
        end = i + 1
        gaps = []
        while end < len(ordered):
            gap = ordered[end][1] - ordered[end - 1][1]
            new_low = min(low, gap)
            new_high = max(high, gap)
            # A missing frame (roughly twice the established interval) splits a set.
            if not minimum <= gap <= maximum or new_high - new_low > tolerance:
                break
            low, high = new_low, new_high
            gaps.append(gap)
            end += 1
        if end - i >= count:
            interval = statistics.median(gaps)
            result.append((ordered[i:end], interval))
            i = end
        else:
            i += 1
    return result


def scan(source, extra_raw):
    entries = list(source.iterdir())
    jpgs = [p for p in entries if p.is_file() and not p.is_symlink() and p.suffix.lower() in {'.jpg', '.jpeg'}]
    photos = [(p, birth_time(p)) for p in jpgs]
    raw_dirs = [source] + [p for p in entries if p.is_dir() and not p.is_symlink() and p.name.lower() == 'raw']
    extensions = RAW_EXTENSIONS | {'.' + ext.lower().lstrip('.') for ext in extra_raw}
    raw = {}
    for directory in raw_dirs:
        for p in directory.iterdir():
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in extensions:
                raw.setdefault(p.stem.casefold(), []).append(p)
    return photos, raw


def make_plan(group, raw):
    plan = []
    for jpg, timestamp in group:
        plan.append((jpg, Path(jpg.name)))
        for partner in raw.get(jpg.stem.casefold(), []):
            # Preserve a separate RAW directory when the source uses one.
            relative = Path('RAW') / partner.name if partner.parent != jpg.parent else Path(partner.name)
            plan.append((partner, relative))
    targets = [str(relative).casefold() for _, relative in plan]
    sources = [str(p) for p, _ in plan]
    if len(set(targets)) != len(targets) or len(set(sources)) != len(sources):
        raise ValueError('ファイル名の衝突、または重複した JPG/RAW ペアがあります。移動を中止します。')
    return plan


def move_set(plan, target):
    # Exclusive directory/file creation never replaces existing destination data.
    target.mkdir(parents=False, exist_ok=False)
    with (target / 'move-log.jsonl').open('x', encoding='utf-8') as log:
        def record(event, src, dst):
            log.write(json.dumps({'event': event, 'source': str(src), 'destination': str(dst)}, ensure_ascii=False) + '\n')
            log.flush()
            os.fsync(log.fileno())
        for src, relative in plan:
            dst = target / relative
            dst.parent.mkdir(exist_ok=True)
            record('copy_started', src, dst)
            before = src.stat()
            with src.open('rb') as reader, dst.open('xb') as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            after = src.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or dst.stat().st_size != before.st_size:
                raise RuntimeError(f'コピー中にファイルが変化しました。元ファイルを保持します: {src}')
            shutil.copystat(src, dst)
            record('copy_completed', src, dst)
            src.unlink()
            record('source_removed', src, dst)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Mac/NAS 写真を作成日時で分類。既定は候補表示のみ。')
    parser.add_argument('source', type=Path)
    parser.add_argument('--min-interval', type=float, default=3)
    parser.add_argument('--max-interval', type=float, default=30)
    parser.add_argument('--tolerance', type=float, default=1, help='間隔の最大値と最小値の差（秒）')
    parser.add_argument('--min-count', type=int, default=5)
    parser.add_argument('--raw-ext', action='append', default=[], help='追加の RAW 拡張子。複数指定可')
    parser.add_argument('--move', action='store_true')
    parser.add_argument('--sets', help='移動する候補番号。例: 1,3 または all')
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args(argv)
    values = (args.min_interval, args.max_interval, args.tolerance)
    if not all(math.isfinite(v) for v in values) or not 0 < args.min_interval <= args.max_interval or not 0 <= args.tolerance < args.min_interval or args.min_count < 2:
        parser.error('間隔は正の数、許容幅は0以上かつ最小間隔未満、最小枚数は2以上にしてください。')
    if args.move and (not args.sets or not args.destination):
        parser.error('--move には --sets と --destination が必要です。')
    if not args.move and (args.sets or args.destination):
        parser.error('--sets と --destination は --move と一緒に指定してください。')
    source = args.source.expanduser().resolve(strict=True)
    if not source.is_dir():
        parser.error('source はフォルダを指定してください。')
    photos, raw = scan(source, args.raw_ext)
    groups = detect_sets(photos, *values, args.min_count)
    print(f'JPG: {len(photos)} 枚 / 候補: {len(groups)} セット')
    for number, (group, interval) in enumerate(groups, 1):
        matched = sum(bool(raw.get(p.stem.casefold())) for p, _ in group)
        stamp = datetime.fromtimestamp(group[0][1]).isoformat(sep=' ', timespec='seconds')
        print(f'[{number}] {stamp} / {len(group)} 枚 / 約{interval:.2f}秒 / RAWあり {matched}枚')
        for p, timestamp in group:
            partners = ', '.join(str(r.relative_to(source)) for r in raw.get(p.stem.casefold(), [])) or 'RAWなし'
            print(f'  {p.name} -> {partners}')
    if not args.move:
        print('候補表示のみ。ファイルは変更していません。')
        return
    selected = set(range(1, len(groups) + 1)) if args.sets == 'all' else {int(n) for n in args.sets.split(',')}
    if not selected or not selected <= set(range(1, len(groups) + 1)):
        parser.error('存在する候補番号を指定してください。')
    destination = args.destination.expanduser().resolve()
    if destination == source or source.is_relative_to(destination):
        parser.error('移動先は元フォルダ自身やその親フォルダ以外にしてください。')
    batch = []
    for number in sorted(selected):
        group, interval = groups[number - 1]
        stamp = datetime.fromtimestamp(group[0][1]).strftime('%Y%m%d_%H%M%S')
        target = destination / f'interval_{stamp}_{number:03d}'
        if target.exists():
            raise FileExistsError(f'移動先が既に存在します: {target}')
        batch.append((make_plan(group, raw), target))
    destination.mkdir(parents=True, exist_ok=True)
    for plan, target in batch:
        move_set(plan, target)
        print(f'移動完了: {target}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'エラー: {exc}\n移動途中の場合は move-log.jsonl と元・移動先を確認してください。自動再実行はしないでください。', file=sys.stderr)
        sys.exit(1)
