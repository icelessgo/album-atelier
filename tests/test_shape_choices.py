"""Client shape proposals keep frozen prices and require a revised, checked proof."""
import copy
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import threading
from unittest import TestCase
from unittest.mock import patch

import test_saal as saal
import server
import proofing
import proof_server
from product_catalogue import default_catalogue


class ShapeChoiceTests(TestCase):
    setUp = saal.SaalProofTests.setUp
    project = saal.SaalProofTests.project

    def publish(self, design_format='saal_portrait_22x30', catalogue=None):
        project = self.project(design_format)
        proof, _ = proofing.create_proof(server.PROOFS, project, self.photos,
            server.PREVIEWS, server.THUMBS, catalogue=catalogue)
        return project, proof

    def change(self, proof, target, **kwargs):
        return proofing.update_client_layout(server.PROOFS, proof['id'],
            proof.get('layout_revision', 0), proof['spreads'], proof['cover_photo_id'],
            selected_format=target, from_format=kwargs.get('from_format', proof['design_format']))

    def test_saal_shapes_are_frozen_at_150_without_becoming_unchecked_approval_offers(self):
        _, proof = self.publish()
        self.assertEqual(len(proof['offers']), 1)
        self.assertEqual({item['design_format'] for item in proof['shape_offers']}, set(saal.SAAL_PROFILES))
        self.assertTrue(all(item['base_minor'] == 15000 for item in proof['shape_offers']))
        with self.assertRaisesRegex(ValueError, 'matching book'):
            proofing.approve_proof(server.PROOFS, proof['id'], 'saal', selected_format='saal_square_30x30')

    def test_shape_proposal_persists_geometry_and_blocks_approval_until_republication(self):
        project, proof = self.publish()
        with patch.object(proofing, 'offer_for', side_effect=AssertionError('Do not read live prices')):
            view = self.change(proof, 'saal_landscape_30x21')
        self.assertTrue(view['client_edits_pending'])
        self.assertTrue(view['shape_review_required'])
        self.assertEqual(view['layout_revision'], 1)
        self.assertEqual(view['offer']['spread_ratio'], 588 / 204)
        self.assertEqual(view['offer']['base_minor'], 15000)
        self.assertEqual(view['spreads'], proof['spreads'])
        self.assertEqual(view['cover_crop'], proof['cover_crop'])
        self.assertEqual(server.load_project(project['id'])['design_format'], 'saal_portrait_22x30')
        saved = proofing.load_proof(server.PROOFS, proof['id'])
        self.assertEqual(saved['photographer_design_format'], 'saal_portrait_22x30')
        with self.assertRaisesRegex(ValueError, 'photographer review'):
            proofing.approve_proof(server.PROOFS, proof['id'], 'saal')
        with self.assertRaisesRegex(ValueError, 'another tab'):
            self.change(proof, 'saal_square_30x30')

        project['active_proof_id'] = proof['id']; server.write_project(project)
        with patch.object(server, 'pull_proof'):
            applied = server.apply_client_layout(project['id'])
        self.assertEqual(applied['design_format'], 'saal_landscape_30x21')
        self.assertEqual(applied['cover_crop'], proof['cover_crop'])
        self.assertEqual(applied['status'], 'draft')
        self.assertNotIn('active_proof_id', applied)
        self.assertFalse(proofing.is_open(proofing.load_proof(server.PROOFS, proof['id'])))
        fresh, _ = proofing.create_proof(server.PROOFS, applied, self.photos, server.PREVIEWS, server.THUMBS)
        approved = proofing.approve_proof(server.PROOFS, fresh['id'], 'saal')
        self.assertEqual(approved['design_format'], 'saal_landscape_30x21')
        self.assertEqual(approved['agreement']['design_format'], 'saal_landscape_30x21')
        self.assertEqual(approved['total_eur'], 150)

    def test_changed_prices_disabled_shapes_and_foreign_books_cannot_be_selected(self):
        catalogue = default_catalogue()
        catalogue['products']['saal_square_30x30']['price_minor'] = 17500
        catalogue['products']['saal_landscape_30x21']['enabled'] = False
        _, proof = self.publish(catalogue=catalogue)
        self.assertEqual([item['design_format'] for item in proof['shape_offers']], ['saal_portrait_22x30'])
        for target in ['saal_square_30x30', 'saal_landscape_30x21', 'gobook_portrait_25x35', 'invented']:
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'same book and price'):
                self.change(proof, target)

    def test_graphistudio_shapes_preserve_the_chosen_book_tier_and_companion_prices(self):
        for source, target, price in [('gobook_portrait_25x35', 'gobook_landscape_35x25', 30000),
                                      ('portrait_30x40', 'landscape_40x30', 50000)]:
            with self.subTest(source=source):
                _, proof = self.publish(source)
                view = self.change(proof, target)
                self.assertEqual(view['offer']['base_minor'], price)
                self.assertEqual(view['offer']['companion_eur'], 200)
                self.assertIn('30×20', view['offer']['companions']['one_mid'])
                self.assertEqual(view['design_format'], target)
        catalogue = default_catalogue()
        catalogue['products']['gobook_landscape_35x25']['companion_price_minor'] = 21000
        _, proof = self.publish('gobook_portrait_25x35', catalogue)
        with self.assertRaisesRegex(ValueError, 'same book and price'):
            self.change(proof, 'gobook_landscape_35x25')
        with self.assertRaisesRegex(ValueError, 'same book and price'):
            self.change(proof, 'landscape_40x30')

    def test_approved_and_payment_locked_proofs_cannot_change_shape(self):
        for lock in ['approved', 'pending', 'paid']:
            _, proof = self.publish()
            if lock == 'approved':
                proofing.approve_proof(server.PROOFS, proof['id'], 'saal')
            else:
                proof['payment'] = {'status': lock}
                proofing._save(server.PROOFS / proof['id'] / 'proof.json', proof)
            before = copy.deepcopy(proofing.load_proof(server.PROOFS, proof['id']))
            with self.assertRaisesRegex(ValueError, 'no longer be edited'):
                self.change(proof, 'saal_square_30x30')
            self.assertEqual(before, proofing.load_proof(server.PROOFS, proof['id']))

    def test_shape_route_retains_authentication_origin_and_revision_checks(self):
        _, proof = self.publish()
        self.stack.enter_context(patch.object(proof_server, 'PROOFS', server.PROOFS))
        self.stack.enter_context(patch.object(proof_server, 'REQUIRE_ACTIVE', False))
        http = ThreadingHTTPServer(('127.0.0.1', 0), proof_server.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close); self.addCleanup(http.shutdown)
        def request(origin=None, cookie=None, revision=0):
            connection = HTTPConnection('127.0.0.1', http.server_port)
            headers = {'Content-Type': 'application/json', 'Origin': origin or f'http://127.0.0.1:{http.server_port}'}
            if cookie: headers['Cookie'] = cookie
            connection.request('PUT', f"/review/{proof['id']}/layout", json.dumps({
                'revision': revision, 'spreads': proof['spreads'],
                'from_format': proof['design_format'], 'design_format': 'saal_square_30x30'}), headers)
            response = connection.getresponse(); body = json.loads(response.read()); connection.close()
            return response.status, body
        cookie = 'album_review=' + proofing.session_cookie(proof)
        self.assertEqual(request()[0], 401)
        self.assertEqual(request('https://foreign.invalid', cookie)[0], 403)
        self.assertEqual(request(cookie=cookie)[0], 200)
        self.assertEqual(request(cookie=cookie)[0], 400)
