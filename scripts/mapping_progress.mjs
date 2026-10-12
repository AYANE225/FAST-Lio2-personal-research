// Sensor-time prefixes shared by the offline renderer and its tests.
export function validateTimeline(preparation, timeline) {
  const count = preparation.frame_count;
  const names = ['stamp_ns', 'elapsed_s', 'recorded_point_prefix_counts', 'display_point_prefix_counts', 'positions'];
  if (timeline.schema_version !== 1 || !Number.isSafeInteger(count) || count < 2
    || names.some(name => !Array.isArray(timeline[name]) || timeline[name].length !== count)) {
    throw new Error('Invalid mapping timeline schema or frame count');
  }
  let previousStamp;
  const firstStamp = BigInt(timeline.stamp_ns[0]);
  for (let i = 0; i < count; i++) {
    if (typeof timeline.stamp_ns[i] !== 'string' || !/^-?\d+$/.test(timeline.stamp_ns[i])) throw new Error('Invalid nanosecond timestamp');
    const stamp = BigInt(timeline.stamp_ns[i]);
    const time = timeline.elapsed_s[i];
    const recorded = timeline.recorded_point_prefix_counts[i];
    const display = timeline.display_point_prefix_counts[i];
    if ((i && stamp <= previousStamp) || !Number.isFinite(time)
      || Math.abs(time - Number(stamp - firstStamp) / 1e9) > 1e-9
      || (i && time <= timeline.elapsed_s[i - 1])
      || !Number.isSafeInteger(recorded) || !Number.isSafeInteger(display)
      || display < 0 || display > recorded
      || (i && (recorded < timeline.recorded_point_prefix_counts[i - 1]
        || display < timeline.display_point_prefix_counts[i - 1]))
      || !Array.isArray(timeline.positions[i]) || timeline.positions[i].length !== 3
      || !timeline.positions[i].every(Number.isFinite)) throw new Error('Invalid mapping timeline values');
    previousStamp = stamp;
  }
  if (timeline.elapsed_s[0] !== 0 || timeline.elapsed_s.at(-1) !== preparation.source_duration_s
    || timeline.recorded_point_prefix_counts.at(-1) !== preparation.recorded_points
    || timeline.display_point_prefix_counts.at(-1) !== preparation.point_count) throw new Error('Mapping timeline totals disagree');
}

export function frameAtTime(timeline, timeSeconds) {
  if (!Number.isFinite(timeSeconds) || timeSeconds < 0) throw new Error('Invalid replay time');
  const times = timeline.elapsed_s;
  let low = 0, high = times.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (times[middle] <= timeSeconds) low = middle + 1;
    else high = middle;
  }
  return Math.max(0, low - 1);
}

export function renderSchedule(timeline, frames, holdFrames) {
  if (!Number.isSafeInteger(frames) || frames < 2 || !Number.isSafeInteger(holdFrames) || holdFrames < 0) {
    throw new Error('Invalid animation frame count');
  }
  const duration = timeline.elapsed_s.at(-1);
  const schedule = Array.from({length: frames}, (_, i) => {
    const target = i === frames - 1 ? duration : duration * i / (frames - 1);
    return {target_s: target, frame_index: frameAtTime(timeline, target), hold: false};
  });
  for (let i = 0; i < holdFrames; i++) schedule.push({target_s: duration, frame_index: timeline.elapsed_s.length - 1, hold: true});
  return schedule;
}
