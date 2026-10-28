"""Regression coverage for frozen proposals, retouches and CRM boundaries."""
from contextlib import ExitStack
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import approval_record
import crm_handoff
import gallery_lifecycle
import picpeak_import
import product_catalogue
import proofing
import server
import source_integrity
from project_store import ProjectConflict, write_json
from renderer import SRGB_PROFILE
import test_p1_regressions as p1


class SharedCatalogueTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        for name in ('PROJECTS','PHOTO_META','ORIGINALS','PREVIEWS','THUMBS','PROOFS','EXPORTS','JOB_RECORDS'):
            folder = self.root / name.lower(); folder.mkdir()
            self.stack.enter_context(patch.object(server, name, folder))
        self.stack.enter_context(patch.object(server,'DATA',self.root))
        self.stack.enter_context(patch.object(server,'set_active'))
        self.key = str(uuid4())
        self.photo = {'id':self.key,'name':'Synthetic.jpg','stored_name':self.key+'.jpg',
                      'width':1200,'height':900,'chapter':'Unsorted'}
        for folder in (server.ORIGINALS,server.PREVIEWS,server.THUMBS):
            Image.new('RGB',(120,90),'teal').save(folder / (self.key+'.jpg'),'JPEG',icc_profile=SRGB_PROFILE)
        write_json(server.PHOTO_META / (self.key+'.json'),self.photo)
        self.project = server.create_project('Synthetic album',[self.key],spreads=[
            {'id':str(uuid4()),'layout':'hero','slots':[{'photo_id':self.key,'focus_x':.5,'focus_y':.5}]}
            for _ in range(10)])
        self.project['design_format'] = 'gobook_portrait_25x35'
        server.write_project(self.project)

    def proof(self, catalogue=None):
        return proofing.create_proof(server.PROOFS,self.project,server.load_photos(),
                                    server.PREVIEWS,server.THUMBS,'synthetic-password',catalogue=catalogue)[0]

    def approve(self, proof):
        return proofing.approve_proof(server.PROOFS,proof['id'],'gobook',selected_format='gobook_portrait_25x35')

    def test_catalogue_revision_and_validated_profile_constraints(self):
        path = self.root / 'catalogue.json'
        catalogue = product_catalogue.default_catalogue()
        catalogue['products']['gobook_portrait_25x35']['price_minor'] = 32550
        saved = product_catalogue.save_catalogue(path,catalogue)
        self.assertEqual(saved['revision'],2)
        self.assertEqual(product_catalogue.offer_for('gobook_portrait_25x35',saved)['base_eur'],325.5)
        with self.assertRaises(ProjectConflict): product_catalogue.save_catalogue(path,catalogue)
        bad = deepcopy(saved); bad['products']['saal_square_30x30']['choices']['cover'] = ['Different spine']
        with self.assertRaisesRegex(ValueError,'verified supplier profile'): product_catalogue.save_catalogue(path,bad)

    def test_published_offer_and_approval_survive_live_price_and_availability_changes(self):
        catalogue = product_catalogue.default_catalogue()
        proof = self.proof(catalogue)
        catalogue['products']['gobook_portrait_25x35'].update(price_minor=99900,enabled=False)
        product_catalogue.save_catalogue(self.root/'catalogue.json',catalogue)
        approved = self.approve(proof)
        self.assertEqual(approved['total_eur'],300)
        self.assertEqual(approved['agreement']['total_minor'],30000)
        self.assertEqual(approved,self.approve(proof))
        with self.assertRaisesRegex(ValueError,'no longer available'): self.proof(catalogue)

    def test_offered_order_choices_are_binding_and_frozen_in_agreement(self):
        catalogue = product_catalogue.default_catalogue()
        catalogue['products']['gobook_portrait_25x35']['choices']['cover']=['Forest green','Ivory']
        with self.assertRaisesRegex(ValueError,'offered cover'): self.proof(catalogue)
        self.project['order_details']={'cover':'Forest green','box':'Matching GoBox','finish':'Matte'}
        record = self.approve(self.proof(catalogue))['agreement']
        self.assertEqual(record['order_details']['cover'],'Forest green')
        self.assertEqual(record['design_sha256'],approval_record.design_identity(self.project))
        approval_record.validate_agreement(record)
        changed = deepcopy(record); changed['order_details']['cover']='Ivory'
        with self.assertRaisesRegex(ValueError,'incomplete or changed'): approval_record.validate_agreement(changed)

    def test_each_compared_edition_freezes_its_own_default_construction(self):
        catalogue=product_catalogue.default_catalogue()
        catalogue['products']['gobook_portrait_25x35']['choices']['cover']=['Forest green']
        catalogue['products']['portrait_30x40']['choices']['cover']=['Ivory']
        proof=self.proof(catalogue)
        self.assertEqual(proof['order_details']['cover'],'Forest green')
        approved=proofing.approve_proof(server.PROOFS,proof['id'],'edge',selected_format='portrait_30x40')
        self.assertEqual(approved['agreement']['order_details']['cover'],'Ivory')
        self.assertEqual(approved['agreement']['total_minor'],50000)
        self.assertEqual(approved['agreement']['design_sha256'],approval_record.design_identity({**self.project,'order_details':{'cover':'Ivory'}}))

    def test_incompatible_cover_is_not_offered_on_another_book(self):
        catalogue=product_catalogue.default_catalogue()
        catalogue['products']['gobook_portrait_25x35']['choices']['cover']=['Forest green']
        catalogue['products']['portrait_30x40']['choices']['cover']=['Ivory']
        self.project['order_details']={'cover':'Forest green'}
        proof=self.proof(catalogue)
        self.assertEqual([offer['product'] for offer in proof['offers']],['gobook'])
        with self.assertRaisesRegex(ValueError,'matching book'):
            proofing.approve_proof(server.PROOFS,proof['id'],'edge',selected_format='portrait_30x40')

    def test_order_notes_and_title_changes_require_fresh_client_approval(self):
        proof = self.proof(); approved = self.approve(proof)
        self.project.update(status='approved',product='gobook',active_proof_id=proof['id'],
            approved_proof_id=proof['id'],approval_record=approved['agreement'])
        server.write_project(self.project)
        saved = server.save_project(self.project['id'],{'revision':1,'order_details':{'cover':'Ivory'}})
        self.assertEqual(saved['status'],'draft')
        self.assertNotIn('approved_proof_id',saved)
        self.assertNotIn('approval_record',saved)
        self.assertEqual(saved['approval_history'][0],approved['agreement'])
        self.assertFalse(proofing.is_open(proofing.load_proof(server.PROOFS,proof['id'])))

    def test_retouch_refreshes_preview_and_hash_but_keeps_photo_and_crop_ids(self):
        result, photos = server.check_project_sources(self.project)
        before = result['photos'][self.key]
        self.assertEqual(photos[self.key]['preview_original_sha256'],before)
        Image.new('RGB',(90,120),'crimson').save(server.ORIGINALS/(self.key+'.jpg'))
        result, photos = server.check_project_sources(self.project)
        self.assertNotEqual(result['photos'][self.key],before)
        self.assertEqual(photos[self.key]['width'],90)
        self.assertEqual(self.project['spreads'][0]['slots'][0]['photo_id'],self.key)
        with self.assertRaisesRegex(ValueError,'changed'): source_integrity.assert_sources({self.key:before},result['photos'])
        with Image.open(server.PREVIEWS/(self.key+'.jpg')) as preview:
            self.assertGreater(preview.getpixel((0,0))[0],150)

    def test_export_staging_cannot_change_when_cached_original_is_replaced(self):
        result, photos = server.check_project_sources(self.project)
        with source_integrity.render_snapshot(self.project,photos,server.ORIGINALS,result['photos']) as staged:
            Image.new('RGB',(120,90),'red').save(server.ORIGINALS/(self.key+'.jpg'))
            self.assertEqual(source_integrity.file_sha(staged/(self.key+'.jpg')),result['photos'][self.key])
        with self.assertRaisesRegex(ValueError,'changed while preparing'):
            with source_integrity.render_snapshot(self.project,photos,server.ORIGINALS,result['photos']): pass

    def test_proof_mutations_are_serialized_across_processes(self):
        proof = self.proof()
        code = 'import proofing,sys; [proofing.add_feedback(__import__("pathlib").Path(sys.argv[1]),sys.argv[2],"comment",sys.argv[3],"Synthetic note") for _ in range(12)]'
        processes = [subprocess.Popen([sys.executable,'-c',code,str(server.PROOFS),proof['id'],proof['spreads'][0]['id']],
                     env={**os.environ,'PYTHONPATH':str(Path(proofing.__file__).parent)},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                     for _ in range(2)]
        for process in processes:
            _, error = process.communicate(timeout=20)
            self.assertEqual(process.returncode,0,error.decode())
        saved = proofing.load_proof(server.PROOFS,proof['id'])
        self.assertEqual(len(saved['feedback']),24)
        self.assertEqual(len(saved['history']),25)

    def test_crm_handoff_requires_valid_approval_and_explicit_customer_and_vat(self):
        with self.assertRaisesRegex(ValueError,'Confirm an unchanged'): crm_handoff.handoff(self.project)
        self.project.update(status='approved',approval_record=self.approve(self.proof())['agreement'])
        handoff = crm_handoff.handoff(self.project)
        self.assertIn(handoff['agreement']['sha256'],handoff['summary'])
        with patch.object(crm_handoff,'bridge',return_value={'id':17,'status':'draft'}) as bridge:
            with self.assertRaisesRegex(ValueError,'VAT'): crm_handoff.create_draft(self.project,{'admin_id':1,'customer_id':2})
            self.assertEqual(crm_handoff.create_draft(self.project,{'admin_id':1,'customer_id':2,'vat_rate':0})['status'],'draft')
            request = bridge.call_args.args[0]
            self.assertEqual(request['agreement'],handoff['agreement'])
            self.assertEqual(request['operation'],'draft')

    def test_remote_source_unavailable_does_not_allow_cached_original(self):
        self.photo.update(picpeak_event_id=7,picpeak_photo_id=99)
        write_json(server.PHOTO_META/(self.key+'.json'),self.photo)
        with patch.object(source_integrity,'source_versions',return_value={'available':False}):
            with self.assertRaisesRegex(ValueError,'gallery is expired'): server.check_project_sources(self.project)

    def test_failed_link_withdrawal_is_visible_and_can_be_retried(self):
        proof=self.proof()
        self.project['active_proof_id']=proof['id']
        server.write_project(self.project)
        with patch.object(server,'set_active',side_effect=OSError('Pi offline')):
            with self.assertRaises(OSError): server.withdraw_project(self.project,'Photo replaced')
        pending=server.load_project(self.project['id'])
        self.assertEqual(pending['sharing_pending'],'Photo replaced')
        self.assertEqual(pending['status'],'draft')
        server.withdraw_project(pending,pending['sharing_pending'])
        server.write_project(pending)
        self.assertNotIn('sharing_pending',server.load_project(self.project['id']))
        self.assertFalse(proofing.is_open(proofing.load_proof(server.PROOFS,proof['id'])))

    def test_r2_version_change_downloads_retouch_and_stream_failure_preserves_cache(self):
        self.photo.update(picpeak_event_id=7,picpeak_photo_id=99,original_source_version='a'*64)
        raw=BytesIO(); Image.new('RGB',(45,60),'red').save(raw,'PNG')
        def child(*args,**kwargs):
            return subprocess.Popen([sys.executable,'-c','import sys;sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))',raw.getvalue().hex()],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        with patch.object(picpeak_import,'_bridge',side_effect=child) as bridge:
            target=picpeak_import.fetch_original(self.photo,server.ORIGINALS,source_version='b'*64)
            self.assertEqual(target.suffix,'.png')
            self.assertFalse((server.ORIGINALS/(self.key+'.jpg')).exists())
            self.assertEqual(bridge.call_args.kwargs['source_version'],'b'*64)
        before=target.read_bytes()
        def failed(*args,**kwargs):
            return subprocess.Popen([sys.executable,'-c','import sys;sys.stdout.buffer.write(b"partial");sys.exit(1)'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        with patch.object(picpeak_import,'_bridge',side_effect=failed):
            with self.assertRaises(RuntimeError): picpeak_import.fetch_original(self.photo,server.ORIGINALS,source_version='c'*64)
        self.assertEqual(target.read_bytes(),before)


class LifecycleTests(TestCase):
    def setUp(self):
        gallery_lifecycle._CACHE.clear()

    def test_requests_cache_available_gallery_without_idle_polling_and_fail_closed(self):
        proof={'source_galleries':[{'slug':'synthetic-gallery'}]}
        response=BytesIO(json.dumps({'event_type':'wedding','is_active':True,'is_expired':False,'hidden_until_reveal':False}).encode())
        with patch.dict(os.environ,{'ALBUM_PICPEAK_INFO_BASE':'http://backend/api/gallery'}):
            with patch.object(gallery_lifecycle,'build_opener') as factory:
                factory.return_value.open.return_value.__enter__.return_value=response
                self.assertTrue(gallery_lifecycle.gallery_access(proof))
                self.assertTrue(gallery_lifecycle.gallery_access(proof))
                self.assertEqual(factory.return_value.open.call_count,1)
            gallery_lifecycle._CACHE.clear()
            with patch.object(gallery_lifecycle,'build_opener',side_effect=OSError('offline')):
                with self.assertRaises(gallery_lifecycle.GalleryCheckUnavailable): gallery_lifecycle.gallery_access(proof)

    def test_reclassified_and_unknown_gallery_types_close_existing_review_access(self):
        proof={'source_galleries':[{'slug':'synthetic-gallery'}]}
        for event_type in ('family', 'other', None):
            with self.subTest(event_type=event_type):
                gallery_lifecycle._CACHE.clear()
                value={'event_type':event_type,'is_active':True,'is_expired':False,'hidden_until_reveal':False}
                response=BytesIO(json.dumps(value).encode())
                with (patch.dict(os.environ,{'ALBUM_PICPEAK_INFO_BASE':'http://backend/api/gallery'}),
                      patch.object(gallery_lifecycle,'build_opener') as factory):
                    factory.return_value.open.return_value.__enter__.return_value=response
                    self.assertFalse(gallery_lifecycle.gallery_access(proof))

    def test_legacy_and_preview_lifetimes_remain_compatible_and_bad_slugs_fail_closed(self):
        self.assertTrue(gallery_lifecycle.gallery_access({}))
        self.assertTrue(gallery_lifecycle.gallery_access({'preview_only':True,'source_galleries':[{'slug':'private'}]}))
        with patch.dict(os.environ,{'ALBUM_PICPEAK_INFO_BASE':'http://backend/api/gallery'}):
            self.assertFalse(gallery_lifecycle.gallery_access({'source_galleries':[{'slug':'../../admin'}]}))


class SharedWorkflowHTTPTests(TestCase):
    request = p1.StudioHTTPTests.request

    def setUp(self):
        p1.StudioHTTPTests.setUp(self)
        self.stack.enter_context(patch.object(server,'DATA',server.PROJECTS.parent))
        self.stack.enter_context(patch.object(server,'publish_proof',return_value=False))
        self.stack.enter_context(patch.object(server,'pull_proof',return_value=False))
        self.stack.enter_context(patch.object(server,'set_active'))
        self.stack.enter_context(patch.object(server,'preflight_project',return_value={'errors':[]}))
        self.key=self.ids[0]
        for key in self.ids:
            for folder in (server.ORIGINALS,server.PREVIEWS,server.THUMBS):
                Image.new('RGB',(120,90),'teal').save(folder/(key+'.jpg'),'JPEG')
        self.project=server.create_project('Synthetic HTTP album',[self.key],spreads=[
            {'id':str(uuid4()),'layout':'hero','slots':[{'photo_id':self.key,'focus_x':.5,'focus_y':.5}]}
            for _ in range(10)])
        self.project['design_format']='gobook_portrait_25x35'
        server.write_project(self.project)

    def test_publish_approve_adopt_download_and_order_change(self):
        base='/api/projects/'+self.project['id']
        status,published=self.request('POST',base+'/proof',{'revision':1})
        self.assertEqual(status,201,published)
        proof=proofing.load_proof(server.PROOFS,published['id'])
        self.assertEqual(set(proof['source_manifest']),{self.key})
        approval=proofing.approve_proof(server.PROOFS,proof['id'],'gobook')
        status,project=self.request('POST',base+'/adopt-approval',{'revision':published['revision']})
        self.assertEqual(status,200,project)
        self.assertEqual(project['approval_record'],approval['agreement'])
        status,handoff=self.request('GET',base+'/approval-record')
        self.assertEqual(status,200)
        self.assertEqual(handoff['agreement']['sha256'],project['approval_record']['sha256'])
        with patch.object(server.crm_handoff,'status',return_value={'ready':False,'reason':'Enable Quotes'}):
            self.assertFalse(self.request('GET',base+'/crm')[1]['ready'])
        status,changed=self.request('PUT',base,{'revision':project['revision'],'order_details':{'cover':'Ivory'}})
        self.assertEqual(status,200,changed)
        self.assertEqual(changed['status'],'draft')
        self.assertNotIn('approval_record',changed)
        self.assertEqual(self.request('GET',base+'/approval-record')[0],400)

    def test_catalogue_requires_operator_origin_and_optimistic_revision(self):
        value=product_catalogue.default_catalogue()
        self.assertEqual(self.request('PUT','/api/catalogue',value,{'Origin':'https://foreign.example'})[0],403)
        self.assertEqual(self.request('PUT','/api/catalogue',value)[0],200)
        self.assertEqual(self.request('PUT','/api/catalogue',value)[0],409)
        status,state=self.request('GET','/api/state')
        self.assertEqual(status,200)
        self.assertEqual(state['offers']['saal_portrait_22x30']['base_eur'],150)

    def test_crm_request_with_old_revision_is_rejected_before_bridge(self):
        with patch.object(server.crm_handoff,'create_draft') as create:
            status,_=self.request('POST','/api/projects/'+self.project['id']+'/crm-quote',{'revision':0})
        self.assertEqual(status,409)
        create.assert_not_called()

    def test_local_client_preview_uses_the_same_catalogue_without_publishing(self):
        catalogue=product_catalogue.default_catalogue()
        catalogue['products']['gobook_portrait_25x35']['price_minor']=35099
        self.assertEqual(self.request('PUT','/api/catalogue',catalogue)[0],200)
        folder=server.PROJECTS.parent/'local-previews';folder.mkdir()
        with patch.object(server,'LOCAL_PREVIEW_PROOFS',folder),patch.object(server,'LOCAL_REVIEW_PORT',12345):
            status,preview=self.request('POST','/api/projects/'+self.project['id']+'/client-preview',{})
        self.assertEqual(status,201,preview)
        path=next(folder.glob('*/proof.json'))
        proof=json.loads(path.read_text())
        self.assertEqual(proof['offer']['base_eur'],350.99)
        self.assertTrue(proof['preview_only'])
        self.assertNotIn('active_proof_id',server.load_project(self.project['id']))

    def test_republishing_archives_previous_approval_and_requires_a_new_one(self):
        base='/api/projects/'+self.project['id']
        status,published=self.request('POST',base+'/proof',{'revision':1})
        self.assertEqual(status,201,published)
        approval=proofing.approve_proof(server.PROOFS,published['id'],'gobook')
        status,adopted=self.request('POST',base+'/adopt-approval',{'revision':published['revision']})
        self.assertEqual(status,200,adopted)
        status,replaced=self.request('POST',base+'/proof',{'revision':adopted['revision']})
        self.assertEqual(status,201,replaced)
        current=server.load_project(self.project['id'])
        self.assertEqual(current['status'],'draft')
        self.assertNotIn('approval_record',current)
        self.assertEqual(current['approval_history'],[approval['agreement']])

    def test_adopting_alternative_book_keeps_its_exact_frozen_order_choices(self):
        catalogue=product_catalogue.default_catalogue()
        catalogue['products']['gobook_portrait_25x35']['choices']['cover']=['Forest green']
        catalogue['products']['portrait_30x40']['choices']['cover']=['Ivory']
        self.assertEqual(self.request('PUT','/api/catalogue',catalogue)[0],200)
        base='/api/projects/'+self.project['id']
        status,published=self.request('POST',base+'/proof',{'revision':1})
        self.assertEqual(status,201,published)
        proofing.approve_proof(server.PROOFS,published['id'],'edge',selected_format='portrait_30x40')
        status,adopted=self.request('POST',base+'/adopt-approval',{'revision':published['revision']})
        self.assertEqual(status,200,adopted)
        self.assertEqual(adopted['order_details'],{'cover':'Ivory'})
        self.assertEqual(adopted['approval_record']['design_sha256'],approval_record.design_identity(adopted))
