"""Portable, versioned approval evidence for printing and CRM hand-off."""
from copy import deepcopy
from hashlib import sha256
import json


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def design_identity(project):
    from saal_profiles import cover_crop
    slots = [slot for spread in project.get('spreads', []) for slot in spread['slots']]
    return digest({'title': project.get('title'), 'spreads': project.get('spreads'),
                   'cover_photo_id': project.get('cover_photo_id') or (slots[0]['photo_id'] if slots else None),
                   'cover_crop': cover_crop(project.get('cover_crop')),
                   'order_details': project.get('order_details', {})})


def make_agreement(proof, approval, offer):
    details = deepcopy(offer.get('order_details', proof.get('order_details', {})))
    record = {'schema_version': 1, 'proof_id': proof['id'],
              'project_id': proof['project_id'], 'project_revision': proof['project_revision'],
              'approved_at': approval['at'], 'design_sha256': design_identity({**proof, 'order_details': details}),
              'source_manifest': deepcopy(proof.get('source_manifest', {})),
              'offer': deepcopy(offer), 'order_details': details,
              'product': approval['product'], 'design_format': approval['design_format'],
              'companion_quantities': deepcopy(approval['companion_quantities']),
              'currency': 'EUR', 'total_minor': round(approval['total_eur'] * 100)}
    return {**record, 'sha256': digest(record)}


def validate_agreement(record):
    if (not isinstance(record, dict) or record.get('schema_version') != 1 or
            record.get('sha256') != digest({key: value for key, value in record.items() if key != 'sha256'})):
        raise ValueError('The approval record is incomplete or changed. Publish a fresh proof.')
    return record


def summary(record):
    validate_agreement(record)
    offer = record['offer']
    counts = record['companion_quantities']
    lines = [f"{offer['product_label']} · {offer['closed_size']}",
             f"Approved {record['approved_at']}",
             f"Larger companion copies: {counts['one_mid']}; smaller copies: {counts['two_small']}"]
    lines.extend(f'{key.title()}: {value}' for key, value in record['order_details'].items() if value)
    lines.extend([f"Total: EUR {record['total_minor'] / 100:.2f}",
                  f"Proof: {record['proof_id']}", f"Approval reference: {record['sha256']}"])
    return '\n'.join(lines)
