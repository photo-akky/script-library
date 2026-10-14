import tempfile
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch
import interval_photos as app


class Tests(unittest.TestCase):
    def photos(self, times):
        return [(Path(f'{i}.jpg'), t) for i, t in enumerate(times)]

    def test_five_frames_and_jitter(self):
        groups = app.detect_sets(self.photos([100, 107, 115, 122, 130]))
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0][0]), 5)

    def test_missing_frame_splits(self):
        groups = app.detect_sets(self.photos([100, 110, 120, 130, 140, 160, 170, 180, 190, 200]))
        self.assertEqual([len(g[0]) for g in groups], [5, 5])

    def test_short_and_equal_times(self):
        self.assertEqual(app.detect_sets(self.photos([100] * 8)), [])
        self.assertEqual(app.detect_sets(self.photos([100, 110, 120, 130])), [])

    def test_drift_does_not_chain(self):
        self.assertEqual(app.detect_sets(self.photos([100, 105, 111, 118, 126])), [])

    def test_missing_birthtime(self):
        with patch.object(Path, 'stat', return_value=object()):
            with self.assertRaisesRegex(ValueError, '作成日時'):
                app.birth_time(Path('image.jpg'))

    def test_pairing_and_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'RAW').mkdir()
            jpg = root / 'IMG.JPG'
            raw = root / 'RAW' / 'img.NEF'
            jpg.write_bytes(b'jpeg data')
            raw.write_bytes(b'raw data')
            with patch.object(app, 'birth_time', return_value=100):
                photos, partners = app.scan(root, [])
            plan = app.make_plan(photos, partners)
            target = root / 'out'
            app.move_set(plan, target)
            self.assertEqual((target / 'IMG.JPG').read_bytes(), b'jpeg data')
            self.assertEqual((target / 'RAW' / 'img.NEF').read_bytes(), b'raw data')
            self.assertFalse(jpg.exists())
            self.assertFalse(raw.exists())
            self.assertEqual(len((target / 'move-log.jsonl').read_text().splitlines()), 6)
            with self.assertRaises(FileExistsError):
                app.move_set([], target)

    def test_preview_leaves_files_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(5):
                (root / f'{i}.jpg').write_bytes(b'photo')
            before = sorted(p.name for p in root.iterdir())
            output = io.StringIO()
            with patch.object(app, 'birth_time', side_effect=lambda p: 100 + int(p.stem) * 10):
                with contextlib.redirect_stdout(output):
                    app.main([str(root)])
            self.assertIn('候補: 1 セット', output.getvalue())
            self.assertEqual(before, sorted(p.name for p in root.iterdir()))

    def test_copy_failure_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'a.jpg'
            src.write_bytes(b'original')
            with patch.object(app.shutil, 'copyfileobj', side_effect=OSError('network lost')):
                with self.assertRaises(OSError):
                    app.move_set([(src, Path('a.jpg'))], root / 'out')
            self.assertEqual(src.read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
