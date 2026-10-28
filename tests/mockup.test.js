import test from 'node:test';
import assert from 'node:assert/strict';
import { coverCrop, quadPoint, sceneForFormat } from '../static/mockup.js';

test('neutral illustrations preserve the closed proportions of every offered Graphistudio format', () => {
  for (const [format, aspect] of Object.entries({
    gobook_portrait_25x35: 25 / 35, gobook_landscape_35x25: 35 / 25,
    portrait_30x40: 30 / 40, landscape_40x30: 40 / 30,
  })) {
    const scene = sceneForFormat(format);
    assert.equal(scene.aspect, aspect);
    assert.equal(scene.background, undefined);
    const width = scene.quad[1][0] - scene.quad[0][0];
    const height = scene.quad[3][1] - scene.quad[0][1];
    assert(Math.abs(width / height - aspect) < 1e-9);
  }
  assert.equal(sceneForFormat('unknown'), null);
});

test('cover crop fills the book face without stretching a portrait image', () => {
  assert.deepEqual(coverCrop(4000, 6000, 4 / 3),
    {left: 0, top: 1500, width: 4000, height: 3000});
  assert.deepEqual(coverCrop(6000, 4000, 8 / 7),
    {left: (6000 - 4000 * 8 / 7) / 2, top: 0, width: 4000 * 8 / 7, height: 4000});
});

test('perspective quad maps all four photograph corners to the illustrated cover surface', () => {
  const quad = [[10,20],[90,25],[75,80],[5,70]];
  assert.deepEqual(quadPoint(quad,0,0),quad[0]);
  assert.deepEqual(quadPoint(quad,1,0),quad[1]);
  assert.deepEqual(quadPoint(quad,1,1),quad[2]);
  assert.deepEqual(quadPoint(quad,0,1),quad[3]);
});
