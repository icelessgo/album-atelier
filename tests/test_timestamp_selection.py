"""Timestamp-only coverage, format limits and protected rebuilds, using synthetic data."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from timestamp_selection import select_by_time
import server
import proofing
import test_p1_regressions as p1


def photo(index, seconds=0, **extra):
    moment = datetime(2026, 6, 14, 8, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return {'id': f'{index:05d}', 'name': f'{index:05d}.jpg',
            'captured_at_utc': moment.isoformat(), 'width': 1800, 'height': 1200,
            'chapter': 'Unsorted', **extra}


class TimestampSelectionTests(TestCase):
    def test_2000_photos_cover_elapsed_day_without_dense_burst_domination(self):
        photos = [photo(index, index / 4) for index in range(1900)]
        photos += [photo(1900 + index, 900 + index * 300) for index in range(100)]
        original = deepcopy(photos)
        selected, report = select_by_time(photos, 80)
        self.assertEqual(len(selected), 80)
        self.assertEqual(len(set(selected)), 80)
        self.assertEqual(selected, sorted(selected))
        self.assertIn('00000', selected)
        self.assertIn('01999', selected)
        self.assertLess(sum(int(key) < 1900 for key in selected), 10)
        self.assertEqual(report['sampling'], 'capture_time')
        self.assertEqual(photos, original)
        self.assertEqual(selected, select_by_time(list(reversed(photos)), 80)[0])

    def test_must_use_and_exclusions_take_precedence(self):
        photos = [photo(index, index * 10, favorite=index in (13, 71), excluded=index == 0)
                  for index in range(120)]
        selected, report = select_by_time(photos, 50)
        self.assertTrue({'00013', '00071'}.issubset(selected))
        self.assertNotIn('00000', selected)
        self.assertEqual(report['must_use_count'], 2)
        self.assertEqual(report['excluded_count'], 1)
        with self.assertRaisesRegex(ValueError, 'must-use'):
            select_by_time([photo(index, index, favorite=True) for index in range(51)], 50)

    def test_missing_and_mixed_offsets_are_reported_without_guessing(self):
        photos = [photo(index, index) for index in range(60)]
        photos[3]['captured_at_utc'] = 'invalid'
        selected, report = select_by_time(photos, 50)
        self.assertEqual(len(selected), 50)
        self.assertEqual(report['missing_timestamps'], 1)
        self.assertEqual(report['sampling'], 'photo_order')
        photos[3]['captured_at_utc'] = '2026-06-14T08:00:03'
        _, report = select_by_time(photos, 50)
        self.assertTrue(report['mixed_offsets'])
        self.assertEqual(report['sampling'], 'photo_order')

    def test_known_offsets_compare_as_instants_and_naive_clocks_stay_naive(self):
        photos = [photo(0, captured_at_utc='2026-06-14T10:00:00+02:00'),
                  photo(1, captured_at_utc='2026-06-14T08:30:00+00:00'),
                  photo(2, captured_at_utc='2026-06-14T09:00:00+00:00')]
        self.assertEqual(select_by_time(photos, 1)[0], ['00001'])
        for row in photos:
            row['captured_at_utc'] = row['captured_at_utc'][:19]
        self.assertEqual(select_by_time(photos, 1)[1]['sampling'], 'capture_time')

    def test_identical_times_missing_times_and_small_pools_do_not_repeat_photos(self):
        for missing in (False, True):
            photos = [photo(index, captured_at_utc=None if missing else '2026-06-14T12:00:00')
                      for index in range(40)]
            selected, report = select_by_time(photos, 80)
            self.assertEqual(len(selected), 40)
            self.assertEqual(len(set(selected)), 40)
            self.assertEqual(report['sampling'], 'photo_order')
            self.assertEqual(report['identical_timestamps'], not missing)
        for value in (True, 0, 101, 80.0, '80'):
            with self.assertRaises(ValueError):
                select_by_time([photo(0)], value)

    def test_preserve_all_layouts_do_not_resample_small_chapters_away(self):
        photos = {str(index): photo(index, index, chapter='Ceremony' if index < 4 else 'Portraits')
                  for index in range(100)}
        # Use the dictionary keys as the photo identity, as real metadata does.
        for key, row in photos.items():
            row['id'] = key
        for design_format in server.PRINT_FORMAT_SPECS:
            if design_format == 'legacy':
                continue
            with self.subTest(design_format=design_format):
                spreads = server.build_directed_draft(list(photos), photos, 34, 3,
                    design_format, avoid_similar=False, preserve_all=True)
                used = [slot['photo_id'] for spread in spreads for slot in spread['slots']]
                self.assertEqual(len(used), 100)
                self.assertEqual(set(used), set(photos))
                self.assertFalse(server.validate_project({'spreads': spreads}, photos))


class TimestampHTTPTests(TestCase):
    setUp = p1.StudioHTTPTests.setUp
    request = p1.StudioHTTPTests.request

    def project(self, count=80):
        ids = []
        for index in range(count):
            key = str(uuid4())
            row = photo(index, index * 60, id=key, picpeak_event_id=7,
                        stored_name=key + '.jpg', favorite=index == 17)
            server.write_json(server.PHOTO_META / (key + '.json'), row)
            ids.append(key)
        source = {'kind': 'picpeak', 'event_id': 7, 'photo_ids': ids}
        manifest = server.PHOTO_META.parent / 'picpeak-galleries' / '7.json'
        server.write_json(manifest, {'photos': {key: {'visibility': 'visible', 'readable': True} for key in ids}})
        project = server.create_project('Timestamp test', source=source, shortlist=[])
        project['design_format'] = 'saal_portrait_22x30'
        server.write_project(project)
        return project, ids

    def build(self, project, ids, **extra):
        return self.request('POST', f"/api/projects/{project['id']}/autobuild",
            {'revision': project['revision'], 'photo_ids': ids,
             'selection_method': 'timestamps', 'target_photos': 80, **extra})

    def test_build_uses_metadata_only_and_settings_survive_autosave(self):
        project, ids = self.project(100)
        with (patch.object(server, 'ensure_visual_hashes', side_effect=AssertionError('pixel scan')),
              patch.object(server.Image, 'open', side_effect=AssertionError('image opened'))):
            status, built = self.build(project, ids)
        self.assertEqual(status, 200, built)
        used = [slot['photo_id'] for spread in built['spreads'] for slot in spread['slots']]
        self.assertEqual(len(used), 80)
        self.assertEqual(len(set(used)), 80)
        self.assertEqual(set(used), set(built['photo_ids']))
        self.assertEqual(len(built['spreads']), 27)
        self.assertFalse(built.get('active_proof_id'))
        saved = server.save_project(project['id'], built)
        self.assertEqual(saved['draft_settings']['target_photos'], 80)
        self.assertEqual(saved['timestamp_draft']['input_count'], 100)

    def test_bad_counts_stale_revisions_and_foreign_gallery_never_replace_work(self):
        project, ids = self.project()
        before = server.id_path(server.PROJECTS, project['id']).read_bytes()
        for extra in ({'target_photos': 49}, {'target_photos': True}, {'revision': 0}):
            self.assertIn(self.build(project, ids, **extra)[0], (400, 409))
            self.assertEqual(server.id_path(server.PROJECTS, project['id']).read_bytes(), before)
        self.assertEqual(self.build(project, ids + [self.ids[2]])[0], 400)
        self.assertEqual(server.id_path(server.PROJECTS, project['id']).read_bytes(), before)

    def test_too_few_saal_photos_leave_draft_untouched(self):
        project, ids = self.project(12)
        before = server.id_path(server.PROJECTS, project['id']).read_bytes()
        self.assertEqual(self.build(project, ids)[0], 400)
        self.assertEqual(server.id_path(server.PROJECTS, project['id']).read_bytes(), before)

    def test_unselected_cover_is_retained_without_adding_an_interior_photo(self):
        project, ids = self.project(100)
        selected, _ = select_by_time([server.json_file(server.PHOTO_META / (key + '.json')) for key in ids], 80)
        cover = next(key for key in ids if key not in selected)
        project.update(cover_photo_id=cover, photo_ids=[cover], cover_crop={'focus_x': .2, 'focus_y': .7})
        server.write_project(project)
        status, built = self.build(project, ids)
        self.assertEqual(status, 200, built)
        self.assertEqual(built['cover_photo_id'], cover)
        self.assertEqual(built['cover_crop'], project['cover_crop'])
        used = [slot['photo_id'] for spread in built['spreads'] for slot in spread['slots']]
        self.assertEqual(len(used), 80)
        self.assertNotIn(cover, used)
        self.assertIn(cover, built['photo_ids'])

    def test_hidden_source_photo_is_rejected_without_replacing_the_album(self):
        project, ids = self.project()
        manifest_path = server.PHOTO_META.parent / 'picpeak-galleries' / '7.json'
        manifest = server.json_file(manifest_path)
        manifest['photos'][ids[20]]['visibility'] = 'hidden'
        server.write_json(manifest_path, manifest)
        before = server.id_path(server.PROJECTS, project['id']).read_bytes()
        self.assertEqual(self.build(project, ids)[0], 400)
        self.assertEqual(server.id_path(server.PROJECTS, project['id']).read_bytes(), before)

    def approved(self):
        project, ids = self.project()
        proof_id = str(uuid4())
        (server.PROOFS / proof_id).mkdir()
        server.write_json(server.PROOFS / proof_id / 'proof.json', {'id': proof_id, 'status': 'open', 'history': []})
        project.update(status='approved', product='saal', active_proof_id=proof_id,
            approved_proof_id=proof_id, approved_at='2026-10-22T09:00:00Z',
            approval_record={'sha256': 'a' * 64},
            photo_ids=[ids[0]], cover_photo_id=ids[0],
            spreads=[{'id': str(uuid4()), 'layout': 'hero',
                      'slots': [{'photo_id': ids[0], 'focus_x': .5, 'focus_y': .5}]}])
        server.write_project(project)
        return project, ids

    def test_rebuild_archives_approval_and_withdraws_old_proof(self):
        project, ids = self.approved()
        with patch.object(server, 'set_active') as withdrawn:
            status, built = self.build(project, ids)
        self.assertEqual(status, 200, built)
        withdrawn.assert_called_once()
        self.assertEqual(built['status'], 'draft')
        for key in ('approval_record', 'approved_proof_id', 'approved_at', 'active_proof_id'):
            self.assertNotIn(key, built)
        self.assertEqual(built['approval_history'], [project['approval_record']])
        self.assertEqual(proofing.load_proof(server.PROOFS, project['active_proof_id'])['status'], 'replaced')

    def test_failed_remote_withdrawal_keeps_old_layout_and_blocks_export(self):
        project, ids = self.approved()
        with patch.object(server, 'set_active', side_effect=RuntimeError('Synthetic Pi unavailable')):
            self.assertEqual(self.build(project, ids)[0], 500)
        saved = server.load_project(project['id'])
        self.assertEqual(saved['spreads'], project['spreads'])
        self.assertTrue(saved['sharing_pending'])
        self.assertEqual(saved['approval_history'], [project['approval_record']])
        with self.assertRaisesRegex(ValueError, 'pending review withdrawal'):
            server.start_export(project['id'])
