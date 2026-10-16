import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from copy_jobs import CopyError, CopyManager
    from web_app import create_app
except ModuleNotFoundError as exc:
    if exc.name in {'PIL','flask'}: raise unittest.SkipTest('Web の依存関係をインストールしてください。')
    raise


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.source=root/'photos'; self.source.mkdir()
        self.folder=self.source/'2026'/'10'/'11'; self.folder.mkdir(parents=True)
        (self.folder/'RAW').mkdir()
        self.contents={'one.JPG':b'jpeg original','RAW/one.NEF':b'raw original'}
        for name,data in self.contents.items(): (self.folder/name).write_bytes(data)
        (self.folder/'before.JPG').write_bytes(b'context')
        self.destination=root/'destination'; self.destination.mkdir()
        self.manager=CopyManager(self.source,self.destination,allow_move=True)
        self.group={'number':1,'files':[{'jpg':'one.JPG','raw':['RAW/one.NEF']}]}
        self.scan=self.manager.register(self.folder,[self.group])
        self.target=self.destination/'2026/10/11-1'

    def tearDown(self): self.tmp.cleanup()

    def wait(self):
        end=time.monotonic()+5
        while time.monotonic()<end:
            job=self.manager.status()
            if job['state']!='running': return job
            time.sleep(.01)
        self.fail('worker did not finish')

    def run_move(self):
        self.manager.start(self.scan,[1],'move')
        return self.wait()

    def assert_originals(self):
        for name,data in self.contents.items(): self.assertEqual((self.folder/name).read_bytes(),data)

    def assert_targets(self):
        for name,data in self.contents.items(): self.assertEqual((self.target/name).read_bytes(),data)

    def make_copy(self):
        self.manager.start(self.scan,[1])
        self.assertEqual(self.wait()['state'],'completed')
        self.assert_originals()

    def test_move_jpg_raw_after_verification_and_keep_context(self):
        job=self.run_move()
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['sources_removed'],2)
        self.assertEqual(job['files_done'],2)
        self.assert_targets()
        for name in self.contents: self.assertFalse((self.folder/name).exists())
        self.assertEqual((self.folder/'before.JPG').read_bytes(),b'context')
        self.assertFalse((self.target/'MOVE_INCOMPLETE').exists())
        records=[json.loads(line) for line in (self.target/'move-log.jsonl').read_text().splitlines()]
        self.assertEqual(len([r for r in records if r['event']=='source_removed']),2)
        self.assertLess(max(i for i,r in enumerate(records) if r['event']=='file_verified'),min(i for i,r in enumerate(records) if r['event']=='source_delete_started'))

    def test_existing_verified_copy_is_reused_without_overwrite(self):
        self.make_copy()
        copy_log=(self.target/'copy-log.jsonl').read_bytes()
        (self.target/'notes.txt').write_bytes(b'keep extra file')
        with patch.object(self.manager,'_copy_file',side_effect=AssertionError('unexpected recopy')):
            job=self.run_move()
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['bytes_copied'],0)
        self.assertEqual(job['bytes_reused'],sum(map(len,self.contents.values())))
        self.assertEqual(job['sources_removed'],2)
        self.assertEqual((self.target/'copy-log.jsonl').read_bytes(),copy_log)
        self.assertEqual((self.target/'notes.txt').read_bytes(),b'keep extra file')
        self.assert_targets()

    def shift_copy(self, number=7):
        self.make_copy()
        shifted = self.target.with_name(f'11-{number}')
        self.target.rename(shifted)
        return shifted

    def test_renumbered_copy_is_reused_with_bounded_search(self):
        shifted = self.shift_copy()
        # A matching-looking directory elsewhere must never be scanned.
        unrelated = self.destination / 'elsewhere'
        unrelated.mkdir()
        visited = []
        original = Path.iterdir
        def listing(path):
            visited.append(path)
            if path != shifted.parent:
                raise AssertionError(f'Unexpected directory scan: {path}')
            return original(path)
        with patch.object(Path, 'iterdir', listing), patch.object(self.manager, '_copy_file', side_effect=AssertionError('recopy')):
            job = self.run_move()
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual(job['destinations'], ['2026/10/11-7'])
        self.assertEqual(job['bytes_copied'], 0)
        self.assertEqual(job['sources_removed'], 2)
        self.assertEqual(visited, [shifted.parent])
        self.assertFalse(self.target.exists())
        for name, data in self.contents.items():
            self.assertEqual((shifted / name).read_bytes(), data)

    def test_registered_conditions_are_immutable(self):
        conditions = {'recursive': False, 'parameters': {'tolerance': 1}}
        token = self.manager.register(self.folder, [self.group], conditions)
        conditions['parameters']['tolerance'] = 99
        self.manager.start(token, [1], 'move')
        job = self.wait()
        self.assertEqual(job['state'], 'completed', job)
        record = json.loads((self.target / 'move-log.jsonl').read_text().splitlines()[0])
        self.assertEqual(record['extraction']['parameters']['tolerance'], 1)

    def test_shifted_copy_is_found_despite_unrelated_number_collision(self):
        shifted = self.shift_copy()
        self.target.mkdir()
        (self.target / 'other.JPG').write_bytes(b'unrelated set')
        job = self.run_move()
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual(job['destinations'], ['2026/10/11-7'])
        self.assertEqual((self.target / 'other.JPG').read_bytes(), b'unrelated set')
        self.assertTrue((shifted / 'one.JPG').exists())

    def test_multiple_matching_copies_are_rejected_without_writes(self):
        import shutil
        shifted = self.shift_copy()
        shutil.copytree(shifted, self.target)
        with self.assertRaisesRegex(CopyError, '複数'):
            self.manager.start(self.scan, [1], 'move')
        self.assert_originals()
        self.assertFalse((shifted / 'MOVE_INCOMPLETE').exists())
        self.assertFalse((self.target / 'MOVE_INCOMPLETE').exists())

    def test_reused_shifted_copy_and_new_set_share_deletion_gate(self):
        shifted = self.shift_copy()
        (self.folder / 'two.JPG').write_bytes(b'new set')
        second = {'number': 2, 'files': [{'jpg': 'two.JPG', 'raw': []}]}
        token = self.manager.register(self.folder, [self.group, second])
        with patch.object(self.manager, '_copy_file', side_effect=OSError('new set failed')):
            self.manager.start(token, [1, 2], 'move')
            job = self.wait()
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['sources_removed'], 0)
        self.assertEqual(job['destinations'], ['2026/10/11-7', '2026/10/11-2'])
        self.assert_originals()
        self.assertTrue((self.folder / 'two.JPG').exists())
        self.assertTrue((shifted / 'MOVE_INCOMPLETE').exists())

    def test_shifted_copy_mismatch_keeps_all_sources(self):
        shifted = self.shift_copy()
        (shifted / 'one.JPG').write_bytes(b'different data')
        job = self.run_move()
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['sources_removed'], 0)
        self.assert_originals()
        self.assertFalse(self.target.exists())

    def test_shifted_incomplete_copy_is_rejected(self):
        shifted = self.shift_copy()
        (shifted / 'COPY_INCOMPLETE').write_text('unfinished')
        with self.assertRaisesRegex(CopyError, '記録'):
            self.manager.start(self.scan, [1], 'move')
        self.assert_originals()

    def test_other_source_folder_and_symlink_are_not_candidates(self):
        shifted = self.shift_copy()
        shifted.rename(shifted.with_name('different-7'))
        shifted.symlink_to(shifted.with_name('different-7'), target_is_directory=True)
        job = self.run_move()
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual(job['destinations'], ['2026/10/11-1'])
        self.assertGreater(job['bytes_copied'], 0)
        self.assert_targets()

    def test_existing_copy_mismatch_never_deletes_any_source(self):
        self.make_copy()
        (self.target/'RAW/one.NEF').write_bytes(b'wrong contents')
        job=self.run_move()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['sources_removed'],0)
        self.assert_originals()
        self.assertTrue((self.target/'MOVE_INCOMPLETE').exists())

    def test_copy_failure_leaves_all_sources(self):
        original=self.manager._copy_file
        def copy(file,target,log):
            if file.target.suffix=='.NEF': raise OSError('disk full')
            return original(file,target,log)
        with patch.object(self.manager,'_copy_file',side_effect=copy): job=self.run_move()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['sources_removed'],0)
        self.assert_originals()

    def test_failure_in_later_set_keeps_sources_from_earlier_set(self):
        (self.folder/'two.JPG').write_bytes(b'second set')
        second={'number':2,'files':[{'jpg':'two.JPG','raw':[]}]}
        token=self.manager.register(self.folder,[self.group,second])
        original=self.manager._copy_file
        def copy(file,target,log):
            if file.target.name=='two.JPG': raise OSError('second set failed')
            return original(file,target,log)
        with patch.object(self.manager,'_copy_file',side_effect=copy):
            self.manager.start(token,[1,2],'move')
            job=self.wait()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['sources_removed'],0)
        self.assert_originals()
        self.assertTrue((self.folder/'two.JPG').exists())

    def test_destination_changed_before_deletion_leaves_all_sources(self):
        original=self.manager._verify_move
        calls=0
        def verify(group,file,digest,version):
            nonlocal calls
            calls+=1
            if calls==3:
                target=self.target/file.target
                stamp=target.stat().st_mtime_ns
                data=target.read_bytes()
                target.write_bytes(b'X'+data[1:])
                os.utime(target,ns=(stamp,stamp))
            return original(group,file,digest,version)
        with patch.object(self.manager,'_verify_move',side_effect=verify): job=self.run_move()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['sources_removed'],0)
        self.assert_originals()

    def test_sync_failure_happens_before_any_deletion(self):
        with patch.object(self.manager,'_sync_directory',side_effect=OSError('sync failed')): job=self.run_move()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['sources_removed'],0)
        self.assert_originals()
        self.assert_targets()

    def test_deletion_permission_failure_records_partial_move(self):
        original=Path.unlink
        def unlink(path,*args,**kwargs):
            if path==self.folder/'RAW/one.NEF': raise PermissionError('cannot delete RAW')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',unlink): job=self.run_move()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['sources_removed'],1)
        self.assertFalse((self.folder/'one.JPG').exists())
        self.assertTrue((self.folder/'RAW/one.NEF').exists())
        self.assert_targets()
        self.assertTrue((self.target/'MOVE_INCOMPLETE').exists())
        self.assertIn('job_failed',(self.target/'move-log.jsonl').read_text())

    def test_source_changed_during_deletion_stops_remaining_deletions(self):
        original=self.manager._verify_move
        calls=0
        def verify(group,file,digest,version):
            nonlocal calls
            calls+=1
            if calls==4: file.source.write_bytes(b'new source content')
            return original(group,file,digest,version)
        with patch.object(self.manager,'_verify_move',side_effect=verify): job=self.run_move()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['sources_removed'],1)
        self.assertEqual((self.folder/'RAW/one.NEF').read_bytes(),b'new source content')
        self.assert_targets()

    def test_disabled_move_and_readonly_source_are_rejected(self):
        self.manager.allow_move=False
        with self.assertRaises(CopyError): self.manager.start(self.scan,[1],'move')
        self.manager.allow_move=True
        with patch('copy_jobs.os.access',return_value=False):
            with self.assertRaisesRegex(CopyError,'削除する権限'): self.manager.start(self.scan,[1],'move')
        self.assertEqual(list(self.destination.iterdir()),[])
        self.assert_originals()

    def test_incomplete_copy_or_prior_move_log_is_not_reused(self):
        self.make_copy()
        for name in ['COPY_INCOMPLETE','MOVE_INCOMPLETE','move-log.jsonl']:
            path=self.target/name; path.write_text('keep')
            with self.assertRaisesRegex(CopyError,'記録'): self.manager.start(self.scan,[1],'move')
            path.unlink()
            self.assert_originals()

    def test_move_api_requires_explicit_operation_and_enabled_setting(self):
        app=create_app({'TESTING':True,'PHOTO_ROOT':str(self.source),'COPY_ROOT':str(self.destination),'SCAN_TOKEN':'test','ALLOW_MOVE':False})
        client=app.test_client(); headers={'X-Scan-Token':'test'}
        manager=app.extensions['copy_manager']; token=manager.register(self.folder,[self.group])
        response=client.post('/api/copy',json={'scan_id':token,'sets':[1],'operation':'move'},headers=headers)
        self.assertEqual(response.status_code,400)
        self.assertFalse(client.get('/api/copy/status').get_json()['move_enabled'])
        self.assert_originals()
        self.assertEqual(client.post('/api/copy',json={'operation':'move'}).status_code,403)


if __name__=='__main__':unittest.main()
