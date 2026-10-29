// The desktop editor's timeline: a ruler, stills of the stream, its waveform and transcript (words, once zoomed in),
// with the clip's ends, the parts cut out of it, a selection and the playhead on top. Zoom with ctrl or ⌘ and the
// scroll wheel (or a pinch), scroll sideways to pan, and drag the overview bar under it to move along the stream.

import { memo, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api, media, type Line } from "../api";
import { cn } from "../lib";
import {
  clamp, cutAt, keptLength, MIN_CLIP_S, round2, shifted, stepAtLeast, timecode, tidy, type Geometry, type Range,
} from "./edit";
import type { View } from "./useEditor";

type Word = { start: number; end: number; text: string };

const RULER_H = 22;
const GRIP_H = 14; // the bar above the clip that moves it
const FILM_H = 44;
const WAVE_H = 52;
const TEXT_H = 26;
const LANES_H = GRIP_H + FILM_H + WAVE_H + TEXT_H;
const SNAP_PX = 8;
const TILE_W = Math.round((FILM_H * 16) / 9);
const WORDS_SPAN_S = 600; // words are fetched once the view is this short or shorter
const WORD_PX_PER_S = 25; // and shown instead of whole lines from this zoom
const MAX_BLOCKS = 800;
const WAVE_IN = "#a78bfa"; // violet-400: the parts that stay
const WAVE_OUT = "#52525b"; // zinc-600: outside the clip
const WAVE_CUT = "rgba(248,113,113,0.35)"; // red-400, faint: cut out

type Drag =
  | { kind: "scrub" }
  | { kind: "start" | "end"; g0: Geometry }
  | { kind: "move"; g0: Geometry; t0: number }
  | { kind: "cut-a" | "cut-b"; g0: Geometry; i: number }
  | { kind: "cut-move"; g0: Geometry; i: number; t0: number }
  | { kind: "select"; t0: number; x0: number; moved: boolean };

/** What a snap leaves out: the edge being dragged, which would otherwise hold on to where it was. */
type Skip = { start?: boolean; end?: boolean; cut?: number };

