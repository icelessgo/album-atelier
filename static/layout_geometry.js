// Frame coordinates are fractions of the full double-page spread.
const MIN_WIDTH = 0.06;
const MIN_HEIGHT = 0.12;
const GUTTER = 0.02;
const SNAP_PIXELS = 12;
const EPSILON = 0.0001;

const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
const intersects = (firstStart, firstEnd, secondStart, secondEnd) =>
  Math.min(firstEnd, secondEnd) - Math.max(firstStart, secondStart) > EPSILON;

function fitEdge(raw, minimum, maximum, snapDistance) {
  const fitted = Math.abs(raw - maximum) <= snapDistance
    ? maximum : clamp(raw, minimum, maximum);
  return { value: fitted, snapped: Math.abs(fitted - raw) > EPSILON };
}

function fitPosition(raw, minimum, maximum, snapDistance) {
  const nearest = Math.abs(raw - minimum) <= Math.abs(raw - maximum) ? minimum : maximum;
  const fitted = Math.abs(raw - nearest) <= snapDistance
    ? nearest : clamp(raw, minimum, maximum);
  return { value: fitted, snapped: Math.abs(fitted - raw) > EPSILON };
}

/** Move a whole frame, snapping its sides to adjacent gutters and canvas edges. */
export function moveLayoutFrame(start, others, deltaX, deltaY, canvasWidth, canvasHeight) {
  const [x, y, width, height] = start;
  const rawX = x + deltaX / canvasWidth;
  const rawY = y + deltaY / canvasHeight;
  const xLimits = frameY => {
    let minimum = 0;
    let maximum = 1 - width;
    for (const [otherX, otherY, otherWidth, otherHeight] of others) {
      if (!intersects(frameY, frameY + height, otherY, otherY + otherHeight)) continue;
      if (otherX >= x + width - EPSILON) {
        maximum = Math.min(maximum, Math.max(x, otherX - GUTTER - width));
      } else if (otherX + otherWidth <= x + EPSILON) {
        minimum = Math.max(minimum, Math.min(x, otherX + otherWidth + GUTTER));
      }
    }
    return [minimum, maximum];
  };
  const yLimits = frameX => {
    let minimum = 0;
    let maximum = 1 - height;
    for (const [otherX, otherY, otherWidth, otherHeight] of others) {
      if (!intersects(frameX, frameX + width, otherX, otherX + otherWidth)) continue;
      if (otherY >= y + height - EPSILON) {
        maximum = Math.min(maximum, Math.max(y, otherY - GUTTER - height));
      } else if (otherY + otherHeight <= y + EPSILON) {
        minimum = Math.max(minimum, Math.min(y, otherY + otherHeight + GUTTER));
      }
    }
    return [minimum, maximum];
  };
  let fittedX, fittedY;
  // Resolve the larger movement first so a diagonal drag can pass a neighbour.
  if (Math.abs(deltaY / canvasHeight) > Math.abs(deltaX / canvasWidth)) {
    fittedY = fitPosition(rawY, ...yLimits(x), SNAP_PIXELS / canvasHeight);
    fittedX = fitPosition(rawX, ...xLimits(fittedY.value), SNAP_PIXELS / canvasWidth);
  } else {
    fittedX = fitPosition(rawX, ...xLimits(y), SNAP_PIXELS / canvasWidth);
    fittedY = fitPosition(rawY, ...yLimits(fittedX.value), SNAP_PIXELS / canvasHeight);
  }
  return {
    rect: [fittedX.value, fittedY.value, width, height],
    snapped: fittedX.snapped || fittedY.snapped,
  };
}

/** Resize from the lower-right handle, keeping a gutter before adjacent frames. */
export function resizeLayoutFrame(start, others, deltaX, deltaY, canvasWidth, canvasHeight) {
  const [x, y, width, height] = start;
  const startRight = x + width;
  const startBottom = y + height;
  const rawRight = clamp(startRight + deltaX / canvasWidth, x + MIN_WIDTH, 1);
  const rawBottom = clamp(startBottom + deltaY / canvasHeight, y + MIN_HEIGHT, 1);

  const rightLimit = bottom => {
    let limit = 1;
    for (const [otherX, otherY, , otherHeight] of others) {
      if (otherX >= startRight - EPSILON &&
          intersects(y, bottom, otherY, otherY + otherHeight)) {
        // Never shrink a frame merely because its existing gutter is narrow.
        limit = Math.min(limit, Math.max(startRight, otherX - GUTTER));
      }
    }
    return limit;
  };
  const bottomLimit = right => {
    let limit = 1;
    for (const [otherX, otherY, otherWidth] of others) {
      if (otherY >= startBottom - EPSILON &&
          intersects(x, right, otherX, otherX + otherWidth)) {
        limit = Math.min(limit, Math.max(startBottom, otherY - GUTTER));
      }
    }
    return limit;
  };

  let right, bottom;
  // A shorter frame may pass a neighbour to its right; resolve that axis first.
  if (rawBottom < startBottom - EPSILON) {
    bottom = fitEdge(rawBottom, y + MIN_HEIGHT, bottomLimit(startRight), SNAP_PIXELS / canvasHeight);
    right = fitEdge(rawRight, x + MIN_WIDTH, rightLimit(bottom.value), SNAP_PIXELS / canvasWidth);
  } else {
    right = fitEdge(rawRight, x + MIN_WIDTH, rightLimit(startBottom), SNAP_PIXELS / canvasWidth);
    bottom = fitEdge(rawBottom, y + MIN_HEIGHT, bottomLimit(right.value), SNAP_PIXELS / canvasHeight);
  }
  return {
    rect: [x, y, right.value - x, bottom.value - y],
    snapped: right.snapped || bottom.snapped,
  };
}
