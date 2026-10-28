import test from 'node:test';
import assert from 'node:assert/strict';
import { cropGeometry, reflowSlots, swapSlots } from '../static/editing.js';

test('reflow keeps each existing photo crop when layout recommendation changes order', () => {
  const before = [
    { photo_id: 'a', focus_x: .2, focus_y: .7 },
    { photo_id: 'b', focus_x: .8, focus_y: .1 },
  ];
  const result = reflowSlots(before, ['b', 'c', 'a']);
  assert.deepEqual(result, [before[1], { photo_id: 'c', focus_x: .5, focus_y: .5 }, before[0]]);
  result[0].focus_x = .1;
  assert.equal(before[1].focus_x, .8);
});

test('reflow preserves separate crops when a photo appears twice', () => {
  const slots = [{ photo_id: 'a', focus_x: .1 }, { photo_id: 'a', focus_x: .9 }];
  assert.deepEqual(reflowSlots(slots, ['a', 'a']), slots);
});

test('swap moves whole photo slots with their focal points', () => {
  const slots = [{ photo_id: 'a', focus_x: .1 }, { photo_id: 'b', focus_x: .9 }];
  assert.deepEqual(swapSlots(slots, 0, 1), [slots[1], slots[0]]);
  assert.equal(slots[0].photo_id, 'a');
  assert.throws(() => swapSlots(slots, 0, 0), RangeError);
});

test('crop inspection uses the original pixels and selected focal point', () => {
  assert.deepEqual(cropGeometry(6000, 4000, 1, .25, .5),
    { cropWidth: 4000, cropHeight: 4000, left: 500, top: 0 });
  assert.deepEqual(cropGeometry(4000, 6000, 2, .5, 1),
    { cropWidth: 4000, cropHeight: 2000, left: 0, top: 4000 });
});