export function Timeline(props: {
  vodId: number;
  length: number; // the stream's
  view: View;
  onView: (v: View) => void;
  onZoom: (factor: number, anchor: number) => void;
  geometry: Geometry;
  editable: boolean;
  now: number;
  lines: Line[];
  selection: Range | null;
  onSelection: (r: Range | null) => void;
  selectedCut: number;
  onSelectCut: (i: number) => void;
  onRemoveCut: (i: number) => void;
  snapOn: boolean;
  onSeek: (t: number) => void;
  live: { begin: () => void; move: (g: Geometry) => void; end: () => void };
  onEdge: (edge: "start" | "end") => void; // the last edge moved, which , and . nudge
}) {
  const { view, geometry: g, length, now } = props;
  const root = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const span = view.to - view.from;
  const pps = width / span || 1; // pixels per second
  const x = (t: number) => (t - view.from) * pps;
  const timeAt = (clientX: number) => {
    const r = root.current!.getBoundingClientRect();
    return clamp(view.from + (clientX - r.left) / pps, 0, length);
  };

  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Ctrl or ⌘ with the wheel (a pinch on a trackpad) zooms around the pointer; the wheel alone pans. It needs a
  // listener that can stop the page scrolling, which React's isn't.
  const latest = useRef({ view, pps, onView: props.onView, onZoom: props.onZoom });
  latest.current = { view, pps, onView: props.onView, onZoom: props.onZoom };
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const { view: v, pps: p, onView, onZoom } = latest.current;
      const at = v.from + (e.clientX - el.getBoundingClientRect().left) / p;
      if (e.ctrlKey || e.metaKey) onZoom(Math.exp(e.deltaY * 0.0025), at);
      else {
        const dx = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        onView({ from: v.from + dx / p, to: v.to + dx / p });
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // --- What's in view ---

  const visibleLines = useMemo(
    () => props.lines.filter((l) => l.end > view.from && l.start < view.to),
    [props.lines, view.from, view.to],
  );
  const words = useWords(props.vodId, view, length);
  const visibleWords = useMemo(() => words.filter((w) => w.end > view.from && w.start < view.to), [words, view.from, view.to]);
  const showWords = pps >= WORD_PX_PER_S && visibleWords.length > 0;
  const peaks = usePeaks(props.vodId, view, length, width);

  // --- Snapping, to the playhead, the clip's ends and cuts, and the transcript's lines and words ---

  const [snapAt, setSnapAt] = useState<number | null>(null);
  const snap = (t: number, e: { altKey: boolean }, from: Geometry, skip: Skip = {}): number => {
    if (!props.snapOn || e.altKey) {
      setSnapAt(null);
      return t;
    }
    let best = t;
    let distance = SNAP_PX / pps;
    const consider = (c: number) => {
      const d = Math.abs(c - t);
      if (d < distance) [best, distance] = [c, d];
    };
    consider(now);
    if (!skip.start) consider(from.start);
    if (!skip.end) consider(from.end);
    from.cuts.forEach(([a, b], i) => i !== skip.cut && (consider(a), consider(b)));
    for (const l of visibleLines) (consider(l.start), consider(l.end));
    if (showWords) for (const w of visibleWords) (consider(w.start), consider(w.end));
    setSnapAt(best !== t ? best : null);
    return round2(best);
  };

  // --- Dragging ---

  const drag = useRef<Drag | null>(null);
  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const hit = (e.target as HTMLElement).closest<HTMLElement>("[data-hit]");
    const what = hit?.dataset.hit ?? "lane";
    if (what === "cut-remove") return; // its own button
    const t = timeAt(e.clientX);
    const i = Number(hit?.dataset.cut ?? -1);
    e.currentTarget.setPointerCapture(e.pointerId);
    if (what === "ruler") {
      drag.current = { kind: "scrub" };
      props.onSeek(t);
    } else if ((what === "start" || what === "end") && props.editable) {
      props.live.begin();
      props.onEdge(what);
      drag.current = { kind: what, g0: g };
    } else if (what === "move" && props.editable) {
      props.live.begin();
      drag.current = { kind: "move", g0: g, t0: t };
    } else if ((what === "cut-a" || what === "cut-b") && props.editable) {
      props.onSelectCut(i);
      props.live.begin();
      drag.current = { kind: what, g0: g, i };
    } else if (what === "cut-body" && props.editable) {
      props.onSelectCut(i);
      props.onSelection(null);
      props.live.begin();
      drag.current = { kind: "cut-move", g0: g, i, t0: t };
    } else {
      drag.current = { kind: "select", t0: snap(t, e, g), x0: e.clientX, moved: false };
    }
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    const t = timeAt(e.clientX);
    if (d.kind === "scrub") props.onSeek(t);
    else if (d.kind === "start") {
      const next = tidy({ ...d.g0, start: clamp(snap(t, e, d.g0, { start: true }), 0, d.g0.end - MIN_CLIP_S) });
      props.live.move(next);
      props.onSeek(next.start);
    } else if (d.kind === "end") {
      const next = tidy({ ...d.g0, end: clamp(snap(t, e, d.g0, { end: true }), d.g0.start + MIN_CLIP_S, length) });
      props.live.move(next);
      props.onSeek(next.end);
    } else if (d.kind === "move") {
      let moved = shifted(d.g0, t - d.t0, length);
      const start = snap(moved.start, e, d.g0, { start: true, end: true });
      if (start !== moved.start) moved = shifted(d.g0, start - d.g0.start, length);
      else {
        const end = snap(moved.end, e, d.g0, { start: true, end: true });
        if (end !== moved.end) moved = shifted(d.g0, end - d.g0.end, length);
      }
      props.live.move(moved);
    } else if (d.kind === "cut-a" || d.kind === "cut-b") {
      const [a, b] = d.g0.cuts[d.i];
      const edge = snap(t, e, d.g0, { cut: d.i });
      const cut: Range = d.kind === "cut-a" ? [clamp(edge, d.g0.start, b - 0.05), b] : [a, clamp(edge, a + 0.05, d.g0.end)];
      props.live.move(tidy({ ...d.g0, cuts: d.g0.cuts.map((c, k) => (k === d.i ? cut : c)) }));
      props.onSeek(d.kind === "cut-a" ? cut[0] : cut[1]);
    } else if (d.kind === "cut-move") {
      const [a, b] = d.g0.cuts[d.i];
      let dt = clamp(t - d.t0, d.g0.start - a, d.g0.end - b);
      const snapped = snap(a + dt, e, d.g0, { cut: d.i });
      if (snapped !== a + dt) dt = clamp(snapped - a, d.g0.start - a, d.g0.end - b);
      const cut: Range = [round2(a + dt), round2(b + dt)];
      props.live.move(tidy({ ...d.g0, cuts: d.g0.cuts.map((c, k) => (k === d.i ? cut : c)) }));
    } else if (d.kind === "select") {
      if (!d.moved && Math.abs(e.clientX - d.x0) < 3) return;
      d.moved = true;
      const t1 = snap(t, e, g);
      props.onSelection([Math.min(d.t0, t1), Math.max(d.t0, t1)]);
    }
  };

  const onPointerUp = () => {
    const d = drag.current;
    drag.current = null;
    setSnapAt(null);
    if (!d) return;
    if (d.kind !== "scrub" && d.kind !== "select") props.live.end();
    if (d.kind === "select" && !d.moved) {
      props.onSelection(null);
      props.onSelectCut(-1);
      props.onSeek(d.t0);
    }
  };

  // A double click selects the word (or line) under the pointer, ready to cut out.
  const onDoubleClick = (e: React.MouseEvent) => {
    const t = timeAt(e.clientX);
    const found = (showWords ? visibleWords : visibleLines).find((w) => t >= w.start && t <= w.end);
    if (found) props.onSelection([found.start, found.end]);
  };

  const cuts = g.cuts;
  const sel = props.selection;
  const clipLeft = x(g.start);
  const clipRight = x(g.end);
  const keptS = keptLength(g);

  return (
    <div className="select-none">
      <div
        ref={root}
        className="relative touch-none overflow-hidden rounded-lg bg-zinc-900 ring-1 ring-white/5"
        style={{ height: RULER_H + LANES_H }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={onDoubleClick}
      >
        {width > 0 && (
          <>
            <Ruler view={view} width={width} />
            <div className="absolute inset-x-0" style={{ top: RULER_H + GRIP_H, height: FILM_H }}>
              <Filmstrip vodId={props.vodId} view={view} width={width} length={length} />
            </div>
            <div className="absolute inset-x-0" style={{ top: RULER_H + GRIP_H + FILM_H, height: WAVE_H }}>
              <Waveform peaks={peaks} view={view} width={width} geometry={g} />
            </div>
            <div className="absolute inset-x-0" style={{ top: RULER_H + GRIP_H + FILM_H + WAVE_H, height: TEXT_H }}>
              <TextLane blocks={showWords ? visibleWords : visibleLines} view={view} width={width} now={now} words={showWords} />
            </div>

            {/* Outside the clip, dimmed */}
            <div className="pointer-events-none absolute left-0 bg-black/45" style={{ top: RULER_H, height: LANES_H, width: clamp(clipLeft, 0, width) }} />
            <div className="pointer-events-none absolute right-0 bg-black/45" style={{ top: RULER_H, height: LANES_H, width: clamp(width - clipRight, 0, width) }} />

            {/* The clip: a bar to move it, and its edges */}
            <div
              className="pointer-events-none absolute border-x-2 border-violet-400 bg-violet-500/[0.07]"
              style={{ top: RULER_H, height: LANES_H, left: clipLeft, width: Math.max(2, clipRight - clipLeft) }}
            />
            <div
              data-hit="move"
              title={props.editable ? "Drag to move the whole clip" : undefined}
              className={cn(
                "absolute flex items-center overflow-hidden whitespace-nowrap bg-violet-500/80 px-2 text-[10px] font-semibold text-white",
                props.editable && "cursor-grab active:cursor-grabbing",
              )}
              style={{ top: RULER_H, height: GRIP_H, left: clipLeft, width: Math.max(2, clipRight - clipLeft) }}
            >
              Clip · {timecode(keptS, 1)}
              {cuts.length > 0 && ` · ${cuts.length} cut${cuts.length === 1 ? "" : "s"}`}
            </div>

            {/* Parts cut out */}
            {cuts.map(([a, b], i) => {
              const left = x(a);
              const w = Math.max(2, x(b) - left);
              const selected = props.selectedCut === i;
              return (
                <div
                  key={i}
                  data-hit="cut-body"
                  data-cut={i}
                  title={`Cut out: ${timecode(a)} to ${timecode(b)}. Drag to move it; select it and press Delete to put it back.`}
                  className={cn("absolute cursor-move border-x border-red-400/80", selected && "ring-2 ring-inset ring-red-300")}
                  style={{
                    top: RULER_H + GRIP_H, height: LANES_H - GRIP_H, left, width: w,
                    background: "repeating-linear-gradient(135deg, rgba(239,68,68,0.38) 0 6px, rgba(9,9,11,0.6) 6px 12px)",
                  }}
                >
                  {w > 44 && (
                    <span className="pointer-events-none absolute inset-x-0 top-1 truncate text-center text-[10px] font-bold text-red-100">
                      −{timecode(b - a, 1)}
                    </span>
                  )}
                  {selected && props.editable && (
                    <>
                      <span data-hit="cut-a" data-cut={i} className="absolute inset-y-0 -left-1.5 w-3 cursor-ew-resize" />
                      <span data-hit="cut-b" data-cut={i} className="absolute inset-y-0 -right-1.5 w-3 cursor-ew-resize" />
                      {w > 22 && (
                        <button
                          data-hit="cut-remove"
                          onPointerDown={(e) => e.stopPropagation()}
                          onClick={() => props.onRemoveCut(i)}
                          className="absolute bottom-1 left-1/2 -translate-x-1/2 rounded bg-zinc-900/90 px-1.5 text-[10px] font-semibold text-zinc-200 hover:bg-zinc-800"
                          title="Put this part back"
                        >
                          Put back
                        </button>
                      )}
                    </>
                  )}
                </div>
              );
            })}

            {/* The selection */}
            {sel && (
              <div
                className="pointer-events-none absolute border-x border-sky-300 bg-sky-400/20"
                style={{ top: RULER_H, height: LANES_H, left: x(sel[0]), width: Math.max(1, x(sel[1]) - x(sel[0])) }}
              >
                <span className="absolute left-1 top-0.5 whitespace-nowrap rounded bg-sky-500/90 px-1 text-[10px] font-semibold text-white">
                  {timecode(sel[1] - sel[0], 2)}
                </span>
              </div>
            )}

            {/* Edge handles, on top of everything they might overlap */}
            {props.editable &&
              (["start", "end"] as const).map((edge) => (
                <div
                  key={edge}
                  data-hit={edge}
                  role="slider"
                  aria-label={edge === "start" ? "Clip start" : "Clip end"}
                  aria-valuenow={edge === "start" ? g.start : g.end}
                  title={`${edge === "start" ? "Start" : "End"}: ${timecode(edge === "start" ? g.start : g.end)}. Drag to trim; hold Alt to stop it snapping.`}
                  className="group absolute flex w-3 -translate-x-1/2 cursor-ew-resize justify-center"
                  style={{ top: RULER_H, height: LANES_H, left: edge === "start" ? clipLeft : clipRight }}
                >
                  <span className="h-full w-1 rounded-full bg-violet-300 shadow shadow-black/60 group-hover:bg-white" />
                </div>
              ))}

            {snapAt !== null && (
              <div className="pointer-events-none absolute w-px border-l border-dashed border-amber-300" style={{ top: 0, height: RULER_H + LANES_H, left: x(snapAt) }} />
            )}

            {/* The playhead */}
            {now >= view.from && now <= view.to && (
              <div className="pointer-events-none absolute top-0 w-0" style={{ left: x(now), height: RULER_H + LANES_H }}>
                <div className="absolute -left-[5px] top-0 size-0 border-x-[5px] border-t-[7px] border-x-transparent border-t-white" />
                <div className="absolute -left-px top-0 h-full w-0.5 bg-white" />
              </div>
            )}
          </>
        )}
      </div>
      <Overview length={length} view={view} geometry={g} now={now} onView={props.onView} />
    </div>
  );
}

/** Words with their timings, for the stretch around the view, once it's zoomed in enough for them to matter. */
function useWords(vodId: number, view: View, length: number): Word[] {
  const [loaded, setLoaded] = useState<{ from: number; to: number; words: Word[] }>({ from: 0, to: 0, words: [] });
  const span = view.to - view.from;
  useEffect(() => {
    if (span > WORDS_SPAN_S || (view.from >= loaded.from && view.to <= loaded.to)) return;
    const timer = setTimeout(() => {
      const from = Math.max(0, view.from - span);
      const to = Math.min(length, view.to + span);
      api.vodTranscript(vodId, { start: round2(from), end: round2(to), words: true }).then(
        (lines) => setLoaded({ from, to, words: lines.flatMap((l) => (l.words ?? []).map(([s, e, text]) => ({ start: s, end: e, text }))) }),
        () => undefined,
      );
    }, 250);
    return () => clearTimeout(timer);
  }, [vodId, view.from, view.to, span, length, loaded.from, loaded.to]);
  return loaded.words;
}

type Peaks = { start: number; end: number; peaks: number[] };

/** The waveform for a stretch around the view, fetched again once the view needs more detail or moves off it. */
function usePeaks(vodId: number, view: View, length: number, width: number): Peaks | null {
  const [peaks, setPeaks] = useState<Peaks | null>(null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    if (!width || missing) return;
    const span = view.to - view.from;
    const enough = peaks && view.from >= peaks.start && view.to <= peaks.end
      && peaks.peaks.length / (peaks.end - peaks.start) >= (0.8 * width) / span;
    if (enough) return;
    const timer = setTimeout(() => {
      const start = round2(Math.max(0, view.from - span / 2));
      const end = round2(Math.min(length, view.to + span / 2));
      const count = clamp(Math.round((width * (end - start)) / span), 1, 4000);
      api.peaks(vodId, start, end, count).then(setPeaks, (e) => {
        if ((e as { status?: number }).status === 404) setMissing(true);
      });
    }, 120);
    return () => clearTimeout(timer);
  }, [vodId, view.from, view.to, length, width, peaks, missing]);
  return peaks;
}

