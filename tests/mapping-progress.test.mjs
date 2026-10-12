import test from 'node:test';
import assert from 'node:assert/strict';

async function api() {
  const module = await import('../scripts/mapping_progress.mjs').catch(() => null);
  assert.ok(module, 'mapping-progress timeline helpers are not implemented');
  return module;
}

function example() {
  return {schema_version: 1, stamp_ns: ['1690000000000000007', '1690000000100000007', '1690000000300000007'],
    elapsed_s: [0, .1, .3], recorded_point_prefix_counts: [2, 5, 7], display_point_prefix_counts: [2, 3, 4],
    positions: [[0, 0, 0], [1, 0, 0], [2, 0, 0]]};
}

const preparation = {frame_count: 3, recorded_points: 7, point_count: 4, source_duration_s: .3};

test('between sensor frames the display uses the last fully observed frame', async () => {
  const {frameAtTime} = await api();
  assert.equal(frameAtTime(example(), 0), 0);
  assert.equal(frameAtTime(example(), .099), 0);
  assert.equal(frameAtTime(example(), .1), 1);
  assert.equal(frameAtTime(example(), .299), 1);
  assert.equal(frameAtTime(example(), .3), 2);
  assert.equal(frameAtTime(example(), 1), 2);
});

test('render schedule starts at the first observation and holds the completed map', async () => {
  const {renderSchedule} = await api();
  const schedule = renderSchedule(example(), 4, 2);
  assert.equal(schedule.length, 6);
  assert.equal(schedule[0].frame_index, 0);
  assert.ok(schedule.slice(0, 4).every(row => example().elapsed_s[row.frame_index] <= row.target_s));
  assert.deepEqual(schedule.slice(-3).map(row => row.frame_index), [2, 2, 2]);
});

test('invalid chronology, totals and nanosecond conversion fail before rendering', async () => {
  const {validateTimeline} = await api();
  validateTimeline(preparation, example());
  for (const change of [
    {elapsed_s: [0, .2, .1]}, {display_point_prefix_counts: [2, 5, 4]},
    {recorded_point_prefix_counts: [2, 5, 8]}, {display_point_prefix_counts: [3, 3, 4]},
    {stamp_ns: ['1690000000000000007', '1690000000200000007', '1690000000300000007']},
    {positions: [[0, 0, 0], [NaN, 0, 0], [2, 0, 0]]},
  ]) assert.throws(() => validateTimeline(preparation, {...example(), ...change}));
});
