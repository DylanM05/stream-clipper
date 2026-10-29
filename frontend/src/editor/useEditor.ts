// What the clip editor knows and does, shared by its phone and desktop layouts: the draft (with undo), the
// preview video and its playback, the timeline's view, and saving, deciding on or making the clip.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorText, media, type Clip, type ClipPatch, type Line, type NewClipInfo, type Vod } from "../api";
import { go, goBack, loadSetting, saveSetting, usePoll } from "../lib";
import { useToast } from "../ui";
import {
  clamp, cutAt, FPS, keptLength, MAX_SHORT_S, MIN_CLIP_S, round2, sameCuts, sameGeometry, shifted, tidy, withCut,
  withEnd, withoutCut, withStart, type Geometry, type Range,
} from "./edit";

export type View = { from: number; to: number };

/** An existing clip to edit, or a new one to make from a stream. */
export type Source =
  | { mode: "edit"; clip: Clip; onSaved: (c: Clip) => void }
  | { mode: "new"; vod: Vod; info: NewClipInfo; from?: number; to?: number };

export type Kind = Clip["kind"];

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
  const fullTitle =
    source.mode === "new"
      ? source.info.title_pattern.replaceAll("{title}", draft.title).split(/\s+/).filter(Boolean).join(" ")
      : draft.title;

  // --- The preview video ---

  const video = useRef<HTMLVideoElement>(null);
  const [now, setNow] = useState(draft.start);
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

  // While playing, follow the playhead every frame: jump over cut parts, loop at the clip's end if looping, and
  // stop at the end of a preview.
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
        setNow(t);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing]);

  // Dragging asks for a new spot many times a second. While the video is still getting to the last one, only the
  // newest is kept and gone to once it's there: each seek can fetch from the server, and on a slow connection
  // they'd pile up.
  const pendingSeek = useRef<number | null>(null);
  const wanted = useRef(0); // the last spot asked for
  const seek = useCallback((t: number) => {
    const v = video.current;
    const to = clamp(t, 0, length);
    wanted.current = to;
    setNow(to);
    if (!v) return;
    if (v.seeking) pendingSeek.current = to;
    else v.currentTime = to;
  }, [length]);
  const play = () => void video.current?.play();
  const pause = () => video.current?.pause();
  const playRange = (from: number, to: number) => {
    seek(from);
    stopAt.current = to;
    play();
  };
  /** The clip as it comes out: from its start to its end, jumping over the cuts. */
  const playClip = () => playRange(draftRef.current.start, draftRef.current.end);
  const togglePlay = () => {
    const v = video.current;
    if (!v) return;
    if (v.paused) {
      stopAt.current = null;
      play();
    } else pause();
  };
  /** Where the playhead is, or is on its way to: steps pressed while the video is still seeking count from there. */
  const position = () => {
    const v = video.current;
    return v && !v.seeking && pendingSeek.current === null ? v.currentTime : wanted.current;
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
      seek(draftRef.current.start);
    },
    onPlay: () => setPlaying(true),
    onPause: () => setPlaying(false),
    onSeeked: () => {
      const v = video.current;
      if (!v) return;
      const next = pendingSeek.current;
      pendingSeek.current = null;
      if (next !== null && Math.abs(next - v.currentTime) > 0.001) v.currentTime = next;
      else setNow(v.currentTime);
    },
    onError: () => setVideoError(true),
  };

  // --- Editing the clip's ends and cuts ---

  const nowRef = useRef(now);
  nowRef.current = now;
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
  const zoom = (factor: number, anchor = nowRef.current) => {
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
  // Page along with the playhead while playing, as editing apps do.
  useEffect(() => {
    if (!desktop || !playing) return;
    if (now > view.to || now < view.from) {
      const span = view.to - view.from;
      setView({ from: now - span * 0.1, to: now + span * 0.9 });
    }
  }, [desktop, playing, now, view, setView]);

  // --- The transcript, and the channels to upload to ---

  const [lines, setLines] = useState<Line[]>([]);
  useEffect(() => {
    // A phone editing a clip has the minute either side of it, as it always has; everything else, the whole stream.
    const load = !desktop && clip ? api.transcript(clip.id) : api.vodTranscript(vodId);
    load.then(setLines, () => setLines([]));
  }, [desktop, clip?.id, vodId]); // eslint-disable-line react-hooks/exhaustive-deps -- the clip's id, not each poll of it
  const { data: destinations } = usePoll(api.destinations, 0);

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

  const nextClipAfter = async (id: number): Promise<string> => {
    const inbox = await api.clips(["proposed"]);
    const i = inbox.findIndex((c) => c.id === id);
    const next = inbox[i + 1] ?? inbox.find((c) => c.id !== id);
    return next ? `clip/${next.id}` : "inbox";
  };

  const save = () =>
    run(async () => {
      if (source.mode !== "edit" || !dirty) return;
      const c = await api.updateClip(source.clip.id, changes);
      source.onSaved(c);
      // The server tidies the cuts the same way; take its numbers so nothing looks unsaved.
      apply({ start: c.start, end: c.end, cuts: c.cuts });
      toast({ text: source.clip.status !== "proposed" && needsRender ? "Saved: it will render again" : "Saved" });
    });

  const decide = (approve: boolean) =>
    run(async () => {
      if (source.mode !== "edit") return;
      const c = source.clip;
      // Extras are opened from their video, so that's where to go back to.
      const next = c.status === "proposed" ? await nextClipAfter(c.id) : null;
      if (dirty) await api.updateClip(c.id, changes);
      await (approve ? api.approve(c.id) : api.reject(c.id));
      toast({
        text: approve ? "Approved: it will render shortly" : "Rejected",
        action: { label: "Undo", run: () => void api.restore(c.id).then(() => go(`clip/${c.id}`)) },
      });
      if (next) go(next);
      else goBack("videos");
    });

  const restore = () =>
    run(async () => {
      if (source.mode === "edit") source.onSaved(await api.restore(source.clip.id));
    });

  // Approved clips can still be thrown away, render and all.
  const discard = () =>
    run(async () => {
      if (source.mode !== "edit") return;
      const id = source.clip.id;
      await api.reject(id);
      toast({ text: "Deleted", action: { label: "Undo", run: () => void api.approve(id) } });
      goBack("approved");
    });

  /** Make the new clip: into the inbox, or with `approve` straight to rendering. */
  const create = (approve: boolean) =>
    run(async () => {
      if (source.mode !== "new") return;
      const d = draftRef.current;
      const made = await api.makeClip(source.vod.id, {
        kind: d.kind === "whole" ? "chapter" : d.kind, start: d.start, end: d.end, cuts: d.cuts, title: fullTitle,
        description: d.description, make_short: d.kind === "highlight" && d.make_short,
        destination_id: d.destination_id, playlist_id: d.playlist_id, approve,
      });
      toast({
        text: approve ? "Made and approved: it will render shortly" : "Made: it's in your inbox",
        action: { label: "Open", run: () => go(`clip/${made.id}`) },
      });
      goBack(`videos/${source.vod.id}`);
    });

  return {
    source, clip, vodId, length, draft, setDraft, busy, dirty, needsRender, changes, duration, editable, fullTitle,
    // history
    commit, live, undo, redo, canUndo: past.current.length > 0, canRedo: future.current.length > 0,
    // playback
    video, videoProps, now, playing, videoError, rate, setRate, loop, setLoop, volume, setVolume, muted, setMuted,
    volumeAdjustable, seek, play, pause, playRange, playClip, togglePlay, skip, step, faster,
    // edits
    setStart, setEnd, cutOut, restoreCut, trimTo, moveBy, previewJoin,
    // timeline view
    view, setView, zoom, fitClip, fitAll,
    // data
    lines, destinations,
    // actions
    save, decide, restore, discard, create,
  };
}

export type Editor = ReturnType<typeof useEditor>;
