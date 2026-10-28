export function reflowSlots(previous, orderedIds) {
  const available = new Map();
  for (const slot of previous) {
    if (!available.has(slot.photo_id)) available.set(slot.photo_id, []);
    available.get(slot.photo_id).push(slot);
  }
  return orderedIds.map(photoId => {
    const existing = available.get(photoId)?.shift();
    return existing ? { ...existing } : { photo_id: photoId, focus_x: 0.5, focus_y: 0.5 };
  });
}

export function swapSlots(slots, first, second) {
  if (first === second || first < 0 || second < 0 || first >= slots.length || second >= slots.length) {
    throw new RangeError('Choose two different photo frames');
  }
  const result = slots.map(slot => ({ ...slot }));
  [result[first], result[second]] = [result[second], result[first]];
  return result;
}

export function cropGeometry(width, height, ratio, focusX = 0.5, focusY = 0.5) {
  if (![width, height, ratio].every(value => Number.isFinite(value) && value > 0)) {
    throw new RangeError('Photo and frame dimensions must be positive');
  }
  const cropWidth = Math.min(width, height * ratio);
  const cropHeight = cropWidth / ratio;
  const left = (width - cropWidth) * Math.max(0, Math.min(1, focusX));
  const top = (height - cropHeight) * Math.max(0, Math.min(1, focusY));
  return { cropWidth, cropHeight, left, top };
}
