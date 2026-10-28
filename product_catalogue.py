"""One retail catalogue; print dimensions remain in the validated supplier profiles.

Existing proofs contain their own offers. Editing this file's persisted catalogue
only changes the next proposal, never a price a client has already approved.
"""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

from project_store import ProjectConflict, project_lock, write_json
from saal_profiles import SAAL_PROFILES, saal_offer


def default_offers():
    offers = {}
    for key, product, size, width, height, companions in (
        ('gobook_portrait_25x35', 'gobook', '25×35 cm', 50.8, 34.7, ('20×30', '15×20')),
        ('gobook_landscape_35x25', 'gobook', '35×25 cm', 69.7, 25.5, ('30×20', '20×15')),
        ('portrait_30x40', 'edge', '30×40 cm', 60, 41, ('20×30', '15×20')),
        ('landscape_40x30', 'edge', '40×30 cm', 82, 30, ('30×20', '20×15')),
    ):
        offers[key] = dict(design_format=key, product=product,
            product_label='GoBook' if product == 'gobook' else 'EDGE',
            base_eur=300 if product == 'gobook' else 500, companion_eur=200,
            closed_size=size, spread_ratio=width / height,
            open_width_cm=width, open_height_cm=height,
            companions={'one_mid': f'One {companions[0]} cm companion',
                        'two_small': f'Two {companions[1]} cm companions'})
    offers.update({key: saal_offer(key) for key in SAAL_PROFILES})
    return offers


def default_catalogue():
    return {'schema_version': 1, 'revision': 1, 'currency': 'EUR', 'products': {
        key: {'label': offer['product_label'], 'enabled': True,
              'price_minor': round(offer['base_eur'] * 100),
              'companion_price_minor': round(offer['companion_eur'] * 100),
              'choices': {'cover': ['Acrylic + white leatherette'], 'box': ['No box'],
                          'finish': ['Matte photographic paper']} if key in SAAL_PROFILES
                         else {'cover': [], 'box': [], 'finish': []}}
        for key, offer in default_offers().items()}}


def load_catalogue(path: Path | None = None):
    if path is None or not path.is_file():
        return default_catalogue()
    value = json.loads(path.read_text(encoding='utf-8'))
    validate_catalogue(value)
    return value


def validate_catalogue(value):
    defaults = default_catalogue()
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or
            value.get('currency') != 'EUR' or type(value.get('revision')) is not int or
            value['revision'] < 1 or set(value.get('products', {})) != set(defaults['products'])):
        raise ValueError('Use the existing, validated album formats and EUR currency')
    for key, item in value['products'].items():
        if not isinstance(item, dict) or set(item) != set(defaults['products'][key]):
            raise ValueError('Invalid catalogue product')
        if not isinstance(item['label'], str) or not 1 <= len(item['label'].strip()) <= 80:
            raise ValueError('Enter a product name of 1 to 80 characters')
        if type(item['enabled']) is not bool:
            raise ValueError('Choose whether the product is available')
        for field in ('price_minor', 'companion_price_minor'):
            if type(item[field]) is not int or not 0 <= item[field] <= 10000000:
                raise ValueError('Enter a valid price in cents')
        choices = item['choices']
        if not isinstance(choices, dict) or set(choices) != {'cover', 'box', 'finish'}:
            raise ValueError('Invalid cover, box or finish choices')
        for entries in choices.values():
            if (not isinstance(entries, list) or len(entries) > 40 or
                    any(not isinstance(entry, str) or not 1 <= len(entry.strip()) <= 120 for entry in entries) or
                    len(set(entries)) != len(entries)):
                raise ValueError('Use distinct cover, box and finish choices of up to 120 characters')
        # Only this Saal construction has verified print dimensions.
        if key in SAAL_PROFILES and item['choices'] != defaults['products'][key]['choices']:
            raise ValueError('Saal construction needs a verified supplier profile before it can change')
    if not any(item['enabled'] for item in value['products'].values()):
        raise ValueError('Keep at least one album available')


def save_catalogue(path: Path, incoming):
    validate_catalogue(incoming)
    with project_lock(path):
        current = load_catalogue(path)
        if incoming['revision'] != current['revision']:
            raise ProjectConflict('The catalogue changed in another tab. Reload it before saving.')
        value = deepcopy(incoming)
        value['revision'] += 1
        write_json(path, value)
        return value


def offer_for(design_format, catalogue=None, *, allow_disabled=False):
    catalogue = catalogue or default_catalogue()
    offer = deepcopy(default_offers()[design_format])
    item = catalogue['products'][design_format]
    if not item['enabled'] and not allow_disabled:
        raise ValueError('This album is no longer available. Choose an available edition.')
    offer.update(product_label=item['label'], base_eur=float(Decimal(item['price_minor']) / 100),
                 companion_eur=float(Decimal(item['companion_price_minor']) / 100),
                 base_minor=item['price_minor'], currency='EUR',
                 catalogue_revision=catalogue['revision'], order_choices=deepcopy(item['choices']),
                 available=item['enabled'])
    return offer


def order_details(offer, details):
    """Empty lists retain existing photographer notes; offered lists are binding."""
    details = deepcopy(details or {})
    for key, choices in offer['order_choices'].items():
        if choices:
            if not details.get(key) and len(choices) == 1:
                details[key] = choices[0]
            if details.get(key) not in choices:
                raise ValueError(f'Choose an offered {key} before sharing this album')
    return details
