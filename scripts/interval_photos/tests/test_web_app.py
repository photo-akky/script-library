import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import timezone
from zoneinfo import ZoneInfo

try:
    import web_app as web
except ModuleNotFoundError as exc:
    if exc.name in {'flask', 'PIL'}:
        raise unittest.SkipTest('Web のテストには requirements.lock のインストールが必要です。')
    raise


class DateTests(unittest.TestCase):
    def test_offset_and_subsecond(self):
        record = {'DateTimeOriginal': '2026:10:11 15:08:17', 'OffsetTimeOriginal': '+09:00', 'SubSecTimeOriginal': '125'}
        date = web.parse_date(record, timezone.utc)
        self.assertEqual(date.isoformat(), '2026-10-11T15:08:17.125000+09:00')

    def test_default_zone(self):
        date = web.parse_date({'DateTimeOriginal': '2026:10:11 15:08:17'}, ZoneInfo('Asia/Tokyo'))
        self.assertEqual(date.isoformat(), '2026-10-11T15:08:17+09:00')

    def test_invalid_date(self):
        for record in [{}, {'DateTimeOriginal': 'wrong'}, {'DateTimeOriginal': '2026:10:11 15:08:17', 'OffsetTimeOriginal': '+99:00'}]:
            with self.assertRaises(ValueError):
                web.parse_date(record, timezone.utc)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / '2026' / '撮影'
        self.folder.mkdir(parents=True)
        self.app = web.create_app({'TESTING': True, 'PHOTO_ROOT': str(self.root), 'SCAN_TOKEN': 'test-token'})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def post_scan(self, **options):
        return self.client.post('/api/scan', json={'folder': '2026/撮影', **options}, headers={'X-Scan-Token': 'test-token'})

    def add_photos(self, seconds):
        records = []
        for number, second in enumerate(seconds):
            path = self.folder / f'{number}.JPG'
            path.write_bytes(b'photo')
            records.append({'SourceFile': str(path), 'DateTimeOriginal': f'2026:10:11 15:08:{second:02d}', 'OffsetTimeOriginal': '+09:00'})
        return records

    def test_home_and_folder_navigation(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('検証後に移動'.encode(), response.data)
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        root = self.client.get('/api/folders').get_json()
        self.assertIsNone(root['parent'])
        self.assertEqual(root['folders'], [{'name': '2026', 'path': '2026'}])
        folder = self.client.get('/api/folders', query_string={'path': '2026/撮影'}).get_json()
        self.assertEqual(folder['parent'], '2026')

    def test_path_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as external:
            (self.root / 'outside').symlink_to(external, target_is_directory=True)
            names = [f['name'] for f in self.client.get('/api/folders').get_json()['folders']]
            self.assertNotIn('outside', names)
            for path in ['../', external, 'outside']:
                self.assertEqual(self.client.get('/api/folders', query_string={'path': path}).status_code, 400)
                self.assertEqual(self.post_scan(folder=path).status_code, 400)

    def test_scan_pairs_and_no_file_changes(self):
        records = self.add_photos([0, 10, 20, 30, 40])
        (self.folder / 'RAW').mkdir()
        (self.folder / 'RAW' / '0.NEF').write_bytes(b'raw')
        (self.folder / '1.ORF').write_bytes(b'raw')
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        with patch.object(web, 'read_exif', return_value=records):
            response = self.post_scan()
        self.assertEqual(response.status_code, 200)
        group = response.get_json()['sets'][0]
        self.assertEqual(group['count'], 5)
        self.assertEqual(group['interval'], 10)
        self.assertEqual(group['paired_count'], 2)
        self.assertEqual(group['files'][0]['raw'], ['RAW/0.NEF'])
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_context_photos_and_time_gaps(self):
        records = self.add_photos([0, 10, 20, 30, 40])
        for name, time in [('before.JPG', '2026:10:11 15:06:00'), ('after.JPG', '2026:10:11 15:10:00')]:
            path = self.folder / name
            path.write_bytes(b'photo')
            records.append({'SourceFile': str(path), 'DateTimeOriginal': time, 'OffsetTimeOriginal': '+09:00'})
        with patch.object(web, 'read_exif', return_value=records):
            result = self.post_scan().get_json()
        group = result['sets'][0]
        self.assertEqual(group['count'], 5)
        self.assertEqual(group['before']['jpg'], 'before.JPG')
        self.assertEqual(group['before']['gap'], 120)
        self.assertEqual(group['after']['jpg'], 'after.JPG')
        self.assertEqual(group['after']['gap'], 80)
        self.assertIn('thumbnail_url', group['before'])
        self.assertIn('preview_url', group['after'])

    def test_no_context_at_folder_edges(self):
        records = self.add_photos([0, 10, 20, 30, 40])
        with patch.object(web, 'read_exif', return_value=records):
            group = self.post_scan().get_json()['sets'][0]
        self.assertIsNone(group['before'])
        self.assertIsNone(group['after'])

    def test_excluded_missing_metadata(self):
        records = self.add_photos([0, 10, 20, 30, 40])
        records[0].pop('DateTimeOriginal')
        records[1].pop('OffsetTimeOriginal')
        with patch.object(web, 'read_exif', return_value=records):
            result = self.post_scan().get_json()
        self.assertEqual(result['excluded'], ['0.JPG'])
        self.assertEqual(result['assumed_zone_count'], 1)
        self.assertEqual(result['sets'], [])

    def test_full_gap_range_through_scan_api(self):
        records = self.add_photos([0, 1, 2, 3, 4])
        times = ['03:55:08', '03:55:27', '03:55:44', '03:56:03', '03:56:20']
        for record, time in zip(records, times):
            record['DateTimeOriginal'] = f'2026:10:10 {time}'
        with patch.object(web, 'read_exif', return_value=records):
            self.assertEqual(self.post_scan(tolerance=1).get_json()['sets'], [])
            response = self.post_scan(tolerance=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()['sets']), 1)

    def test_previous_gap_and_fractional_times(self):
        records = self.add_photos([0, 10, 20, 30, 40, 50])
        # The first photo is outside the set, but remains its predecessor.
        records[0]['DateTimeOriginal'] = '2026:10:11 15:07:00'
        for record in records:
            record['SubSecTimeOriginal'] = '125'
        records[2]['SubSecTimeOriginal'] = '375'
        with patch.object(web, 'read_exif', return_value=records):
            group = self.post_scan().get_json()['sets'][0]
        self.assertEqual(group['files'][0]['time'], '2026-10-11T15:08:10.125000+09:00')
        self.assertEqual([file['previous_gap'] for file in group['files']],
                         [70, 10.25, 9.75, 10, 10])
        self.assertIsNone(group['before']['previous_gap'])

    def test_csrf_and_invalid_options(self):
        self.assertEqual(self.client.post('/api/scan', json={}).status_code, 403)
        for options in [{'min_count': 4}, {'min_count': 5.5}, {'min_interval': True}, {'max_interval': 0}, {'tolerance': 3}, {'tolerance': -1}]:
            self.assertEqual(self.post_scan(**options).status_code, 400)
        self.assertEqual(self.client.post('/api/scan', json=[], headers={'X-Scan-Token': 'test-token'}).status_code, 400)

    def test_exif_failure_reported(self):
        self.add_photos([0])
        with patch.object(web, 'read_exif', side_effect=web.PreviewError('ExifTool が見つかりません。')):
            response = self.post_scan()
        self.assertEqual(response.status_code, 400)
        self.assertIn('ExifTool', response.get_json()['error'])

    def test_empty_folder(self):
        response = self.post_scan()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['jpg_count'], 0)
        self.assertEqual(response.get_json()['sets'], [])

    def test_no_move_or_photo_serving_route(self):
        self.assertEqual(self.client.post('/api/move', json={}).status_code, 404)
        self.assertEqual(self.client.get('/photos/example.JPG').status_code, 404)