const Ruler = memo(function Ruler({ view, width }: { view: View; width: number }) {
  const pps = width / (view.to - view.from);
  const major = stepAtLeast(80 / pps);
  const minor = stepAtLeast(8 / pps);
  const decimals = major < 1 ? (major < 0.1 ? 2 : 1) : 0;
  const ticks = [];
  for (let k = Math.floor(view.from / minor); k * minor <= view.to; k++) {
    const t = k * minor;
    const isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
    const left = (t - view.from) * pps;
    ticks.push(
      <div key={k} className="absolute bottom-0" style={{ left }}>
        <div className={cn("w-px", isMajor ? "h-2.5 bg-zinc-400" : "h-1.5 bg-zinc-600")} />
        {isMajor && (
          <span className="absolute bottom-3 left-1 whitespace-nowrap font-mono text-[10px] tabular-nums text-zinc-400">
            {timecode(t, decimals)}
          </span>
        )}
      </div>,
    );
  }
  return (
    <div data-hit="ruler" title="Click or drag to move the playhead" className="absolute inset-x-0 top-0 cursor-col-resize border-b border-white/10 bg-zinc-950" style={{ height: RULER_H }}>
      {ticks}
    </div>
  );
});

const Filmstrip = memo(function Filmstrip({ vodId, view, width, length }: { vodId: number; view: View; width: number; length: number }) {
  const pps = width / (view.to - view.from);
  // Round steps, so panning and zooming back reuse stills already fetched.
  const step = stepAtLeast(Math.max(0.5, TILE_W / pps));
  const tiles = [];
  for (let k = Math.floor(view.from / step); k * step < Math.min(view.to, length); k++) {
    const t = k * step;
    tiles.push(
      <img
        key={t}
        src={media.frame(vodId, t)}
        alt=""
        draggable={false}
        onError={(e) => (e.currentTarget.style.visibility = "hidden")}
        className="absolute top-0 h-full object-cover"
        style={{ left: (t - view.from) * pps, width: Math.max(TILE_W, step * pps) }}
      />,
    );
  }
  return <div className="relative h-full overflow-hidden bg-zinc-950">{tiles}</div>;
});

