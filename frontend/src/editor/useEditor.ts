// What the clip editor knows and does, shared by its phone and desktop layouts: the draft (with undo), the
// preview video and its playback, the timeline's view, and saving, deciding on or making the clip.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorText, media, type Clip, type ClipPatch, type Compilation, type Line, type NewClipInfo, type Vod } from "../api";
import { go, goBack, goInstead, loadSetting, saveSetting, usePoll } from "../lib";
import { useToast } from "../ui";
import { createClock } from "./clock";
import {
  clamp, cutAt, FPS, keptLength, MAX_CUTS, MAX_SHORT_S, MIN_CLIP_S, pauseCuts, round2, sameCuts, sameGeometry, shifted,
  streamTime, tidy, withCut, withEnd, withoutCut, withStart, type Geometry, type Range,
} from "./edit";

export type View = { from: number; to: number };

/** An existing clip to edit, or a new one to make from a stream. */
export type Source =
  | { mode: "edit"; clip: Clip; onSaved: (c: Clip) => void }
  | { mode: "new"; vod: Vod; info: NewClipInfo; from?: number; to?: number };

export type Kind = Clip["kind"];

/** What deciding on a clip does: in a stream that makes a compilation, "keep" keeps it for the compilation only and
 * "approve" keeps it and posts it by itself too. */
export type Decision = "approve" | "keep" | "reject";

/** Where a clip made in the editor goes: the inbox, or decided on straight away. */
export type Making = "inbox" | "approve" | "keep";

const ACTIVE = ["approved", "rendering", "rendered", "failed"]; // the server's ACTIVE_CLIP: rendering or rendered to post

export interface Draft extends Geometry {
  title: string; // a clip's full title; for a new one, the part you write, which goes into the show's pattern
  description: string;
  make_short: boolean;
  destination_id: number | null;
  playlist_id: string;
  kind: Kind;
}

const NEW_CLIP_S = 60; // a new clip starts this long, from where it's started

function toDraft(c: Clip): Draft {
  return {
    title: c.title, description: c.description, start: c.start, end: c.end, cuts: c.cuts ?? [], make_short: c.make_short,
    destination_id: c.destination_id, playlist_id: c.playlist_id, kind: c.kind,
  };
}

/** Pads a view around a clip, as the phone's trim strip does. */
export function viewAround(start: number, end: number, length: number): View {
  const pad = Math.max(10, (end - start) * 0.25);
  return { from: Math.max(0, start - pad), to: Math.min(length, end + pad) };
}

