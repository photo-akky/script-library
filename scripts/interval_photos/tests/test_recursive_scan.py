import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from PIL import Image
    import web_app as web
except ModuleNotFoundError as exc:
    if exc.name in {'flask', 'PIL'}:
        raise unittest.SkipTest('Web のテストには requirements.lock のインストールが必要です。')
    raise


class RecursiveScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / 'photos'
        self.folder = self.root / '2026'
        self.folder.mkdir(parents=True)
        self.destination = base / 'destination'
        self.destination.mkdir()
        self.records = {}
        self.app = web.create_app({'TESTING': True, 'PHOTO_ROOT': str(self.root),
                                   'COPY_ROOT': str(self.destination), 'ALLOW_MOVE': True,
                                   'PREVIEW_CACHE': str(base / 'cache'), 'SCAN_TOKEN': 'test'})
        self.client = self.app.test_client()
        self.headers = {'X-Scan-Token': 'test'}
        self.manager = self.app.extensions['copy_manager']

    def tearDown(self):
        self.tmp.cleanup()

    def photos(self, folder, count=5, raw=False):
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            path = folder / f'{i}.JPG'
            Image.new('RGB', (8, 8), (i * 20, 30, 40)).save(path)
            self.records[str(path)] = {'SourceFile': str(path),
                                      'DateTimeOriginal': f'2026:10:11 15:08:{i * 10:02d}',
                                      'OffsetTimeOriginal': '+09:00'}
            if raw:
                (folder / 'RAW').mkdir(exist_ok=True)
                (folder / 'RAW' / f'{i}.NEF').write_bytes(f'{folder.name}-{i}'.encode())

    def scan(self, **options):
        with patch.object(web, 'read_exif', side_effect=lambda paths, _: [self.records[str(p)] for p in paths]):
            return self.client.post('/api/scan', json={'folder': '2026', **options}, headers=self.headers)

    def finish(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.manager.status()
            if job['state'] != 'running':
                return job
            time.sleep(.01)
        self.fail('worker did not finish')

    def test_direct_scan_remains_default(self):
        self.photos(self.folder)
        self.photos(self.folder / 'child')
        data = self.scan().get_json()
        self.assertFalse(data['recursive'])
        self.assertEqual(data['folder_count'], 1)
        self.assertEqual(data['jpg_count'], 5)
        self.assertEqual(len(data['sets']), 1)

    def test_recursive_scan_nested_folders_and_local_numbering(self):
        for folder in [self.folder, self.folder / 'a', self.folder / 'b' / 'deep']:
            self.photos(folder, raw=True)
        data = self.scan(recursive=True).get_json()
        self.assertEqual(data['jpg_count'], 15)
        self.assertEqual([g['number'] for g in data['sets']], [1, 2, 3])
        self.assertEqual([g['folder_number'] for g in data['sets']], [1, 1, 1])
        self.assertEqual([g['target_folder'] for g in data['sets']],
                         ['2026-1', '2026/a-1', '2026/b/deep-1'])
        for group in data['sets']:
            self.assertIsNone(group['before'])
            self.assertIsNone(group['after'])
            self.assertIsNone(group['files'][0]['previous_gap'])
            response = self.client.get(group['files'][0]['thumbnail_url'])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'image/jpeg')

    def test_recursive_copy_and_move_preserve_each_folder_and_raw(self):
        for folder in [self.folder / 'a', self.folder / 'b']:
            self.photos(folder, raw=True)
        data = self.scan(recursive=True).get_json()
        selected = [g['number'] for g in data['sets']]
        self.manager.start(data['scan_id'], selected)
        self.assertEqual(self.finish()['state'], 'completed')
        for group in data['sets']:
            for photo in group['files']:
                for name in [photo['jpg'], *photo['raw']]:
                    source = self.root / group['folder'] / name
                    self.assertEqual(source.read_bytes(), (self.destination / group['target_folder'] / name).read_bytes())
        self.manager.start(data['scan_id'], selected, 'move')
        job = self.finish()
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual(job['bytes_copied'], 0)
        self.assertEqual(job['sources_removed'], 20)
        for group in data['sets']:
            self.assertFalse((self.root / group['folder'] / '0.JPG').exists())
            self.assertTrue((self.destination / group['target_folder'] / 'RAW/0.NEF').exists())

    def test_short_sequences_in_separate_folders_do_not_merge(self):
        self.photos(self.folder / 'a', count=3)
        self.photos(self.folder / 'b', count=3)
        data = self.scan(recursive=True).get_json()
        self.assertEqual(data['jpg_count'], 6)
        self.assertEqual(data['sets'], [])

    def test_hidden_and_symlink_directories_are_skipped(self):
        self.photos(self.folder / '.hidden')
        self.photos(self.root / 'outside')
        (self.folder / 'link').symlink_to(self.root / 'outside', target_is_directory=True)
        (self.folder / 'loop').symlink_to(self.folder, target_is_directory=True)
        data = self.scan(recursive=True).get_json()
        self.assertEqual(data['folder_count'], 1)
        self.assertEqual(data['jpg_count'], 0)

    def test_exclusions_keep_relative_folder_paths(self):
        self.photos(self.folder / 'a')
        self.records[str(self.folder / 'a/0.JPG')].pop('DateTimeOriginal')
        data = self.scan(recursive=True).get_json()
        self.assertEqual(data['excluded'], ['a/0.JPG'])
        self.assertEqual(data['sets'], [])

    def test_recursive_flag_requires_boolean(self):
        for value in ['true', 1, None, []]:
            self.assertEqual(self.scan(recursive=value).status_code, 400)

    def test_failure_does_not_register_partial_results(self):
        self.photos(self.folder)
        self.photos(self.folder / 'child')
        original = web.scan_folder
        def scan(folder, *args):
            if folder.name == 'child':
                raise PermissionError('denied')
            return original(folder, *args)
        with patch.object(web, 'scan_folder', side_effect=scan):
            self.assertEqual(self.scan(recursive=True).status_code, 400)
        self.assertEqual(len(self.manager.scans), 0)
