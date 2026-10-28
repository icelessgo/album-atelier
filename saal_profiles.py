"""Verified standard Professional Line profiles; safe to import on the proof host."""

SAAL_PROFILES = {
    'saal_square_30x30': {'size': '30×30 cm', 'label': 'Saal Professional Line · 30×30 square',
                         'trim_mm': (588, 294), 'cover_trim_mm': (268, 300)},
    'saal_landscape_30x21': {'size': '30×21 cm', 'label': 'Saal Professional Line · 30×21 landscape',
                            'trim_mm': (588, 204), 'cover_trim_mm': (268, 210)},
    'saal_portrait_22x30': {'size': '22×30 cm', 'label': 'Saal Professional Line · 22×30 portrait',
                           'trim_mm': (438, 294), 'cover_trim_mm': (193, 300)},
}
PROFILE_REVISION = 'saal-professional-line-matte-acrylic-white-2026-10-21'


def is_saal(design_format):
    return design_format in SAAL_PROFILES


def minimum_spreads(design_format):
    return 13 if is_saal(design_format) else 10


def cover_crop(value=None):
    if value is None:
        return {'focus_x': .5, 'focus_y': .5}
    import math
    if not isinstance(value, dict) or set(value) != {'focus_x', 'focus_y'}:
        raise ValueError('Choose a valid cover crop')
    if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in value.values()):
        raise ValueError('Cover crop values must be between zero and one')
    return dict(value)


def saal_offer(design_format):
    profile = SAAL_PROFILES[design_format]
    width, height = profile['trim_mm']
    return {'design_format': design_format, 'supplier': 'Saal Digital', 'product': 'saal',
            'product_label': 'Saal Professional Line', 'closed_size': profile['size'],
            'base_eur': 150, 'amount_minor': 15000, 'currency': 'EUR', 'companion_eur': 0, 'companions': {},
            'spread_ratio': width / height, 'open_width_cm': width / 10, 'open_height_cm': height / 10,
            'minimum_spreads': 13, 'maximum_spreads': 40, 'checkout_enabled': False,
            'cover_ratio': profile['cover_trim_mm'][0] / profile['cover_trim_mm'][1],
            'profile_revision': PROFILE_REVISION, 'paper': 'Matte photographic paper',
            'cover_material': 'Acrylic + white leatherette'}
