// The editor's arithmetic: a clip's ends and the parts cut out of it, all in the stream's seconds.

export type Range = [number, number];

/** Where a clip starts and ends in the stream, and the parts cut out of its middle. */
export interface Geometry {
  start: number;
  end: number;
  cuts: Range[];
}

export const FPS = 30; // the preview copy plays at 30 frames a second
export const FRAME = 1 / FPS;
export const MIN_CLIP_S = 1;
export const MAX_SHORT_S = 180;
// The same as the server's (backend/clipper/cuts.py), so what's saved is what's shown.
const MIN_CUT_S = 0.05;
const EDGE_S = 0.01;

export const round2 = (t: number) => Math.round(t * 100) / 100;
export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** Cuts kept inside the clip, sorted, overlapping ones merged and slivers dropped; a cut that reaches an end of
 * the clip trims the clip instead. The server does the same. */
export function tidy({ start, end, cuts }: Geometry): Geometry {
  const merged: Range[] = [];
  for (const [x, y] of [...cuts].map(([a, b]) => [Math.min(a, b), Math.max(a, b)]).sort((p, q) => p[0] - q[0])) {
    const a = Math.max(x, start);
    const b = Math.min(y, end);
    if (b - a < MIN_CUT_S) continue;
    const last = merged[merged.length - 1];
    if (last && a <= last[1] + EDGE_S) last[1] = Math.max(last[1], b);
    else merged.push([a, b]);
  }
  if (merged.length && merged[0][0] <= start + EDGE_S) start = merged.shift()![1];
  if (merged.length && merged[merged.length - 1][1] >= end - EDGE_S) end = merged.pop()![0];
  return { start: round2(start), end: round2(end), cuts: merged.map(([a, b]) => [round2(a), round2(b)]) };
}

/** The parts of the clip that stay. */
export function kept({ start, end, cuts }: Geometry): Range[] {
  const parts: Range[] = [];
  let at = start;
  for (const [a, b] of cuts) {
    if (a > at) parts.push([at, a]);
    at = Math.max(at, b);
  }
  if (end > at) parts.push([at, end]);
  return parts;
}

/** How long the clip comes out, with the cuts taken out. */
export const keptLength = (g: Geometry) => kept(g).reduce((sum, [a, b]) => sum + b - a, 0);

/** How far into the finished clip a stream time falls. Time spent in cuts doesn't count. */
export const clipTime = (g: Geometry, t: number) =>
  kept(g).reduce((sum, [a, b]) => sum + clamp(t - a, 0, b - a), 0);

/** Where in the stream the clip, as it comes out, is `t` seconds in. */
export function streamTime(g: Geometry, t: number): number {
  let left = Math.max(0, t);
  for (const [a, b] of kept(g)) {
    if (left <= b - a) return a + left;
    left -= b - a;
  }
  return g.end;
}

export const MAX_CUTS = 500; // the same as the server's
// Left either side of a pause that's cut out, so the words around it aren't clipped (word timings are a little loose).
const PAUSE_KEEP_AFTER = 0.3;
const PAUSE_KEEP_BEFORE = 0.2;

/**
 * The pauses between the clip's words (as [start, end] in the stream) of at least `minPause` seconds, as parts to cut
 * out, less a little either side. Only those between words: the clip's own start and end stay where they are. At most
 * `limit`, the longest ones.
 */
export function pauseCuts(words: Range[], g: Geometry, minPause: number, limit = MAX_CUTS): Range[] {
  const found: Range[] = [];
  let spoken = -Infinity; // the end of the words so far
  for (const [a, b] of words.filter(([a, b]) => b > g.start && a < g.end).sort((p, q) => p[0] - q[0])) {
    if (spoken > -Infinity && a - spoken >= minPause) found.push([round2(spoken + PAUSE_KEEP_AFTER), round2(a - PAUSE_KEEP_BEFORE)]);
    spoken = Math.max(spoken, b);
  }
  return found
    .sort((p, q) => q[1] - q[0] - (p[1] - p[0]))
    .slice(0, Math.max(0, limit))
    .sort((p, q) => p[0] - q[0]);
}

/** The cut that `t` is in, or -1. */
export const cutAt = (cuts: Range[], t: number) => cuts.findIndex(([a, b]) => t >= a && t < b);

export const sameCuts = (a: Range[], b: Range[]) => a.length === b.length && a.every((r, i) => r[0] === b[i][0] && r[1] === b[i][1]);

export const sameGeometry = (a: Geometry, b: Geometry) => a.start === b.start && a.end === b.end && sameCuts(a.cuts, b.cuts);

/** The clip with another part cut out (merged with any it overlaps). */
export const withCut = (g: Geometry, [a, b]: Range): Geometry => tidy({ ...g, cuts: [...g.cuts, [a, b]] });

export const withoutCut = (g: Geometry, i: number): Geometry => ({ ...g, cuts: g.cuts.filter((_, k) => k !== i) });

/** The whole clip moved by `dt`, cuts and all, kept inside the stream. */
export function shifted(g: Geometry, dt: number, length: number): Geometry {
  const d = clamp(dt, -g.start, length - g.end);
  return {
    start: round2(g.start + d),
    end: round2(g.end + d),
    cuts: g.cuts.map(([a, b]) => [round2(a + d), round2(b + d)]),
  };
}

/** A new start: past the end, the clip moves along with it, keeping its length. */
export function withStart(g: Geometry, t: number, length: number, push = false): Geometry {
  if (push && t > g.end - MIN_CLIP_S) return shifted({ ...g, cuts: [] }, t - g.start, length);
  return tidy({ ...g, start: round2(clamp(t, 0, g.end - MIN_CLIP_S)) });
}

/** A new end: before the start, the clip moves back with it, keeping its length. */
export function withEnd(g: Geometry, t: number, length: number, push = false): Geometry {
  if (push && t < g.start + MIN_CLIP_S) return shifted({ ...g, cuts: [] }, t - g.end, length);
  return tidy({ ...g, end: round2(clamp(t, g.start + MIN_CLIP_S, length)) });
}

const pad = (n: number, width = 2) => String(n).padStart(width, "0");

/** A moment as "1:02:03.45" or "2:03.45". */
export function timecode(t: number, decimals = 2): string {
  const s = Math.max(0, t);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const scale = 10 ** decimals;
  const secs = Math.floor((s % 60) * scale + 1e-6) / scale;
  const sec = decimals ? secs.toFixed(decimals).padStart(3 + decimals, "0") : pad(Math.floor(secs));
  return h ? `${h}:${pad(m)}:${sec}` : `${m}:${sec}`;
}

/** "1:02:03.45", "62:03.4", "3723.45" or "1h2m3s" to seconds, or null if it isn't a time. */
export function parseTimecode(text: string): number | null {
  const t = text.trim();
  if (!t) return null;
  const hms = /^(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?$/i.exec(t);
  if (hms && (hms[1] || hms[2] || hms[3])) return (Number(hms[1] ?? 0) * 3600) + Number(hms[2] ?? 0) * 60 + Number(hms[3] ?? 0);
  const parts = t.split(":");
  if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
  return parts.reduce((total, p) => total * 60 + Number(p), 0);
}

/** Tick spacing for a timeline, in seconds: round numbers of frames, seconds, minutes and hours. */
export const STEPS = [FRAME, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];

/** The smallest step at least `min` seconds. */
export const stepAtLeast = (min: number) => STEPS.find((s) => s >= min) ?? STEPS[STEPS.length - 1];