function Waveform({ peaks, view, width, geometry }: { peaks: Peaks | null; view: View; width: number; geometry: Geometry }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.round(width * dpr);
    c.height = Math.round(WAVE_H * dpr);
    const ctx = c.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, WAVE_H);
    if (!peaks) return;
    const n = peaks.peaks.length;
    const pspan = peaks.end - peaks.start;
    const pps = width / (view.to - view.from);
    const mid = WAVE_H / 2;
    for (let px = 0; px < width; px++) {
      const t0 = view.from + px / pps;
      const t1 = view.from + (px + 1) / pps;
      const i0 = Math.max(0, Math.floor(((t0 - peaks.start) / pspan) * n));
      const i1 = Math.min(n, Math.max(i0 + 1, Math.ceil(((t1 - peaks.start) / pspan) * n)));
      if (i0 >= n || i1 <= 0) continue;
      let v = 0;
      for (let i = i0; i < i1; i++) if (peaks.peaks[i] > v) v = peaks.peaks[i];
      // Square root, so quiet talk still shows next to shouting.
      const h = Math.max(1, Math.sqrt(v) * (WAVE_H - 6));
      const t = (t0 + t1) / 2;
      ctx.fillStyle = t < geometry.start || t >= geometry.end ? WAVE_OUT : cutAt(geometry.cuts, t) >= 0 ? WAVE_CUT : WAVE_IN;
      ctx.fillRect(px, mid - h / 2, 1, h);
    }
  }, [peaks, view, width, geometry]);
  return <canvas ref={canvas} className="block h-full w-full" />;
}

