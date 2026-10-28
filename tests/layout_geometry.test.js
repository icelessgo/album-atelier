import assert from 'node:assert/strict';
import { test } from 'node:test';
import { moveLayoutFrame, resizeLayoutFrame } from '../static/layout_geometry.js';

const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 0.00001,
  `expected ${actual} to equal ${expected}`);
const overlaps = (a, b) => Math.min(a[0] + a[2], b[0] + b[2]) - Math.max(a[0], b[0]) > 0.0001 &&
  Math.min(a[1] + a[3], b[1] + b[3]) - Math.max(a[1], b[1]) > 0.0001;

test('resize snaps to the gutter and cannot enter the neighbouring frame', () => {
  const left = [0.025, 0.045, 0.465, 0.91];
  const right = [0.51, 0.045, 0.465, 0.91];
  const nearby = resizeLayoutFrame(left, [right], -6, 0, 1000, 300);
  close(nearby.rect[2], 0.465);
  assert.equal(nearby.snapped, true);
  const overshot = resizeLayoutFrame(left, [right], 200, 0, 1000, 300);
  close(overshot.rect[2], 0.465);
  assert.equal(overlaps(overshot.rect, right), false);
  const smaller = resizeLayoutFrame(left, [right], -90, 0, 1000, 300);
  close(smaller.rect[2], 0.375);
});

test('resize snaps vertically and to the spread boundary', () => {
  const top = [0.025, 0.045, 0.465, 0.445];
  const bottom = [0.025, 0.51, 0.465, 0.445];
  const fitted = resizeLayoutFrame(top, [bottom], 0, 100, 1000, 300);
  close(fitted.rect[3], 0.445);
  assert.equal(overlaps(fitted.rect, bottom), false);
  const boundary = resizeLayoutFrame([0.025, 0.045, 0.5, 0.5], [], 468, 0, 1000, 300);
  close(boundary.rect[2], 0.975);
});

test('diagonal resize can widen while stopping before a frame below', () => {
  const first = [0.025, 0.045, 0.465, 0.445];
  const diagonal = [0.51, 0.51, 0.465, 0.445];
  const fitted = resizeLayoutFrame(first, [diagonal], 400, 150, 1000, 300);
  close(fitted.rect[2], 0.865);
  close(fitted.rect[3], 0.445);
  assert.equal(overlaps(fitted.rect, diagonal), false);
});

test('a shorter frame can extend past a lower neighbour on its right', () => {
  const first = [0.025, 0.045, 0.465, 0.91];
  const lowerRight = [0.51, 0.60, 0.465, 0.35];
  const fitted = resizeLayoutFrame(first, [lowerRight], 300, -180, 1000, 300);
  close(fitted.rect[2], 0.765);
  close(fitted.rect[3], 0.31);
  assert.equal(overlaps(fitted.rect, lowerRight), false);
});

test('resizing preserves an existing gutter that is smaller than the default', () => {
  const first = [0.02, 0.05, 0.48, 0.9];
  const next = [0.51, 0.05, 0.47, 0.9];
  const fitted = resizeLayoutFrame(first, [next], 200, 0, 1000, 300);
  close(fitted.rect[2], 0.48);
  assert.equal(overlaps(fitted.rect, next), false);
});

test('moving frames snaps to adjacent gutters and blocks overlap', () => {
  const left = [0.025, 0.045, 0.465, 0.91];
  const right = [0.51, 0.045, 0.465, 0.91];
  const pushed = moveLayoutFrame(left, [right], 120, 0, 1000, 300);
  close(pushed.rect[0], 0.025);
  assert.equal(overlaps(pushed.rect, right), false);
  const pulled = moveLayoutFrame(left, [right], -18, 0, 1000, 300);
  close(pulled.rect[0], 0);
  assert.equal(pulled.snapped, true);
});

test('moving a frame towards a lower neighbour stops at its gutter', () => {
  const top = [0.025, 0.045, 0.465, 0.445];
  const bottom = [0.025, 0.51, 0.465, 0.445];
  const pushed = moveLayoutFrame(top, [bottom], 0, 100, 1000, 300);
  close(pushed.rect[1], 0.045);
  assert.equal(overlaps(pushed.rect, bottom), false);
});

test('dragging across a four-photo grid never creates an overlap', () => {
  const grid = [[0.025, 0.045, 0.465, 0.445], [0.51, 0.045, 0.465, 0.445],
    [0.025, 0.51, 0.465, 0.445], [0.51, 0.51, 0.465, 0.445]];
  for (let index = 0; index < grid.length; index++) {
    const others = grid.filter((_, otherIndex) => otherIndex !== index);
    for (const drag of [moveLayoutFrame, resizeLayoutFrame]) {
      for (let dx = -400; dx <= 400; dx += 80) {
        for (let dy = -150; dy <= 150; dy += 50) {
          const { rect } = drag(grid[index], others, dx, dy, 1000, 300);
          assert.ok(rect[0] >= 0 && rect[1] >= 0 &&
            rect[0] + rect[2] <= 1.000001 && rect[1] + rect[3] <= 1.000001);
          assert.ok(others.every(other => !overlaps(rect, other)),
            `${drag.name} overlapped frame ${index + 1} after ${dx},${dy}`);
        }
      }
    }
  }
});
