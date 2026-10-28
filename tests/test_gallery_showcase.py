"""Explicit gallery publication, frozen cards, withdrawal and proof isolation."""
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import threading
from unittest import TestCase
from unittest.mock import patch

import test_p1_regressions as p1
import test_saal as saal
import server
import proofing
import proof_server
from product_catalogue import default_catalogue
from gallery_showcase import prepare_showcase, find_showcase, card_view


class GalleryShowcaseTests(TestCase):
    setUp = p1.StudioHTTPTests.setUp
    request = p1.StudioHTTPTests.request
    project = saal.SaalProofTests.project

    def linked(self):
        project = self.project()
        project['source'] = {'kind': 'picpeak', 'event_id': 7, 'photo_ids': self.ids[:2]}
        project['source_galleries'] = [{'event_id': 7, 'slug': 'synthetic-wedding', 'expires_at': None}]
        server.write_project(project)
        return project

    def publish(self, project, show=True):
        with (patch.object(server, 'check_project_sources'),
              patch.object(server, 'preflight_project', return_value={'errors': []}),
              patch.object(server, 'publish_proof', return_value=False),
              patch('gallery_showcase.gallery_access', return_value=True)):
            status, result = self.request('POST', f"/api/projects/{project['id']}/proof",
                {'revision': project['revision'], 'show_in_gallery': show})
        self.assertEqual(status, 201, result)
        return proofing.load_proof(server.PROOFS, result['id'])

    def find(self, event=7):
        with patch('gallery_showcase.gallery_access', return_value=True):
            return find_showcase(server.PROOFS, event)

    def test_ordinary_proof_and_unpublished_draft_have_no_gallery_card(self):
        project = self.linked()
        self.assertEqual(self.find(), (None, None))
        proof = self.publish(project, False)
        self.assertEqual(self.find(), (None, None))
        self.assertNotIn('gallery_showcase_event_id', proof)

    def test_explicit_publish_is_scoped_and_exposes_only_frozen_card_fields(self):
        proof = self.publish(self.linked())
        card, current = self.find()
        view = card_view(card, current)
        self.assertEqual(view['proof_id'], proof['id'])
        self.assertEqual((view['price_minor'], view['currency']), (15000, 'EUR'))
        self.assertEqual(self.find(8), (None, None))
        self.assertFalse({'password_digest', 'session_secret', 'photos', 'source_manifest', 'approved_at'} & view.keys())
        self.assertEqual(proofing.public_view(proof)['gallery_path'], '/gallery/synthetic-wedding')

    def test_client_edits_keep_published_cover_price_and_block_approval(self):
        proof = self.publish(self.linked())
        proofing.update_client_layout(server.PROOFS, proof['id'], 0, proof['spreads'], self.ids[1])
        card, current = self.find()
        self.assertEqual(card['cover_photo_id'], self.ids[0])
        self.assertEqual(card_view(card, current)['price_minor'], 15000)
        self.assertTrue(card_view(card, current)['changes_pending'])
        with self.assertRaisesRegex(ValueError, 'photographer review'):
            proofing.approve_proof(server.PROOFS, proof['id'], 'saal')

    def test_expiry_withdrawal_and_source_lifecycle_hide_card(self):
        proof = self.publish(self.linked())
        with patch('gallery_showcase.gallery_access', return_value=False):
            self.assertEqual(find_showcase(server.PROOFS, 7), (None, None))
        proof['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        proofing._save(server.PROOFS / proof['id'] / 'proof.json', proof)
        self.assertEqual(self.find(), (None, None))
        proof['expires_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        proofing._save(server.PROOFS / proof['id'] / 'proof.json', proof)
        server.set_active(server.PROOFS, proof['project_id'], None)
        self.assertEqual(self.find(), (None, None))

    def test_approved_compared_edition_uses_its_frozen_base_offer(self):
        project = self.linked()
        project['design_format'] = 'gobook_landscape_35x25'
        server.write_project(project)
        proof = self.publish(project)
        card, current = self.find()
        self.assertEqual(card_view(card, current)['price_minor'], 30000)
        approved = proofing.approve_proof(server.PROOFS, proof['id'], 'edge',
            selected_format='landscape_40x30', companion_quantities={'one_mid': 1, 'two_small': 0})
        self.assertEqual(approved['total_eur'], 700)
        card, current = self.find()
        summary = card_view(card, current)
        self.assertEqual(summary['price_minor'], 50000)
        self.assertEqual(summary['product_label'], 'EDGE')
        self.assertEqual(summary['closed_size'], '40×30 cm')

    def test_gallery_cards_support_every_verified_saal_and_graphistudio_format(self):
        for design_format in server.PRINT_FORMAT_SPECS:
            if design_format == 'legacy':
                continue
            with self.subTest(design_format=design_format):
                project = self.linked()
                project['design_format'] = design_format
                server.write_project(project)
                proof = self.publish(project)
                card, current = self.find()
                self.assertEqual(card['id'], proof['id'])
                self.assertEqual(card_view(card, current)['price_minor'], proof['offer']['base_minor'])

    def test_saal_card_freezes_catalogue_price_instead_of_profile_default(self):
        catalogue = default_catalogue()
        catalogue['products']['saal_portrait_22x30']['price_minor'] = 17300
        with patch.object(server, 'load_catalogue', return_value=catalogue):
            self.publish(self.linked())
        card, current = self.find()
        self.assertEqual(card_view(card, current)['price_minor'], 17300)

    def test_mixed_or_unlinked_sources_cannot_be_promoted(self):
        project = self.linked()
        proof = proofing.create_proof(server.PROOFS, project, self.photos, server.PREVIEWS, server.THUMBS)[0]
        for galleries in ([], [{'event_id': 8, 'slug': 'other'}], [*project['source_galleries'], {'event_id': 8, 'slug': 'other'}]):
            with self.assertRaisesRegex(ValueError, 'one verified'):
                prepare_showcase(server.PROOFS, {**proof, 'source_galleries': galleries}, project)

    def test_republish_replaces_gallery_card_and_removes_old_review_access(self):
        project = self.linked()
        old = self.publish(project)
        project = server.load_project(project['id'])
        newer = self.publish(project)
        self.assertEqual(self.find()[0]['id'], newer['id'])
        self.assertFalse(proofing.is_open(proofing.load_proof(server.PROOFS, old['id'])))

    def test_internal_card_does_not_unlock_media_or_approval(self):
        proof = self.publish(self.linked())
        self.stack.enter_context(patch.object(proof_server, 'PROOFS', server.PROOFS))
        self.stack.enter_context(patch('gallery_showcase.gallery_access', return_value=True))
        self.stack.enter_context(patch.object(proof_server, 'gallery_access', return_value=True))
        http = ThreadingHTTPServer(('127.0.0.1', 0), proof_server.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close); self.addCleanup(http.shutdown)
        def get(path):
            connection = HTTPConnection('127.0.0.1', http.server_port)
            connection.request('GET', path)
            response = connection.getresponse(); body = response.read(); connection.close()
            return response.status, response.getheaders(), body
        status, headers, body = get('/internal/gallery/7/album')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['proof_id'], proof['id'])
        self.assertIn(('Cache-Control', 'no-store'), headers)
        self.assertEqual(get('/internal/gallery/7/album/cover')[0], 200)
        self.assertEqual(get('/internal/gallery/8/album')[0], 404)
        self.assertEqual(get('/review/internal/gallery/7/album')[0], 404)
        self.assertEqual(get(f"/review/{proof['id']}/api")[0], 401)
        self.assertEqual(get(f"/review/{proof['id']}/media/{self.ids[0]}.jpg")[0], 401)