const TextLane = memo(function TextLane(props: { blocks: { start: number; end: number; text: string }[]; view: View; width: number; now: number; words: boolean }) {
  const { view, width } = props;
  const pps = width / (view.to - view.from);
  if (props.blocks.length > MAX_BLOCKS) {
    return <p className="px-2 pt-1.5 text-[10px] text-zinc-500">Zoom in to see the transcript here.</p>;
  }
  return (
    <div className="relative h-full overflow-hidden">
      {props.blocks.map((b) => {
        const left = (b.start - view.from) * pps;
        const w = Math.max(1, (b.end - b.start) * pps - 1);
        const current = props.now >= b.start && props.now < b.end;
        return (
          <div
            key={`${b.start}-${b.end}`}
            title={b.text}
            className={cn(
              "absolute top-1 overflow-hidden whitespace-nowrap rounded-sm px-1 leading-[18px] text-zinc-300",
              props.words ? "text-[11px]" : "text-[10px]",
              current ? "bg-zinc-600" : "bg-zinc-800",
            )}
            style={{ left, width: w, height: TEXT_H - 8 }}
          >
            {w > 14 ? b.text : ""}
          </div>
        );
      })}
    </div>
  );
});

/** The whole stream, with where the clip is and the stretch in view. Drag the view's box to move it, or click to
 * go somewhere. */
