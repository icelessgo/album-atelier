"""Cheap, deterministic album suggestions from metadata; never inspect image pixels."""
from datetime import timezone

from capture_time import photo_moment, photo_sort_key


def select_by_time(photos, count=80):
    """Cover the capture-time range, preserving must-use photos and exclusions.

    Fill the largest uncovered intervals first. Ordinal distance breaks ties,
    distributing repeated timestamps instead of taking the start of a burst.
    Missing times or mixed known/unknown offsets use the existing ordered-photo
    fallback: do not guess a camera timezone or claim inferred wedding chapters.
    """
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError('Choose between 1 and 100 photos for a timestamp draft')
    unique = {photo['id']: photo for photo in photos}
    eligible = sorted((photo for photo in unique.values() if not photo.get('excluded')),
                      key=lambda photo: (*photo_sort_key(photo), photo['id']))
    if not eligible:
        raise ValueError('No available photos remain after your exclusions')
    target = min(count, len(eligible))
    picked = {index for index, photo in enumerate(eligible) if photo.get('favorite')}
    if len(picked) > target:
        raise ValueError('There are more must-use favourites than the target photo count. Increase the target or review your stars.')
    moments = [photo_moment(photo) for photo in eligible]
    missing = sum(moment is None for moment in moments)
    offsets = {moment.tzinfo is not None for moment in moments if moment is not None}
    mixed = len(offsets) > 1
    positions = list(range(len(eligible)))
    sampling = 'photo_order'
    same_time = False
    if not missing and not mixed:
        # Explicit UTC conversion only for already-aware times. Naive camera
        # clocks remain naive; subtraction never assumes this PC's timezone.
        normalized = [moment.astimezone(timezone.utc) if moment.tzinfo else moment
                      for moment in moments]
        positions = [(moment - normalized[0]).total_seconds() for moment in normalized]
        same_time = positions[-1] == positions[0]
        if same_time:
            positions = list(range(len(eligible)))
        else:
            sampling = 'capture_time'
    if target == 1 and not picked:
        midpoint = (positions[0] + positions[-1]) / 2
        picked.add(min(range(len(eligible)), key=lambda index: (abs(positions[index] - midpoint), index)))
    # Keep both ends of the day when must-use choices leave room.
    for index in (0, len(eligible) - 1):
        if len(picked) < target:
            picked.add(index)
    distance = [min(abs(position - positions[index]) for index in picked) for position in positions]
    ordinal_distance = [min(abs(index - chosen) for chosen in picked) for index in range(len(eligible))]
    while len(picked) < target:
        candidate = max((index for index in range(len(eligible)) if index not in picked),
                        key=lambda index: (distance[index], ordinal_distance[index], -index))
        picked.add(candidate)
        for index, position in enumerate(positions):
            distance[index] = min(distance[index], abs(position - positions[candidate]))
            ordinal_distance[index] = min(ordinal_distance[index], abs(index - candidate))
    selected = [photo['id'] for index, photo in enumerate(eligible) if index in picked]
    return selected, {'input_count': len(unique), 'eligible_count': len(eligible),
                      'selected_count': len(selected), 'requested_count': count,
                      'must_use_count': sum(bool(photo.get('favorite')) for photo in eligible),
                      'excluded_count': len(unique) - len(eligible),
                      'missing_timestamps': missing, 'mixed_offsets': mixed,
                      'identical_timestamps': same_time, 'sampling': sampling}
