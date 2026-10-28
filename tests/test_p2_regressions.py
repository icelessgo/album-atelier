"""P2 behavior: source revocation, EXIF chronology, durable exports and bounded work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.client import HTTPConnection
from io import BytesIO
import base64
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
import zipfile

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import picpeak_import
import proofing
from bridge_stream import stream, json_rows, BridgeCancelled
from capture_time import exif_capture, photo_sort_key
from gallery_sources import current_manifest
from scripts.backfill_capture_times import backfill
import test_p1_regressions as p1


def child(data=b'', *, stderr=b'', code=0, pause=0):
    process = subprocess.Popen([sys.executable, '-c',
        "import sys,time;sys.stderr.buffer.write(b'x'*int(sys.argv[2][1:]) if sys.argv[2].startswith('#') else bytes.fromhex(sys.argv[2]));sys.stderr.flush();"
        'sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]));sys.stdout.flush();'
        'time.sleep(float(sys.argv[4]));sys.exit(int(sys.argv[3]))',
        data.hex(), ('#'+str(len(stderr))) if len(stderr)>50000 else stderr.hex(), str(code), str(pause)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    process.album_new_session = True
    return process


def preview_rows(ids, *, hidden=(), membership=None, empty_error=False):
    raw = BytesIO()
    Image.new('RGB', (60,40), 'teal').save(raw, 'JPEG')
    rows = [{'photo': {'event_id':7,'photo_id':key,'name':f'Photo-{key}.jpg','width':5000,'height':4000,
                      'visibility':'hidden' if key in hidden else 'visible'},
             'preview':base64.b64encode(raw.getvalue()).decode()} for key in ids]
    entries = membership if membership is not None else ids
    rows.append({'summary': {'event_id':7,'listing_complete':True,'skipped':0,'imported':len(ids),
                             'photos':[{'photo_id':key,'visibility':'hidden' if key in hidden else 'visible'} for key in entries]}})
    return child(b''.join(json.dumps(row).encode()+b'\n' for row in rows),
                 stderr=b'No readable photos found' if empty_error else b'', code=1 if empty_error else 0)


def tiny_export(project, photos, originals, target):
    manifest = {'project_id':project['id'],'project_revision':project['revision'],
                'title':project['title'],'warnings':[]}
    with zipfile.ZipFile(target,'w') as archive:
        archive.writestr('print-check.json',json.dumps(manifest))
        archive.writestr('gobook/002-003.jpg',b'synthetic renderer output')
    return manifest


class P2HTTPTests(TestCase):
    request = p1.StudioHTTPTests.request
    def setUp(self):
        p1.StudioHTTPTests.setUp(self)
        # Export tests now exercise real source hashing and immutable staging,
        # even when the expensive final renderer is replaced with tiny_export.
        for key, photo in self.photos.items():
            Image.new('RGB',(120,90),'teal').save(server.ORIGINALS / photo['stored_name'],'JPEG')
        root = server.PROJECTS.parent
        exports = root / 'exports'; exports.mkdir()
        self.stack.enter_context(patch.object(server,'EXPORTS',exports))
        self.stack.enter_context(patch.object(server,'JOBS',{}))
        self.stack.enter_context(patch.object(server,'JOB_CANCEL',{}))
        self.stack.enter_context(patch.object(server,'EXPORT_SLOTS',threading.BoundedSemaphore(1)))
        self.stack.enter_context(patch.object(server,'IMPORT_SLOTS',threading.BoundedSemaphore(1)))

    def wait_job(self, job_id):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            job = server.get_job(job_id)
            if job['status'] in ('ready','failed','cancelled'): return job
            time.sleep(.01)
        self.fail('Job did not finish')

    def test_local_upload_uses_nested_capture_time_for_chronological_builds(self):
        ids = []
        for name, captured, offset in [('A-later.jpg','2025:06:10 09:00:00','+00:00'),
                                       ('Z-earlier.jpg','2025:06:10 10:00:00','+02:00')]:
            raw = BytesIO(); exif = Image.Exif()
            exif[34665] = {36867:captured,36881:offset}
            Image.new('RGB',(40,30),'teal').save(raw,'JPEG',exif=exif)
            status, photo = self.request('POST','/api/photos?name='+name,raw.getvalue(),{'Content-Type':'image/jpeg'})
            self.assertEqual(status,201)
            self.assertTrue(photo['captured_at_utc'])
            ids.append(photo['id'])
        project = server.create_project('Chronology')
        status, built = self.request('POST',f"/api/projects/{project['id']}/autobuild",{'revision':1,'photo_ids':ids})
        self.assertEqual(status,200)
        self.assertEqual(built['spreads'][0]['slots'][0]['photo_id'],ids[1])

    def test_shortlist_and_builder_settings_survive_reload_without_revoking_a_proof(self):
        project=server.create_project('Draft',[self.ids[0]])
        project['active_proof_id']=str(uuid4())
        server.write_project(project)
        settings={'target_spreads':31,'photos_per_spread':4,'avoid_similar':False}
        with patch.object(server,'set_active') as revoked:
            status,saved=self.request('PUT',f"/api/projects/{project['id']}",
                {**project,'shortlist':[self.ids[1]],'draft_settings':settings})
        self.assertEqual(status,200)
        self.assertFalse(revoked.called)
        loaded=self.request('GET',f"/api/projects/{project['id']}")[1]
        self.assertEqual(loaded['shortlist'],[self.ids[1]])
        self.assertEqual(loaded['photo_ids'],[self.ids[0]])
        self.assertEqual(loaded['draft_settings'],settings)
        self.assertEqual(loaded['active_proof_id'],project['active_proof_id'])
        self.assertEqual(loaded['spreads'],[])

    def test_p1_retry_receipts_still_replay_after_the_settings_upgrade(self):
        project = server.create_project('Old client')
        request = {**project,'title':'Already saved','draft_settings':{'target_spreads':25},'_operation_id':str(uuid4())}
        legacy = {key:request[key] for key in ('revision','title','spreads','photo_ids','cover_photo_id','status','product',
            'design_format','companion_option','companion_quantities','order_details','shortlist') if key in request}
        digest = hashlib.sha256(json.dumps(legacy,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        committed = {**project,'title':'Already saved','revision':2,'_save_receipts':[
            {'id':request['_operation_id'],'digest':digest,'revision':2}]}
        server.write_project(committed)
        replayed = server.save_project(project['id'],request)
        self.assertEqual(replayed['revision'],2)
        self.assertEqual(replayed['title'],'Already saved')

    def test_invalid_builder_settings_are_rejected(self):
        project=server.create_project('Draft')
        for settings in ({'target_spreads':41},{'target_spreads':True},{'photos_per_spread':0},{'avoid_similar':'yes'}):
            self.assertEqual(self.request('PUT',f"/api/projects/{project['id']}",
                {**project,'draft_settings':settings})[0],400)

    def test_removed_and_hidden_photos_stay_out_of_new_proofs_but_historical_edits_survive(self):
        for hidden in (False,True):
            with self.subTest(hidden=hidden):
                with patch.object(picpeak_import,'_bridge',return_value=preview_rows([1,2])):
                    first=picpeak_import.import_previews(7,server.PHOTO_META,server.PREVIEWS,server.THUMBS,server.SRGB_PROFILE)
                a,b=first
                project=server.create_project('Historical',[a,b],source={'kind':'picpeak','event_id':7,'photo_ids':[a,b]})
                project['design_format']='landscape_40x30'
                project['cover_photo_id']=a
                project['spreads']=[{'id':str(uuid4()),'layout':'hero','slots':[{'photo_id':a}]} for _ in range(10)]
                if hidden:
                    next_process=preview_rows([1,2],hidden=(2,))
                else:
                    next_process=preview_rows([1])
                with patch.object(picpeak_import,'_bridge',return_value=next_process):
                    picpeak_import.import_previews(7,server.PHOTO_META,server.PREVIEWS,server.THUMBS,server.SRGB_PROFILE)
                photos=server.load_photos()
                self.assertFalse(photos[b]['source_available'])
                proof,_=proofing.create_proof(server.PROOFS,project,photos,server.PREVIEWS,server.THUMBS)
                self.assertEqual([row['id'] for row in proof['photos']],[a])
                project['spreads'][0]['slots'][0]={'photo_id':b,'focus_x':.25,'focus_y':.7}
                server.write_project(project)
                loaded=server.load_project(project['id'])
                self.assertEqual(loaded['spreads'],project['spreads'])
                self.assertEqual(loaded['source_issues'][0]['id'],b)
                with self.assertRaisesRegex(ValueError,'Review photos'):
                    proofing.create_proof(server.PROOFS,loaded,photos,server.PREVIEWS,server.THUMBS)
                saved=server.save_project(project['id'],{**loaded,'title':'Keep my edits'})
                self.assertEqual(saved['spreads'][0]['slots'][0]['focus_y'],.7)

    def test_malformed_partial_import_cannot_replace_the_last_complete_membership(self):
        with patch.object(picpeak_import,'_bridge',return_value=preview_rows([1,2])):
            picpeak_import.import_previews(7,server.PHOTO_META,server.PREVIEWS,server.THUMBS,server.SRGB_PROFILE)
        before = current_manifest(server.PHOTO_META,7)
        process = child(b'{malformed}\n',pause=20)
        with patch.object(picpeak_import,'_bridge',return_value=process):
            with self.assertRaises(json.JSONDecodeError):
                picpeak_import.import_previews(7,server.PHOTO_META,server.PREVIEWS,server.THUMBS,server.SRGB_PROFILE,total=2,stall=1)
        self.assertEqual(current_manifest(server.PHOTO_META,7),before)
        self.assertIsNotNone(process.poll())
        self.assertEqual(list(server.PREVIEWS.glob('*.tmp')),[])

    def test_completed_empty_gallery_listing_records_removals_even_though_import_reports_failure(self):
        with patch.object(picpeak_import,'_bridge',return_value=preview_rows([1,2])):
            ids=picpeak_import.import_previews(7,server.PHOTO_META,server.PREVIEWS,server.THUMBS,server.SRGB_PROFILE)
        with patch.object(picpeak_import,'_bridge',return_value=preview_rows([],empty_error=True)):
            with self.assertRaisesRegex(RuntimeError,'No readable'):
                picpeak_import.import_previews(7,server.PHOTO_META,server.PREVIEWS,server.THUMBS,server.SRGB_PROFILE)
        self.assertEqual(current_manifest(server.PHOTO_META,7)['photos'],{})
        self.assertTrue(all(not server.load_photos()[key]['source_available'] for key in ids))
        self.assertTrue(all((server.PREVIEWS/(key+'.jpg')).exists() for key in ids))

    def test_finished_export_survives_registry_loss_and_downloads_exact_bytes(self):
        project=server.create_project('Snapshot',[self.ids[0]],spreads=[{'id':str(uuid4()),'layout':'hero','slots':[{'photo_id':self.ids[0],'focus_x':.5,'focus_y':.5}]}])
        with patch.object(server,'export_album',side_effect=tiny_export):
            job_id=server.start_export(project['id']); job=self.wait_job(job_id)
        self.assertEqual(job['status'],'ready')
        server.JOBS.clear()
        status,restored=self.request('GET','/api/exports/'+job_id)
        self.assertEqual((status,restored['project_revision']),(200,1))
        connection=HTTPConnection(self.host)
        connection.request('GET','/download/'+job_id)
        response=connection.getresponse();raw=response.read();connection.close()
        self.assertEqual(response.status,200)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),job['sha256'])
        self.assertEqual(response.getheader('X-Content-SHA256'),job['sha256'])
        self.assertIn('no-store',response.getheader('Cache-Control'))

    def test_restart_reconciles_interrupted_tasks_and_recovers_legacy_zip(self):
        interrupted=str(uuid4())
        server.store_job({'id':interrupted,'kind':'export','status':'working'})
        (server.EXPORTS/(interrupted+'.zip.part')).write_bytes(b'incomplete')
        imported=str(uuid4())
        server.store_job({'id':imported,'kind':'picpeak-import','status':'working'})
        legacy=str(uuid4());project=server.create_project('Archived revision')
        project['revision']=6
        tiny_export(project,{},server.ORIGINALS,server.EXPORTS/(legacy+'.zip'))
        server.JOBS.clear();server.recover_jobs()
        self.assertEqual(server.get_job(interrupted)['status'],'failed')
        self.assertEqual(server.get_job(imported)['status'],'failed')
        self.assertFalse((server.EXPORTS/(interrupted+'.zip.part')).exists())
        restored=server.get_job(legacy)
        self.assertEqual((restored['status'],restored['project_revision']),('ready',6))
        self.assertEqual(server.verified_export(legacy)[0]['sha256'],restored['sha256'])

    def test_pre_revision_legacy_archive_is_recovered_without_guessing_revision(self):
        legacy=str(uuid4());target=server.EXPORTS/(legacy+'.zip')
        manifest={'title':'Historical print','spreads':1,'printed_sides':2,'specifications':{}}
        with zipfile.ZipFile(target,'w') as archive:
            archive.writestr('print-check.json',json.dumps(manifest))
            archive.writestr('gobook/002-003.jpg',b'original legacy bytes')
        checksum=hashlib.sha256(target.read_bytes()).hexdigest()
        server.recover_jobs()
        job=server.get_job(legacy)
        self.assertEqual(job['status'],'ready')
        self.assertTrue(job['legacy_unversioned'])
        self.assertIsNone(job['project_revision'])
        connection=HTTPConnection(self.host)
        connection.request('GET','/download/'+legacy)
        response=connection.getresponse();raw=response.read();connection.close()
        self.assertEqual(response.status,200)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),checksum)
        self.assertIn('revision-unknown',response.getheader('Content-Disposition'))
        from export_records import archive_info
        with self.assertRaises(ValueError):archive_info(target)

    def test_export_snapshot_is_fixed_and_concurrent_renders_are_bounded(self):
        project=server.create_project('Original',[self.ids[0]],spreads=[{'id':str(uuid4()),'layout':'hero','slots':[{'photo_id':self.ids[0],'focus_x':.5,'focus_y':.5}]}])
        entered,release=threading.Event(),threading.Event()
        concurrent=maximum=0
        lock=threading.Lock()
        def render(*args):
            nonlocal concurrent,maximum
            with lock: concurrent+=1;maximum=max(maximum,concurrent)
            entered.set();release.wait(4)
            try:return tiny_export(*args)
            finally:
                with lock:concurrent-=1
        with patch.object(server,'export_album',side_effect=render):
            jobs=[server.start_export(project['id'])]
            self.assertTrue(entered.wait(2))
            jobs.extend(server.start_export(project['id']) for _ in range(2))
            with self.assertRaisesRegex(ValueError,'already queued'):server.start_export(project['id'])
            server.save_project(project['id'],{**project,'title':'Newer edit'})
            release.set()
            results=[self.wait_job(key) for key in jobs]
        self.assertEqual(maximum,1)
        self.assertTrue(all(job['project_revision']==1 and job['project_title']=='Original' for job in results))
        self.assertEqual(server.load_project(project['id'])['revision'],2)

    def test_changed_export_is_rejected_instead_of_serving_a_different_file(self):
        project=server.create_project('Archive',[self.ids[0]],spreads=[{'id':str(uuid4()),'layout':'hero','slots':[{'photo_id':self.ids[0],'focus_x':.5,'focus_y':.5}]}])
        with patch.object(server,'export_album',side_effect=tiny_export):
            key=server.start_export(project['id']);self.wait_job(key)
        path=server.EXPORTS/(key+'.zip');path.chmod(0o600)
        with path.open('ab') as output:output.write(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):server.verified_export(key)
        self.assertEqual(server.get_job(key)['status'],'failed')

    def test_queued_import_cancellation_reaches_a_terminal_recoverable_state(self):
        entered,release=threading.Event(),threading.Event()
        def slow(cancel,progress):
            entered.set()
            while not cancel.wait(.02):
                if release.is_set():break
            if cancel.is_set():raise BridgeCancelled('Cancelled')
            return {'done':1}
        first=server._import_task('picpeak-import',slow,total=1)
        self.assertTrue(entered.wait(2))
        second=server._import_task('picpeak-import',slow,total=1)
        status,_=self.request('POST',f'/api/picpeak/jobs/{second}/cancel',{})
        self.assertEqual(status,200)
        self.assertEqual(self.wait_job(second)['status'],'cancelled')
        server.cancel_job(first)
        self.assertEqual(self.wait_job(first)['status'],'cancelled')

    def test_versioned_preview_cache_is_private_and_does_not_apply_to_originals(self):
        key=self.ids[0]
        for folder in (server.PREVIEWS,server.THUMBS,server.ORIGINALS):
            Image.new('RGB',(30,20),'teal').save(folder/(key+'.jpg'))
        url=server.load_photos()[key]['thumb_url']
        connection=HTTPConnection(self.host);connection.request('GET',url)
        response=connection.getresponse();response.read();connection.close()
        self.assertIn('private',response.getheader('Cache-Control'))
        self.assertIn('immutable',response.getheader('Cache-Control'))
        self.assertEqual(response.getheader('Cross-Origin-Resource-Policy'),'same-origin')
        connection=HTTPConnection(self.host);connection.request('GET','/media/original/'+key)
        response=connection.getresponse();response.read();connection.close()
        self.assertIn('no-store',response.getheader('Cache-Control'))
        Image.new('RGB',(40,20),'red').save(server.THUMBS/(key+'.jpg'))
        self.assertEqual(self.request('GET',url)[0],404)


class CaptureTimeTests(TestCase):
    def test_nested_original_time_beats_modified_time_and_keeps_offset_and_subseconds(self):
        exif=Image.Exif();exif[306]='2026:01:01 01:01:01'
        exif[34665]={36867:'2025:06:10 14:25:30',36881:'+02:00',37521:'125'}
        raw=BytesIO();Image.new('RGB',(40,30),'teal').save(raw,'JPEG',exif=exif)
        with Image.open(BytesIO(raw.getvalue())) as image:result=exif_capture(image.getexif())
        self.assertEqual(result['captured_at'],'2025:06:10 14:25:30')
        self.assertEqual(result['captured_at_utc'],'2025-06-10T12:25:30.125000+00:00')
        self.assertEqual(result['capture_time_source'],'original')

    def test_filename_fallback_offset_chronology_and_burst_window(self):
        self.assertEqual(exif_capture({})['capture_time_source'],'filename')
        a={'name':'Z.jpg','captured_at_utc':'2025-06-10T10:00:20+02:00','visual_hash':'0'*16}
        b={'name':'A.jpg','captured_at_utc':'2025-06-10T08:00:40Z','visual_hash':'0'*16}
        c={**b,'captured_at_utc':'2025-06-10T08:00:51Z'}
        self.assertLess(photo_sort_key(a),photo_sort_key(b))
        self.assertTrue(server.similar_burst_photo(a,b))
        self.assertFalse(server.similar_burst_photo(a,c))
        self.assertFalse(server.similar_burst_photo(a,{'captured_at':'2025:06:10 08:00:20','visual_hash':'0'*16}))

    def test_backfill_repairs_old_import_without_altering_curation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'photos').mkdir();(root/'originals').mkdir()
            key=str(uuid4());exif=Image.Exif();exif[34665]={36867:'2025:06:10 14:25:30'}
            Image.new('RGB',(40,30),'teal').save(root/'originals'/(key+'.jpg'),exif=exif)
            path=root/'photos'/(key+'.json');path.write_text(json.dumps({'id':key,'stored_name':key+'.jpg',
                'captured_at':'','favorite':True,'chapter':'Portraits','excluded':False}))
            self.assertEqual(backfill(root)['would_update'],1)
            self.assertEqual(json.loads(path.read_text())['captured_at'],'')
            self.assertEqual(backfill(root,True)['updated'],1)
            data=json.loads(path.read_text());self.assertTrue(data['favorite'])
            self.assertEqual(data['chapter'],'Portraits');self.assertEqual(data['captured_at'],'2025:06:10 14:25:30')


class BridgeDeadlineTests(TestCase):
    def test_stalled_stdout_has_a_real_deadline_and_reaps_the_child(self):
        process=child(pause=20);started=time.monotonic()
        with patch.object(picpeak_import,'_bridge',return_value=process):
            with self.assertRaisesRegex(TimeoutError,'stopped responding'):
                picpeak_import.galleries(total=2,stall=.15)
        self.assertLess(time.monotonic()-started,2)
        self.assertIsNotNone(process.poll());self.assertTrue(process.stdout.closed)

    def test_stderr_pressure_is_drained_without_deadlock(self):
        process=child(b'[]',stderr=b'x'*400000)
        with patch.object(picpeak_import,'_bridge',return_value=process):
            self.assertEqual(picpeak_import.galleries(total=3,stall=2),[])
        self.assertEqual(process.returncode,0)

    def test_cancellation_and_malformed_streams_cleanup_children(self):
        cancel=threading.Event();process=child(pause=20)
        timer=threading.Timer(.1,cancel.set);timer.start()
        try:
            with patch.object(picpeak_import,'_bridge',return_value=process):
                with self.assertRaises(BridgeCancelled):picpeak_import.galleries(cancel=cancel,total=2,stall=1)
        finally:timer.join()
        self.assertIsNotNone(process.poll())
        process=child(b'{malformed}\n',pause=20)
        with self.assertRaises(json.JSONDecodeError):
            with stream(process,total=2,stall=1) as chunks:list(json_rows(chunks))
        self.assertIsNotNone(process.poll())

    def test_processor_exit_is_a_useful_failure_and_reaps_the_child(self):
        process=child(stderr=b'Processor restarted',code=1)
        with patch.object(picpeak_import,'_bridge',return_value=process):
            with self.assertRaisesRegex(RuntimeError,'Processor restarted'):picpeak_import.galleries(total=2,stall=1)
        self.assertIsNotNone(process.poll())