function Overview(props: { length: number; view: View; geometry: Geometry; now: number; onView: (v: View) => void }) {
  const bar = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x0: number; v0: View } | null>(null);
  const { length, view, geometry: g } = props;
  const pct = (t: number) => `${(clamp(t, 0, length) / length) * 100}%`;
  const timeAt = (clientX: number) => {
    const r = bar.current!.getBoundingClientRect();
    return clamp(((clientX - r.left) / r.width) * length, 0, length);
  };
  return (
    <div
      ref={bar}
      className="relative mt-1.5 h-3.5 cursor-pointer touch-none rounded bg-zinc-900 ring-1 ring-white/5"
      title="The whole stream. Drag the box to move along it."
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        const span = view.to - view.from;
        const t = timeAt(e.clientX);
        const v0 = t >= view.from && t <= view.to ? view : { from: t - span / 2, to: t + span / 2 };
        if (v0 !== view) props.onView(v0);
        drag.current = { x0: e.clientX, v0 };
      }}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d) return;
        const dt = ((e.clientX - d.x0) / bar.current!.getBoundingClientRect().width) * length;
        props.onView({ from: d.v0.from + dt, to: d.v0.to + dt });
      }}
      onPointerUp={() => (drag.current = null)}
    >
      <div className="absolute inset-y-0.5 rounded-sm bg-violet-500/70" style={{ left: pct(g.start), width: `max(2px, calc(${pct(g.end)} - ${pct(g.start)}))` }} />
      <div className="absolute inset-y-0 w-px bg-white/80" style={{ left: pct(props.now) }} />
      <div
        className="absolute inset-y-0 rounded-sm border border-white/60 bg-white/10"
        style={{ left: pct(view.from), width: `max(4px, calc(${pct(view.to)} - ${pct(view.from)}))` }}
      />
    </div>
  );
}