class ExifInvocationTests(unittest.TestCase):
    def test_real_arguments_and_partial_error(self):
        records = [{'SourceFile': '/photos/a.JPG', 'Error': 'bad file'}]
        result = type('Result', (), {'stdout': json.dumps(records), 'returncode': 1})()
        with patch.object(web.subprocess, 'run', return_value=result) as run:
            self.assertEqual(web.read_exif([Path('/photos/a.JPG')], 'exiftool'), records)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:3], ['exiftool', '-config', ''])
        self.assertEqual(kwargs['input'], '/photos/a.JPG\n')
        self.assertNotIn('shell', kwargs)

    def test_timeout_and_invalid_output(self):
        with patch.object(web.subprocess, 'run', side_effect=web.subprocess.TimeoutExpired('exiftool', 180)):
            with self.assertRaisesRegex(web.PreviewError, '180秒'):
                web.read_exif([Path('/photos/a.JPG')], 'exiftool')
        result = type('Result', (), {'stdout': 'not json', 'returncode': 1})()
        with patch.object(web.subprocess, 'run', return_value=result):
            with self.assertRaises(web.PreviewError):
                web.read_exif([Path('/photos/a.JPG')], 'exiftool')

    def test_newline_filename_rejected(self):
        with self.assertRaises(web.PreviewError):
            web.read_exif([Path('/photos/new\nline.JPG')], 'exiftool')


if __name__ == '__main__':
    unittest.main()
