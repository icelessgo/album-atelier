"""HTTP regressions for concurrent editing, recovery, gallery boundaries and isolation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import proofing
import proof_server
from project_store import ProjectConflict


class QuietHandler(server.Handler):
    def log_message(self, *args):
        pass


class StudioHTTPTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(TemporaryDirectory()))
        for name in ('PROJECTS', 'PHOTO_META', 'ORIGINALS', 'PREVIEWS', 'THUMBS', 'PROOFS'):
            path = root / name.lower()
            path.mkdir()
            self.stack.enter_context(patch.object(server, name, path))
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.host = f'127.0.0.1:{self.http.server_address[1]}'
        self.origin = 'http://' + self.host
        self.photos = {}
        for index, event in enumerate((7, 7, 8)):
            key = str(uuid4())
            photo = {'id': key, 'name': f'photo-{index}.jpg', 'width': 5000, 'height': 4000,
                     'picpeak_event_id': event, 'stored_name': key + '.jpg'}
            self.photos[key] = photo
            server.write_json(server.PHOTO_META / (key + '.json'), photo)
        self.ids = list(self.photos)
        manifests = root / 'picpeak-galleries'
        manifests.mkdir()
        for event in (7, 8):
            server.write_json(manifests / f'{event}.json', {'photos': {key:{'visibility':'visible','readable':True}
                for key, photo in self.photos.items() if photo['picpeak_event_id']==event}})
        self.stack.enter_context(patch.object(server, 'JOB_RECORDS', root / 'jobs'))
        (root / 'jobs').mkdir()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection(self.host, timeout=8)
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        request_headers = {'Origin': self.origin, 'Content-Type': 'application/json'}
        request_headers.update(headers or {})
        connection.request(method, path, data, request_headers)
        response = connection.getresponse()
        output = response.read()
        connection.close()
        return response.status, json.loads(output)

    def test_foreign_hosts_origins_and_simple_requests_cannot_read_or_write(self):
        for method, headers, status in (
            ('GET', {'Host': 'attacker.invalid'}, 403),
            ('POST', {'Origin': 'https://attacker.invalid'}, 403),
            ('POST', {'Origin': ''}, 403),
            ('POST', {'Content-Type': 'text/plain'}, 415),
            ('POST', {'Sec-Fetch-Site': 'cross-site'}, 403),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(method, '/api/projects', {'title': 'blocked'}, headers)[0], status)
        self.assertEqual(list(server.PROJECTS.glob('*.json')), [])
        self.assertEqual(self.request('POST', '/api/projects', {'title': 'Allowed'})[0], 201)
        raw = BytesIO()
        Image.new('RGB', (20, 20), 'teal').save(raw, 'JPEG')
        self.assertEqual(self.request('POST', '/api/photos', raw.getvalue(), {'Content-Type': 'image/jpeg'})[0], 201)

    def test_same_revision_http_saves_have_exactly_one_winner(self):
        project = server.create_project('Original')
        barrier = threading.Barrier(2)
        def save(title):
            barrier.wait()
            return self.request('PUT', '/api/projects/' + project['id'], {**project, 'title': title})
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(save, ['First', 'Second']))
        self.assertEqual(sorted(row[0] for row in results), [200, 409])
        winner = next(row[1] for row in results if row[0] == 200)
        self.assertEqual(server.load_project(project['id'])['title'], winner['title'])
        self.assertEqual(server.load_project(project['id'])['revision'], 2)
        self.assertEqual(list(server.PROJECTS.glob('*.tmp')), [])

    def test_save_vs_autobuild_is_serialized_for_the_entire_mutation(self):
        project = server.create_project('Original')
        entered, release = threading.Event(), threading.Event()
        real_build = server.build_draft
        def slow_build(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return real_build(*args)
        with patch.object(server, 'build_draft', side_effect=slow_build), ThreadPoolExecutor(2) as pool:
            build = pool.submit(self.request, 'POST', f"/api/projects/{project['id']}/autobuild",
                                {'revision': 1, 'photo_ids': self.ids[:2]})
            self.assertTrue(entered.wait(3))
            save = pool.submit(self.request, 'PUT', '/api/projects/' + project['id'], {**project, 'title': 'Lost edit'})
            time.sleep(.05)
            self.assertFalse(save.done())
            release.set()
            self.assertEqual(build.result()[0], 200)
            self.assertEqual(save.result()[0], 409)
        self.assertEqual(server.load_project(project['id'])['title'], 'Original')

    def test_lost_reply_replays_once_and_never_rebases_over_newer_changes(self):
        project = server.create_project('Original')
        request = {**project, 'title': 'Recovered', '_operation_id': str(uuid4())}
        first = server.save_project(project['id'], request)
        self.assertEqual(server.save_project(project['id'], request), first)
        self.assertEqual(first['revision'], 2)
        with self.assertRaises(ProjectConflict):
            server.save_project(project['id'], {**request, 'title': 'Different payload'})
        server.save_project(project['id'], {**first, 'title': 'Other tab', '_operation_id': str(uuid4())})
        with self.assertRaises(ProjectConflict):
            server.save_project(project['id'], request)
        self.assertEqual(server.load_project(project['id'])['title'], 'Other tab')

    def test_empty_and_foreign_selections_never_fall_back_to_the_library(self):
        source = {'kind': 'picpeak', 'event_id': 7, 'photo_ids': self.ids[:2]}
        project = server.create_project('Gallery seven', source=source, shortlist=[self.ids[0]])
        path = f"/api/projects/{project['id']}"
        for ids in ([], [self.ids[2]]):
            self.assertEqual(self.request('POST', path + '/autobuild', {'revision': 1, 'photo_ids': ids})[0], 400)
        self.assertEqual(server.load_project(project['id'])['spreads'], [])
        for changed in ({'photo_ids': [self.ids[2]]}, {'shortlist': [self.ids[2]]},
                        {'spreads': [{'id': str(uuid4()), 'layout': 'hero', 'slots': [{'photo_id': self.ids[2]}]}]}):
            self.assertEqual(self.request('PUT', path, {**project, **changed})[0], 400)
        status, built = self.request('POST', path + '/autobuild', {'revision': 1, 'photo_ids': [self.ids[0]]})
        self.assertEqual(status, 200)
        self.assertEqual(built['source'], source)
        self.assertEqual(built['shortlist'], [self.ids[0]])
        self.assertEqual(self.request('GET', path)[1]['source'], source)

    def test_import_project_keeps_a_manifest_and_shortlist_before_first_build(self):
        job_id = str(uuid4())
        with patch.dict(server.JOBS, {job_id: {'kind': 'picpeak-import', 'status': 'ready',
                        'event_id': 7, 'photo_ids': self.ids[:2], 'suggested_ids': [self.ids[1]]}}):
            status, project = self.request('POST', '/api/projects', {'title': 'Imported', 'import_job_id': job_id})
        self.assertEqual(status, 201)
        loaded = self.request('GET', '/api/projects/' + project['id'])[1]
        self.assertEqual(loaded['source']['photo_ids'], self.ids[:2])
        self.assertEqual(loaded['shortlist'], [self.ids[1]])
        self.assertEqual(loaded['spreads'], [])

    def test_legacy_single_gallery_inference_preserves_intentional_mixed_albums(self):
        project = server.create_project('Old album', self.ids[:2])
        project.pop('source')
        server.write_json(server.id_path(server.PROJECTS, project['id']), project)
        self.assertEqual(server.load_project(project['id'])['source']['event_id'], 7)
        project['photo_ids'] = self.ids
        server.write_json(server.id_path(server.PROJECTS, project['id']), project)
        self.assertIsNone(server.load_project(project['id'])['source'])
        # New local albums may deliberately use photos from several sources.
        local = server.create_project('Mixed local')
        self.assertEqual(server.save_project(local['id'], {**local, 'photo_ids': self.ids})['photo_ids'], self.ids)

    def test_remote_revocation_failure_leaves_project_retryable(self):
        project = server.create_project('Published')
        project['active_proof_id'] = str(uuid4())
        server.write_json(server.id_path(server.PROJECTS, project['id']), project)
        request = {**project, 'title': 'Edited', '_operation_id': str(uuid4())}
        with patch.object(server, 'set_active', side_effect=RuntimeError('Pi unavailable')):
            with self.assertRaises(RuntimeError):
                server.save_project(project['id'], request)
        self.assertEqual(server.load_project(project['id'])['revision'], 1)
        with patch.object(server, 'set_active'), patch.object(server, 'revoke_proof'):
            saved = server.save_project(project['id'], request)
        self.assertEqual(saved['title'], 'Edited')
        self.assertNotIn('active_proof_id', saved)

    def test_every_mutating_project_action_checks_revision_before_side_effects(self):
        project = server.create_project('Album')
        for action in ('autobuild', 'proof', 'adopt-approval', 'apply-client-layout'):
            for body in ({}, {'revision': 0}):
                with self.subTest(action=action, body=body):
                    self.assertEqual(self.request('POST', f"/api/projects/{project['id']}/{action}", body)[0], 409)

    def test_new_proof_uses_import_manifest_instead_of_other_cached_gallery_photos(self):
        selected = self.ids[0]
        for key in self.ids:
            for folder in (server.PREVIEWS, server.THUMBS):
                Image.new('RGB', (30, 20), 'teal').save(folder / (key + '.jpg'))
        project = server.create_project('Imported', [selected],
            source={'kind': 'picpeak', 'event_id': 7, 'photo_ids': [selected]})
        project['design_format'] = 'landscape_40x30'
        project['spreads'] = [{'id': str(uuid4()), 'layout': 'hero', 'slots': [{'photo_id': selected}]} for _ in range(10)]
        proof, _ = proofing.create_proof(server.PROOFS, project, self.photos, server.PREVIEWS, server.THUMBS)
        self.assertEqual([photo['id'] for photo in proof['photos']], [selected])
        self.assertEqual(proof['gallery_photo_count'], 1)
