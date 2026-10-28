"""Small, explicit gallery cards for active photographer-published proofs.

PicPeak authorizes gallery access before requesting these internal endpoints.
Guests may see the offer; the separate album password still protects the proof.
There is no public discovery route, shared database credential or automatic unlock.
"""
import json
import re
from uuid import UUID

from gallery_lifecycle import gallery_access
from project_store import write_json
from proofing import is_open, load_proof


def prepare_showcase(root, proof, project):
    source = project.get('source') or {}
    galleries = proof.get('source_galleries') or []
    event_id = source.get('event_id')
    if (source.get('kind') != 'picpeak' or type(event_id) is not int or event_id < 1 or
            len(galleries) != 1 or galleries[0].get('event_id') != event_id or
            not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', galleries[0].get('slug', ''))):
        raise ValueError('Publish in gallery needs one verified, linked PicPeak gallery')
    offer = proof.get('offer')
    if not offer or type(offer.get('base_minor')) is not int:
        raise ValueError('Choose an available album edition before publishing in the gallery')
    proof['gallery_showcase_event_id'] = event_id
    proof['gallery_path'] = '/gallery/' + galleries[0]['slug']
    write_json(root / proof['id'] / 'proof.json', proof)
    # Frozen published cover/price: client-proposed edits never silently replace
    # the photographer's gallery presentation before review and republication.
    card = {key: proof[key] for key in ('id', 'project_id', 'title', 'created_at', 'cover_photo_id')}
    card.update(schema_version=1, event_id=event_id, cover_crop=proof.get('cover_crop'),
                spreads=len(proof['spreads']), offer={key: offer.get(key) for key in
                    ('base_minor', 'currency', 'product_label', 'closed_size', 'cover_ratio')})
    write_json(root / proof['id'] / 'gallery-card.json', card)


def find_showcase(root, event_id):
    """Inspect bounded small sidecars, then recheck the live proof every request.

    Active pointers already cross the desktop/Pi publication and withdrawal
    boundary. Reusing them avoids a second mutable publication authority.
    """
    candidates = []
    for count, pointer in enumerate((root / 'active').glob('*.txt')):
        if count >= 2000:
            raise ValueError('Gallery album lookup needs photographer attention')
        try:
            project_id = str(UUID(pointer.stem))
            proof_id = str(UUID(pointer.read_text(encoding='ascii').strip()))
            sidecar = root / proof_id / 'gallery-card.json'
            if not sidecar.is_file() or sidecar.stat().st_size > 8192:
                continue
            card = json.loads(sidecar.read_text(encoding='utf-8'))
            if (card.get('schema_version') == 1 and card.get('event_id') == event_id and
                    card.get('id') == proof_id and card.get('project_id') == project_id):
                candidates.append(card)
        except (ValueError, OSError, KeyError):
            continue
    for card in sorted(candidates, key=lambda value: (value.get('created_at', ''), value['id']), reverse=True):
        proof = load_proof(root, card['id'])
        galleries = proof.get('source_galleries') or []
        if (proof.get('preview_only') or not is_open(proof) or
                proof.get('gallery_showcase_event_id') != event_id or
                proof.get('project_id') != card['project_id'] or
                len(galleries) != 1 or galleries[0].get('event_id') != event_id):
            continue
        if not gallery_access(proof):
            continue
        return card, proof
    return None, None


def card_view(card, proof):
    offer = card['offer']
    if proof.get('approved'):
        # A couple may approve EDGE after starting with GoBook. Show that
        # edition's frozen base offer, never a stale GoBook price or live catalogue.
        approved_format = proof['approved'].get('design_format')
        offer = next((item for item in proof.get('offers', [])
                      if item.get('design_format') == approved_format), offer)
    return {'event_id': card['event_id'], 'proof_id': card['id'], 'title': card['title'],
            'spreads': card['spreads'], 'price_minor': offer.get('base_minor', offer.get('amount_minor')),
            'currency': offer['currency'], 'product_label': offer['product_label'],
            'closed_size': offer['closed_size'], 'cover_ratio': offer.get('cover_ratio'),
            'cover_crop': card.get('cover_crop'), 'approved': bool(proof.get('approved')),
            'changes_pending': bool(proof.get('client_edits_pending') or proof.get('feedback'))}
