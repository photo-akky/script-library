from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from copy_jobs import CopyError, CopyManager
    from web_app import create_app
except ModuleNotFoundError as exc:
    if exc.name in {'PIL', 'flask'}:
        raise unittest.SkipTest('Web の依存関係をインストールしてください。')
    raise


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'photos'; self.source.mkdir()
        self.folder = self.source / 'shoot'; self.folder.mkdir()
        self.destination = self.root / 'output'; self.destination.mkdir()
        (self.folder / 'RAW').mkdir()
        (self.folder / 'one.JPG').write_bytes(b'jpeg original')
        (self.folder / 'RAW' / 'one.NEF').write_bytes(b'raw original')
        (self.folder / 'before.JPG').write_bytes(b'context only')
        self.group = {'number': 1, 'start': '2026-10-11T15:08:00+09:00',
                      'files': [{'jpg': 'one.JPG', 'raw': ['RAW/one.NEF']}],
                      'before': {'jpg': 'before.JPG'}, 'after': None}
        self.manager = CopyManager(self.source, self.destination)
        self.scan = self.manager.register(self.folder, [self.group])
        self.before = {p.relative_to(self.source).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                       for p in self.source.rglob('*') if p.is_file()}

    def tearDown(self):
        self.tmp.cleanup()

    def wait_job(self, manager=None):
        manager = manager or self.manager
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = manager.status()
            if job['state'] != 'running': return job
            time.sleep(.01)
        self.fail('copy worker did not finish')

    def test_verified_jpg_raw_copy_preserves_sources_and_excludes_context(self):
        self.manager.start(self.scan, [1])
        job = self.wait_job()
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual(job['files_done'], 2)
        target = self.destination / self.group['target_folder']
        self.assertEqual((target / 'one.JPG').read_bytes(), b'jpeg original')
        self.assertEqual((target / 'RAW/one.NEF').read_bytes(), b'raw original')
        self.assertFalse((target / 'before.JPG').exists())
        self.assertFalse((target / 'COPY_INCOMPLETE').exists())
        records = [json.loads(line) for line in (target / 'copy-log.jsonl').read_text().splitlines()]
        verified = [record for record in records if record['event'] == 'file_verified']
        self.assertEqual(len(verified), 2)
        self.assertEqual(verified[0]['sha256'], hashlib.sha256(b'jpeg original').hexdigest())
        after = {p.relative_to(self.source).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.source.rglob('*') if p.is_file()}
        self.assertEqual(self.before, after)
        self.assertEqual((target / 'one.JPG').stat().st_mtime_ns, self.before['shoot/one.JPG'][1])

    def nested_source(self):
        nested = self.source / '2026' / '10' / '11'
        nested.parent.mkdir(parents=True)
        self.folder.rename(nested)
        self.folder = nested
        return self.manager.register(self.folder, [self.group])

    def test_nested_structure_and_raw_subfolder(self):
        token = self.nested_source()
        self.assertEqual(self.group['target_folder'], '2026/10/11-1')
        self.manager.start(token, [1])
        job = self.wait_job()
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual(job['destinations'], ['2026/10/11-1'])
        target = self.destination / '2026/10/11-1'
        self.assertEqual((target/'one.JPG').read_bytes(), b'jpeg original')
        self.assertEqual((target/'RAW/one.NEF').read_bytes(), b'raw original')
        self.assertTrue((self.folder/'one.JPG').exists())

    def test_root_selection_uses_root_suffix(self):
        (self.source/'root.JPG').write_bytes(b'root photo')
        group = {'number':2,'files':[{'jpg':'root.JPG','raw':[]}]}
        token = self.manager.register(self.source,[group])
        self.assertEqual(group['target_folder'],'root-2')
        self.manager.start(token,[2])
        self.assertEqual(self.wait_job()['state'],'completed')
        self.assertTrue((self.destination/'root-2/root.JPG').is_file())

    def test_nested_parent_symlink_is_rejected(self):
        token = self.nested_source()
        outside = self.root/'outside'; outside.mkdir()
        (self.destination/'2026').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(CopyError,'シンボリックリンク'):
            self.manager.start(token,[1])
        self.assertEqual(list(outside.iterdir()),[])

    def test_nested_parent_file_is_rejected(self):
        token = self.nested_source()
        (self.destination/'2026').write_bytes(b'keep')
        with self.assertRaisesRegex(CopyError,'親階層'):
            self.manager.start(token,[1])
        self.assertEqual((self.destination/'2026').read_bytes(),b'keep')

    def test_parent_creation_failure_cleans_only_created_empty_folders(self):
        token = self.nested_source()
        parent = self.destination/'2026'; parent.mkdir()
        original = Path.mkdir
        def mkdir(path,*args,**kwargs):
            if path.name=='11-1': raise PermissionError('test denial')
            return original(path,*args,**kwargs)
        with patch.object(Path,'mkdir',mkdir):
            with self.assertRaises(PermissionError): self.manager.start(token,[1])
        self.assertTrue(parent.is_dir())
        self.assertEqual(list(parent.iterdir()),[])

    def test_existing_destination_is_never_overwritten(self):
        target = self.destination / self.group['target_folder']; target.mkdir()
        (target / 'one.JPG').write_bytes(b'existing')
        with self.assertRaisesRegex(CopyError, '既に存在'):
            self.manager.start(self.scan, [1])
        self.assertEqual((target / 'one.JPG').read_bytes(), b'existing')

    def test_changed_source_rejected_before_creating_folders(self):
        (self.folder / 'one.JPG').write_bytes(b'changed original')
        with self.assertRaisesRegex(CopyError, '変更'):
            self.manager.start(self.scan, [1])
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_symlink_or_parent_path_cannot_be_copied(self):
        (self.folder / 'link.JPG').symlink_to(self.folder / 'one.JPG')
        for name in ['link.JPG', '../outside.JPG']:
            group = {**self.group, 'files': [{'jpg': name, 'raw': []}]}
            token = self.manager.register(self.folder, [group])
            self.assertIn('copy_error', group)
            with self.assertRaises(CopyError): self.manager.start(token, [1])

    def test_duplicate_raw_pairs_are_flagged(self):
        group = {**self.group, 'files': self.group['files'] * 2}
        self.manager.register(self.folder, [group])
        self.assertIn('copy_error', group)

    def test_unknown_selection_and_expiry(self):
        for selection in [[], [True], [2], [1,1], '../shoot']:
            with self.assertRaises(CopyError): self.manager.start(self.scan, selection)
        with self.assertRaises(CopyError): self.manager.start('forged', [1])
        with patch('copy_jobs.time.monotonic', return_value=time.monotonic()+3601):
            with self.assertRaisesRegex(CopyError, '期限切れ'): self.manager.start(self.scan, [1])

    def test_failure_leaves_marker_log_and_originals(self):
        with patch.object(self.manager, '_copy_file', side_effect=OSError('disk full')):
            self.manager.start(self.scan, [1])
            job = self.wait_job()
        self.assertEqual(job['state'], 'failed')
        target = self.destination / self.group['target_folder']
        self.assertTrue((target / 'COPY_INCOMPLETE').exists())
        self.assertIn('job_failed', (target / 'copy-log.jsonl').read_text())
        self.assertTrue((self.folder / 'one.JPG').exists())

    def test_checksum_mismatch_never_publishes_final_file(self):
        real_open = Path.open
        def opened(path, mode='r', *args, **kwargs):
            if path.suffix == '.part' and mode == 'rb': return BytesIO(b'corrupt readback')
            return real_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', opened):
            self.manager.start(self.scan, [1])
            job = self.wait_job()
        self.assertEqual(job['state'], 'failed')
        target = self.destination / self.group['target_folder']
        self.assertFalse((target / 'one.JPG').exists())
        self.assertTrue((target / 'one.JPG.part').exists())
        self.assertTrue((self.folder / 'one.JPG').exists())

    def test_only_selected_set_is_copied(self):
        (self.folder / 'two.JPG').write_bytes(b'second set')
        second = {'number':2, 'start':'2026-10-11T16:00:00+09:00', 'files':[{'jpg':'two.JPG','raw':[]}]}
        token = self.manager.register(self.folder, [self.group, second])
        self.manager.start(token,[2])
        self.assertEqual(self.wait_job()['state'],'completed')
        self.assertFalse((self.destination/self.group['target_folder']).exists())
        self.assertEqual((self.destination/second['target_folder']/'two.JPG').read_bytes(),b'second set')

    def test_file_appearing_during_copy_is_not_overwritten(self):
        original = self.manager._copy_file
        def raced(file, target, log):
            target.write_bytes(b'other writer')
            original(file,target,log)
        with patch.object(self.manager,'_copy_file',side_effect=raced):
            self.manager.start(self.scan,[1])
            self.assertEqual(self.wait_job()['state'],'failed')
        target=self.destination/self.group['target_folder']/'one.JPG'
        self.assertEqual(target.read_bytes(),b'other writer')
        self.assertEqual((self.folder/'one.JPG').read_bytes(),b'jpeg original')

    def test_source_changed_after_job_start_is_rejected(self):
        original = self.manager._copy_file
        def changed(file,target,log):
            file.source.write_bytes(b'changed by external application')
            original(file,target,log)
        with patch.object(self.manager,'_copy_file',side_effect=changed):
            self.manager.start(self.scan,[1])
            job=self.wait_job()
        self.assertEqual(job['state'],'failed')
        self.assertFalse((self.destination/self.group['target_folder']/'one.JPG').exists())
        self.assertTrue((self.folder/'one.JPG').exists())

    def test_one_worker_at_a_time(self):
        entered, release = threading.Event(), threading.Event()
        original = self.manager._copy_file
        def blocked(*args):
            entered.set()
            release.wait(3)
            original(*args)
        with patch.object(self.manager, '_copy_file', side_effect=blocked):
            self.manager.start(self.scan, [1])
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(CopyError, '実行中'): self.manager.start(self.scan, [1])
            finally: release.set()
            self.assertEqual(self.wait_job()['state'], 'completed')

    def test_empty_space_or_destination_configuration(self):
        with patch('copy_jobs.shutil.disk_usage', return_value=type('Usage', (), {'free':0})()):
            with self.assertRaisesRegex(CopyError, '容量'): self.manager.start(self.scan,[1])
        manager = CopyManager(self.source, None)
        token = manager.register(self.folder, [self.group])
        with self.assertRaisesRegex(CopyError, '未設定'): manager.start(token,[1])

    def test_copy_api_requires_csrf_and_server_scan(self):
        app = create_app({'TESTING':True,'PHOTO_ROOT':str(self.source),'COPY_ROOT':str(self.destination),'SCAN_TOKEN':'test'})
        client = app.test_client()
        self.assertEqual(client.post('/api/copy',json={}).status_code,403)
        headers = {'X-Scan-Token':'test'}
        self.assertEqual(client.post('/api/copy',json={'scan_id':'forged','sets':[1]},headers=headers).status_code,400)
        manager=app.extensions['copy_manager']
        token=manager.register(self.folder,[self.group])
        response=client.post('/api/copy',json={'scan_id':token,'sets':[1]},headers=headers)
        self.assertEqual(response.status_code,202)
        self.assertEqual(self.wait_job(manager)['state'],'completed')
        data=client.get('/api/copy/status').get_json()
        self.assertTrue(data['copy_enabled'])
        self.assertEqual(data['job']['files_done'],2)
        self.assertEqual(client.post('/api/move',json={}).status_code,404)


if __name__ == '__main__': unittest.main()