export function useEditor(source: Source, desktop: boolean) {
  const toast = useToast();
  const clip = source.mode === "edit" ? source.clip : null;
  const vodId = source.mode === "edit" ? source.clip.vod_id : source.vod.id;
  const length = source.mode === "edit" ? source.clip.vod_duration || source.clip.end + 600 : source.vod.duration || 3600;

  const [draft, setDraft] = useState<Draft>(() => {
    if (source.mode === "edit") return toDraft(source.clip);
    const start = clamp(source.from ?? 0, 0, Math.max(0, length - MIN_CLIP_S));
    const end = clamp(source.to ?? start + NEW_CLIP_S, start + MIN_CLIP_S, length);
    return {
      title: "", description: "", start: round2(start), end: round2(end), cuts: [], make_short: false,
      destination_id: source.info.destination_id, playlist_id: source.info.playlist_id, kind: "highlight",
    };
  });
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const [busy, setBusy] = useState(false);

  // --- Undo and redo, of where the clip starts and ends and what's cut out ---

  const past = useRef<Geometry[]>([]);
  const future = useRef<Geometry[]>([]);
  const liveFrom = useRef<Geometry | null>(null);
  const [, setHistoryVersion] = useState(0);
  const geometry = (): Geometry => ({ start: draftRef.current.start, end: draftRef.current.end, cuts: draftRef.current.cuts });
  const apply = (g: Geometry) => {
    draftRef.current = { ...draftRef.current, ...g };
    setDraft((d) => ({ ...d, ...g, make_short: d.make_short && keptLength(g) <= MAX_SHORT_S }));
  };
  const remember = (before: Geometry) => {
    past.current = [...past.current.slice(-199), before];
    future.current = [];
    setHistoryVersion((v) => v + 1);
  };
  /** An edit that can be undone. */
  const commit = useCallback((next: Geometry) => {
    const before = geometry();
    if (sameGeometry(before, next)) return;
    remember(before);
    apply(next);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- reads the latest draft through its ref
  /** A drag: the edits as it goes aren't undone one by one, only the whole of it. */
  const live = {
    begin: () => (liveFrom.current = geometry()),
    move: (next: Geometry) => apply(next),
    end: () => {
      if (liveFrom.current && !sameGeometry(liveFrom.current, geometry())) remember(liveFrom.current);
      liveFrom.current = null;
    },
  };
  const undo = () => {
    const before = past.current.pop();
    if (!before) return;
    future.current.push(geometry());
    apply(before);
    setHistoryVersion((v) => v + 1);
  };
  const redo = () => {
    const after = future.current.pop();
    if (!after) return;
    past.current.push(geometry());
    apply(after);
    setHistoryVersion((v) => v + 1);
  };

  // --- What's changed since it was saved ---

  const saved = clip ? toDraft(clip) : null;
  const changes: ClipPatch = {};
  if (saved) {
    for (const key of ["title", "description", "start", "end", "make_short", "destination_id", "playlist_id"] as const) {
      if (draft[key] !== saved[key]) Object.assign(changes, { [key]: draft[key] });
    }
    if (!sameCuts(draft.cuts, saved.cuts)) changes.cuts = draft.cuts;
  }
  const dirty = Object.keys(changes).length > 0;
  const needsRender = ["start", "end", "cuts", "make_short"].some((k) => k in changes);
  const duration = keptLength(draft);
  const editable = draft.kind !== "whole";
  // A chapter or highlight of a stream that makes a compilation: Approve is Keep there.
  const compiling = source.mode === "edit" ? source.clip.in_compilation : source.vod.compilation !== null;
  const compilationPiece = source.mode === "edit" ? source.clip.compilation_piece : source.vod.mode === "compilation";
  const fullTitle =
    source.mode === "new"
      ? source.info.title_pattern.replaceAll("{title}", draft.title).split(/\s+/).filter(Boolean).join(" ")
      : draft.title;

  // --- The preview video ---

  const video = useRef<HTMLVideoElement>(null);
  // The playhead's time: a clock that what shows it subscribes to (clock.ts), not state, so playing doesn't redraw
  // the whole editor every frame.
  const [clock] = useState(() => createClock(draft.start));
  const [playing, setPlaying] = useState(false);
  const [videoError, setVideoError] = useState(false);
  const [rate, setRateState] = useState(1);
  const [loop, setLoop] = useState(false);
  const stopAt = useRef<number | null>(null);
  const loopRef = useRef(loop);
  loopRef.current = loop;

  // Preview volume: only how loud it plays here, remembered on this device. The clip's audio is untouched.
  const [volume, setVolume] = useState(() => loadSetting("clipper.previewVolume", 1));
  const [muted, setMuted] = useState(() => loadSetting("clipper.previewMuted", false));
  const [volumeAdjustable, setVolumeAdjustable] = useState(true);
  useEffect(() => {
    const v = video.current;
    if (v) {
      v.volume = volume;
      v.muted = muted;
    }
    saveSetting("clipper.previewVolume", volume);
    saveSetting("clipper.previewMuted", muted);
  }, [volume, muted]);

  // What the frame loop needs of the timeline (set further down), without starting it again each time it changes.
  const follow = useRef<{ desktop: boolean; view: View; setView: (v: View) => void } | null>(null);

  // While playing, follow the playhead every frame: jump over cut parts, loop at the clip's end if looping, stop at
  // the end of a preview, and on a desktop, page the timeline along when the playhead runs off it.
  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    let prev = video.current?.currentTime ?? 0;
    const tick = () => {
      const v = video.current;
      if (v) {
        const g = draftRef.current;
        let t = v.currentTime;
        const inCut = cutAt(g.cuts, t);
        if (inCut >= 0) v.currentTime = t = g.cuts[inCut][1];
        if (loopRef.current && prev < g.end && t >= g.end) v.currentTime = t = g.start;
        else if (stopAt.current !== null && t >= stopAt.current) {
          v.pause();
          stopAt.current = null;
        }
        prev = t;
        clock.set(t);
        const f = follow.current;
        if (f?.desktop && (t > f.view.to || t < f.view.from)) {
          const span = f.view.to - f.view.from;
          f.view = { from: t - span * 0.1, to: t + span * 0.9 };
          f.setView(f.view);
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing, clock]);

  // Dragging asks for a new spot many times a second. While the video is still getting to the last one, only the
  // newest is kept and gone to once it's there: each seek can fetch from the server, and on a slow connection
  // they'd pile up.
  const pendingSeek = useRef<number | null>(null);
  const wanted = useRef(draft.start); // the last spot asked for
  const seek = useCallback((t: number) => {
    const v = video.current;
    const to = clamp(t, 0, length);
    wanted.current = to;
    clock.set(to);
    // On a desktop, a jump off the timeline (a key, a line) pages it along, as playing does.
    const f = follow.current;
    if (f?.desktop && (to > f.view.to || to < f.view.from)) {
      const span = f.view.to - f.view.from;
      f.view = { from: to - span * 0.1, to: to + span * 0.9 };
      f.setView(f.view);
    }
    if (!v) return;
    if (v.seeking) pendingSeek.current = to;
    else v.currentTime = to;
  }, [length, clock]);
  const play = () => void video.current?.play();
  const pause = () => video.current?.pause();
  const playRange = (from: number, to: number) => {
    seek(from);
    stopAt.current = to;
    play();
  };
  /** The clip as it comes out: from its start to its end, jumping over the cuts. */
  const playClip = () => playRange(draftRef.current.start, draftRef.current.end);
  /** The first or last `seconds` of the clip as it comes out. */
  const playFirst = (seconds: number) => playRange(draftRef.current.start, streamTime(draftRef.current, seconds));
  const playLast = (seconds: number) => {
    const g = draftRef.current;
    playRange(streamTime(g, keptLength(g) - seconds), g.end);
  };
  const togglePlay = () => {
    const v = video.current;
    if (!v) return;
    if (v.paused) {
      stopAt.current = null;
      play();
    } else pause();
  };
  /**
   * Where the playhead is, or is on its way to: steps pressed while the video is still seeking count from there, and
   * before it has loaded (or while its preview is still being made), from where they've taken the playhead so far.
   */
  const position = () => {
    const v = video.current;
    return v && v.readyState > 0 && !v.seeking && pendingSeek.current === null ? v.currentTime : wanted.current;
  };
  // Jump back or ahead while watching. It keeps playing, even past the end of a preview.
  const skip = (seconds: number) => {
    if (!video.current) return;
    stopAt.current = null;
    seek(position() + seconds);
  };
  /** Frame by frame, paused. Counted in whole frames, so stepping doesn't drift. */
  const step = (frames: number) => {
    pause();
    seek((Math.round(position() * FPS) + frames) / FPS);
  };
  const setRate = (r: number) => {
    setRateState(r);
    if (video.current) video.current.playbackRate = r;
  };
  /** Play, or if playing already, faster each time. */
  const faster = () => {
    const v = video.current;
    if (!v) return;
    if (v.paused) {
      stopAt.current = null;
      setRate(1);
      play();
    } else setRate(({ 1: 1.5, 1.5: 2, 2: 3 } as Record<number, number>)[v.playbackRate] ?? 3);
  };

  const videoProps = {
    ref: video,
    src: media.proxy(vodId),
    playsInline: true,
    preload: "auto",
    onLoadedMetadata: (e: React.SyntheticEvent<HTMLVideoElement>) => {
      // iPhones ignore volume set from a page (only the hardware buttons work); offer mute only.
      const v = e.currentTarget;
      v.volume = 0.5;
      setVolumeAdjustable(v.volume === 0.5);
      v.volume = volume;
      v.muted = muted;
      v.playbackRate = rate;
      seek(wanted.current); // the clip's start, unless the playhead was moved while it loaded
    },
    onPlay: () => setPlaying(true),
    onPause: () => setPlaying(false),
    onSeeked: () => {
      const v = video.current;
      if (!v) return;
      const next = pendingSeek.current;
      pendingSeek.current = null;
      if (next !== null && Math.abs(next - v.currentTime) > 0.001) v.currentTime = next;
      else clock.set(v.currentTime);
    },
    onError: () => setVideoError(true),
  };

  // --- Editing the clip's ends and cuts ---

  // Changing a cut point plays the few seconds around it, so you hear exactly where it lands. `push` moves the
  // whole clip along when the new point is past its other end (setting it from the transcript or the playhead).
  const setStart = (t: number, preview = true, push = false) => {
    const next = withStart(geometry(), t, length, push);
    commit(next);
    if (preview) playRange(next.start, Math.min(next.start + 3, next.end));
    else seek(next.start);
  };
  const setEnd = (t: number, preview = true, push = false) => {
    const next = withEnd(geometry(), t, length, push);
    commit(next);
    if (preview) playRange(Math.max(next.start, next.end - 3), next.end);
    else seek(next.end);
  };
  const cutOut = (range: Range) => commit(withCut(geometry(), range));
  const restoreCut = (i: number) => commit(withoutCut(geometry(), i));
  const trimTo = ([a, b]: Range) => commit(tidy({ start: round2(a), end: round2(b), cuts: geometry().cuts }));
  const moveBy = (dt: number) => commit(shifted(geometry(), dt, length));
  /** Hear a join: a couple of seconds either side of a cut, jumping over it. */
  const previewJoin = (i: number) => {
    const [a, b] = draftRef.current.cuts[i] ?? [];
    if (a !== undefined) playRange(Math.max(draftRef.current.start, a - 2), Math.min(draftRef.current.end, b + 2));
  };

  // --- The desktop timeline's view: which stretch of the stream it shows ---

  const MIN_SPAN = 2;
  const [view, setViewState] = useState<View>(() => viewAround(draft.start, draft.end, length));
  const setView = useCallback((v: View) => {
    const span = clamp(v.to - v.from, MIN_SPAN, length);
    const from = clamp(v.from, 0, Math.max(0, length - span));
    setViewState({ from, to: from + span });
  }, [length]);
  const zoom = (factor: number, anchor = clock.get()) => {
    setViewState((v) => {
      const span = clamp((v.to - v.from) * factor, MIN_SPAN, length);
      const at = clamp(anchor, v.from, v.to);
      const ratio = (at - v.from) / (v.to - v.from || 1);
      const from = clamp(at - ratio * span, 0, Math.max(0, length - span));
      return { from, to: from + span };
    });
  };
  const fitClip = () => setView(viewAround(draftRef.current.start, draftRef.current.end, length));
  const fitAll = () => setView({ from: 0, to: length });
  follow.current = { desktop, view, setView };

  // --- The transcript, and the channels to upload to ---

  const [lines, setLines] = useState<Line[]>([]);
  useEffect(() => {
    // A phone editing a clip has the minute either side of it, as it always has; everything else, the whole stream.
    const load = !desktop && clip ? api.transcript(clip.id) : api.vodTranscript(vodId);
    load.then(setLines, () => setLines([]));
  }, [desktop, clip?.id, vodId]); // eslint-disable-line react-hooks/exhaustive-deps -- the clip's id, not each poll of it
  const { data: destinations } = usePoll(api.destinations, 0);

  // --- The stream's compilation: for a compilation's strip, and where a kept clip comes in it ---

  const wantsCompilation = !!clip && (clip.kind === "compilation" || clip.status === "kept");
  const { data: compilation, refresh: refreshCompilation } = usePoll<Compilation | null>(
    () => (wantsCompilation ? api.vod(vodId).then((d) => d.vod.compilation) : Promise.resolve(null)),
    wantsCompilation ? 5000 : 0,
    `${clip?.id} ${wantsCompilation}`,
  );
  // A compiling stream's compilation says what it's made of in its words (the server's says "approved"), as its card does.
  const why = clip?.kind === "compilation" && compilation ? `The ${clip.compilation_clips} kept clips of this stream, in order.` : clip?.why ?? "";
  /** For a kept clip once the compilation is made: how far into the compilation it starts. */
  let compilationFrom: number | null = null;
  if (clip?.status === "kept" && compilation?.clip_id && compilation.kept.some(([a, b]) => a < clip.end && b > clip.start)) {
    compilationFrom = compilation.kept.reduce((t, [a, b]) => t + Math.max(0, Math.min(b, clip.start) - a), 0);
  }

  /** To the start of the next line of the transcript, or back to the start of this one (once a moment into it) or the
   * one before. Playing carries on. */
  const toLine = (direction: 1 | -1) => {
    const t = position();
    let next: Line | undefined;
    if (direction > 0) next = lines.find((l) => l.start > t + 0.05);
    else for (const l of lines) if (l.start < t - 0.3) next = l;
    if (!next) return;
    stopAt.current = null;
    seek(next.start);
  };

  /** Cut out the pauses of at least `minPause` seconds between the clip's words, as one edit (undone together). */
  const tighten = (minPause: number) =>
    run(async () => {
      const asked = geometry();
      const said = await api.vodTranscript(vodId, { start: asked.start, end: asked.end, words: true });
      const words = said.flatMap((l) => (l.words ?? []).map(([a, b]): Range => [a, b]));
      if (!words.length) {
        toast({ text: "There are no words in this part of the transcript to go by." });
        return;
      }
      const g = geometry(); // as it is now, in case it changed while the words loaded
      const pauses = pauseCuts(words, g, minPause, MAX_CUTS - g.cuts.length).filter(
        ([a, b]) => !g.cuts.some(([c, d]) => c <= a && d >= b), // not already cut out
      );
      const next = tidy({ ...g, cuts: [...g.cuts, ...pauses] });
      const shorter = keptLength(g) - keptLength(next);
      if (!pauses.length || shorter < 0.05) {
        toast({ text: `No pauses of ${minPause} s or more left to cut out.` });
        return;
      }
      commit(next);
      toast({
        text: `Cut out ${pauses.length} pause${pauses.length === 1 ? "" : "s"}: ${shorter.toFixed(1)} s shorter. Undo puts them back.`,
      });
    });

  // --- Saving, deciding, making ---

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    try {
      await work();
    } catch (e) {
      toast({ text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const nextClipAfter = async (id: number): Promise<string | null> => {
    const inbox = await api.clips(["proposed"]);
    const i = inbox.findIndex((c) => c.id === id);
    const next = inbox[i + 1] ?? inbox.find((c) => c.id !== id);
    return next ? `clip/${next.id}` : null;
  };

  const save = () =>
    run(async () => {
      if (source.mode !== "edit" || !dirty) return;
      const c = await api.updateClip(source.clip.id, changes);
      source.onSaved(c);
      // The server tidies the cuts the same way; take its numbers so nothing looks unsaved.
      apply({ start: c.start, end: c.end, cuts: c.cuts });
      const renders = !["proposed", "extra", "kept"].includes(source.clip.status) && needsRender;
      toast({ text: renders ? "Saved: it will render again" : "Saved" });
    });

  const decide = (decision: Decision) =>
    run(async () => {
      if (source.mode !== "edit") return;
      const c = source.clip;
      // Extras are opened from their video, so that's where to go back to.
      const next = c.status === "proposed" && !c.compilation_piece ? await nextClipAfter(c.id) : null;
      if (dirty) await api.updateClip(c.id, changes);
      const decidedClip = await (decision === "keep" ? api.keep(c.id) : decision === "approve" ? api.approve(c.id) : api.reject(c.id));
      // Undo opens the clip again, in place of the next one if that's still showing.
      const reopen = () => (location.hash.startsWith("#/clip/") ? goInstead : go)(`clip/${c.id}`);
      const text = {
        keep: "Kept for the compilation",
        approve: c.in_compilation ? "Kept. It will also render to post by itself." : "Approved: it will render shortly",
        reject: "Rejected",
      }[decision];
      toast({ text, action: { label: "Undo", run: () => void api.restore(c.id, c.compilation_piece ? decidedClip.updated_at : undefined).then(reopen, (e) => toast({ text: errorText(e) })) } });
      // The next clip takes this one's place, so Back goes straight to where you started; after the last, that's
      // where this goes.
      if (next) goInstead(next);
      else goBack(c.status === "proposed" ? "inbox" : "videos");
    });

  const restore = () =>
    run(async () => {
      if (source.mode === "edit") source.onSaved(await api.restore(source.clip.id));
    });

  // Approved and kept clips can still be thrown away, render and all. Undo puts it back as it was: a kept clip is kept
  // again, not approved, which would render it.
  const discard = () =>
    run(async () => {
      if (source.mode !== "edit") return;
      const { id, status } = source.clip;
      const kept = status === "kept";
      await api.reject(id);
      toast({ text: "Deleted", action: { label: "Undo", run: () => void (kept ? api.keep(id) : api.approve(id)) } });
      goBack(kept ? `videos/${vodId}` : "approved"); // Upload never lists a kept clip
    });

  /** A kept clip: post it by itself too, as well as in the compilation. */
  const postToo = () =>
    run(async () => {
      if (source.mode !== "edit") return;
      const id = source.clip.id;
      if (dirty) await api.updateClip(id, changes);
      source.onSaved(await api.approve(id));
      toast({
        text: "It will render to post by itself",
        action: { label: "Undo", run: () => void api.keep(id).then(source.onSaved) },
      });
    });

  /** A clip of a compiling stream that's posted by itself too: keep it for the compilation only. */
  const compilationOnly = () =>
    run(async () => {
      if (source.mode !== "edit") return;
      const id = source.clip.id;
      if (dirty) await api.updateClip(id, changes);
      source.onSaved(await api.keep(id));
      toast({
        text: "It stays in the compilation and won't be posted by itself",
        action: { label: "Undo", run: () => void api.approve(id).then(source.onSaved) },
      });
    });

  /** A compilation: make it again from the clips kept now. What's in the draft is saved first, so its cuts stay out. */
  const rebuild = () =>
    run(async () => {
      if (source.mode !== "edit") return;
      if (!confirm("Rebuild with the clips kept now? Its description is written again, and the parts you cut out stay out.")) return;
      const id = source.clip.id;
      if (dirty) await api.updateClip(id, changes);
      const c = await api.rebuild(id);
      source.onSaved(c);
      // Its ends, cuts and description are new, so the draft starts again from them.
      past.current = [];
      future.current = [];
      draftRef.current = toDraft(c);
      setDraft(toDraft(c));
      setHistoryVersion((v) => v + 1);
      void refreshCompilation();
      toast({ text: "Rebuilt" });
    });

  /** Make the new clip: into the inbox, or decided on straight away (approved to render, or kept). */
  const create = (making: Making) =>
    run(async () => {
      if (source.mode !== "new") return;
      const d = draftRef.current;
      const made = await api.makeClip(source.vod.id, {
        kind: d.kind === "highlight" ? "highlight" : "chapter", start: d.start, end: d.end, cuts: d.cuts, title: fullTitle,
        description: d.description, make_short: d.kind === "highlight" && d.make_short,
        destination_id: d.destination_id, playlist_id: d.playlist_id, approve: making === "approve", keep: making === "keep",
      });
      const text = {
        inbox: "Made: it's in your inbox",
        keep: "Made and kept for the compilation",
        approve: compiling ? "Made and kept: it will also render to post by itself" : "Made and approved: it will render shortly",
      }[making];
      toast({ text, action: { label: "Open", run: () => go(`clip/${made.id}`) } });
      goBack(`videos/${source.vod.id}`);
    });

  return {
    source, clip, vodId, length, draft, setDraft, busy, dirty, needsRender, changes, duration, editable, fullTitle,
    compiling, compilationPiece, compilation, compilationFrom, why, active: !!clip && ACTIVE.includes(clip.status),
    // history
    commit, live, undo, redo, canUndo: past.current.length > 0, canRedo: future.current.length > 0,
    // playback
    video, videoProps, clock, playing, videoError, rate, setRate, loop, setLoop, volume, setVolume, muted, setMuted,
    volumeAdjustable, seek, play, pause, playRange, playClip, playFirst, playLast, togglePlay, skip, step, faster, toLine,
    // edits
    setStart, setEnd, cutOut, restoreCut, trimTo, moveBy, previewJoin, tighten,
    // timeline view
    view, setView, zoom, fitClip, fitAll,
    // data
    lines, destinations,
    // actions
    save, decide, restore, discard, create, postToo, compilationOnly, rebuild,
  };
}

export type Editor = ReturnType<typeof useEditor>;
