import { useEffect, useRef, useState } from "react";
import { api, errorText, media, type Clip } from "../api";
import { createClock, useClock, type Clock } from "../editor/clock";
import { clamp, clipTime, kept, keptLength, streamTime, timecode } from "../editor/edit";
import { cn, go, goBack, loadSetting, saveSetting, useDesktop, usePoll } from "../lib";
import { Button, ErrorNote, Screen, useToast } from "../ui";

const UNDO_MS = 10_000;
const RATES = [0.5, 1, 1.5, 2];
type ReviewUndo = { id: number; version: string; expires: number };
type EditorReturn = { wasProposed: boolean; undo?: ReviewUndo };

const STATUS = { proposed: "To review", kept: "Kept", rejected: "Rejected" } as const;

export function CompilationReview({ id, returnPiece, returnTime }: { id: number; returnPiece: number; returnTime: number }) {
  const desktop = useDesktop();
  const { data, error, refresh } = usePoll(() => api.vod(id), desktop ? 5000 : 0, id);
  const [decisions, setDecisions] = useState<Record<number, Clip>>({});
  const pieces = data?.clips.filter((c) => c.compilation_piece).map((clip) => {
    const decision = decisions[clip.id];
    return decision && decision.updated_at > clip.updated_at ? decision : clip;
  }).sort((a, b) => a.start - b.start || a.id - b.id) ?? [];
  const back = () => goBack(`videos/${id}`);

  return (
    <Screen title="Review compilation pieces" subtitle={data?.vod.title} onBack={back} wide>
      {error && <ErrorNote>{error}</ErrorNote>}
      {!desktop ? (
        <Button onClick={() => go("inbox")}>Review in the inbox</Button>
      ) : data?.vod.mode === "compilation" && !data.vod.cleaned ? (
        <Player
          key={id}
          id={id}
          pieces={pieces}
          onChange={(clip) => setDecisions((current) => ({ ...current, [clip.id]: clip }))}
          onError={() => void refresh()}
          returnPiece={returnPiece}
          returnTime={returnTime}
          compilationId={data.vod.compilation?.clip_id}
        />
      ) : data && <p className="text-sm text-graphite">There are no compilation pieces to play for this video.</p>}
    </Screen>
  );
}

function Player({ id, pieces, onChange, onError, returnPiece, returnTime, compilationId }: {
  id: number;
  pieces: Clip[];
  onChange: (clip: Clip) => void;
  onError: () => void;
  returnPiece: number;
  returnTime: number;
  compilationId?: number | null;
}) {
  const toast = useToast();
  const video = useRef<HTMLVideoElement>(null);
  const [resume] = useState<EditorReturn | undefined>(() => returnPiece ? history.state?.compilationReview : undefined);
  const returningAt = pieces.findIndex((clip) => clip.id === returnPiece);
  const decidedInEditor = resume?.wasProposed && returningAt >= 0 && pieces[returningAt].status !== "proposed";
  const [selected, setSelected] = useState<number | null>(() => {
    if (!decidedInEditor) return returnPiece || null;
    return pieces.slice(returningAt + 1).find((clip) => clip.status === "proposed")?.id ?? null;
  });
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [muted, setMuted] = useState(() => loadSetting("clipper.previewMuted", false));
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [mediaError, setMediaError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [undo, setUndo] = useState<ReviewUndo | undefined>(() => (
    resume?.undo && resume.undo.expires > Date.now() ? resume.undo : undefined
  ));
  const [clock] = useState(() => createClock(decidedInEditor ? 0 : returnTime || 0));
  const pendingSeek = useRef<number | null>(null);
  const strip = useRef<HTMLDivElement>(null);

  const playable = pieces.filter((c) => c.status === "proposed" || c.status === "kept");
  const firstPending = playable.find((c) => c.status === "proposed");
  const current = playable.find((c) => c.id === selected) ?? firstPending;
  const parts = current ? kept(current) : [];
  const position = current ? pieces.findIndex((c) => c.id === current.id) : -1;
  const sequencePosition = current ? playable.findIndex((c) => c.id === current.id) : -1;
  const canDecide = !busy && current?.status === "proposed";
  const duration = current ? keptLength(current) : 0;
  const controlsDisabled = busy || mediaError || !duration;

  useEffect(() => {
    if (!undo) return;
    const timeout = window.setTimeout(() => setUndo(undefined), Math.max(0, undo.expires - Date.now()));
    return () => clearTimeout(timeout);
  }, [undo]);

  useEffect(() => {
    strip.current?.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [current?.id]);

  const seek = (time: number) => {
    clock.set(time);
    const v = video.current;
    if (!v || !v.readyState) return;
    if (v.seeking) pendingSeek.current = time;
    else if (v.currentTime !== time) v.currentTime = time;
  };

  const seekPiece = (time: number) => {
    if (!current) return;
    const t = streamTime(current, clamp(time, 0, duration));
    const part = parts.find(([, end]) => end > t);
    // At a cut, show the first retained frame after the join. Scrubbing to the end holds there.
    if (!part) video.current?.pause();
    seek(part ? Math.max(t, part[0]) : current.end);
  };

  const skip = (seconds: number) => {
    if (current) seekPiece(clipTime(current, clock.get()) + seconds);
  };

  const move = (clip: Clip) => {
    setSelected(clip.id);
    seek(kept(clip)[0][0]);
  };

  const moveBy = (direction: number) => {
    const next = playable[sequencePosition < 0 ? 0 : sequencePosition + direction];
    if (next) move(next);
  };

  const toggle = () => {
    const v = video.current;
    if (!v || !parts.length || controlsDisabled) return;
    if (!v.paused) {
      v.pause();
      return;
    }
    const at = clock.get();
    if (!parts.some(([a, b]) => a <= at && at < b)) seek(parts[0][0]);
    void v.play().catch(() => {});
  };

  const playback = useRef<() => void>(() => {});
  playback.current = () => {
    const v = video.current;
    if (!v || v.paused || v.seeking || !current || inFlight.current) return;
    const nextPart = parts.find(([, end]) => end > v.currentTime);
    if (nextPart) {
      if (v.currentTime < nextPart[0]) seek(nextPart[0]);
      else clock.set(v.currentTime);
    } else {
      const next = playable[sequencePosition + 1];
      if (next) move(next);
      else {
        v.pause();
        seek(current.end);
      }
    }
  };
  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    const tick = () => {
      playback.current();
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing]);

  const change = async (operation: () => Promise<Clip>, after: (clip: Clip) => void) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const clip = await operation();
      onChange(clip);
      after(clip);
    } catch (e) {
      toast({ text: errorText(e) });
      onError();
    } finally {
      setBusy(false);
      inFlight.current = false;
    }
  };

  const decide = (decision: "keep" | "reject") => {
    if (!canDecide || !current) return;
    const next = pieces.slice(position + 1).find((c) => c.status === "proposed")
      ?? playable.find((c) => c.id !== current.id && c.status === "proposed");
    const expires = Date.now() + UNDO_MS;
    const resume = playing;
    video.current?.pause();
    void change(() => api[decision](current.id, current.updated_at), (clip) => {
      setUndo({ id: clip.id, version: clip.updated_at, expires });
      if (next) {
        move(next);
        if (resume) void video.current?.play().catch(() => {});
      } else setSelected(null);
    });
  };

  const undoDecision = () => {
    if (!undo || Date.now() >= undo.expires || inFlight.current) return;
    setUndo(undefined);
    void change(() => api.restore(undo.id, undo.version), move);
  };

  const commands = useRef<Record<string, () => void>>({});
  commands.current = {
    a: () => decide("keep"),
    r: () => decide("reject"),
    arrowleft: () => moveBy(-1),
    arrowright: () => moveBy(1),
    " ": toggle,
    z: undoDecision,
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target;
      if (event.metaKey || event.ctrlKey || event.altKey || event.repeat ||
          (target instanceof HTMLElement && target.closest("input, textarea, select, [contenteditable]"))) return;
      const key = event.key.toLowerCase();
      if (key === " " && target instanceof HTMLElement && target.closest("button, a")) return;
      const command = inFlight.current ? undefined : commands.current[key];
      if (!command) return;
      event.preventDefault();
      command();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const edit = () => {
    if (!current) return;
    const time = clock.get();
    history.replaceState(
      { ...history.state, compilationReview: { wasProposed: current.status === "proposed", undo } },
      "",
      `#/review/${id}/${current.id}/${time.toFixed(3)}`,
    );
    go(`clip/${current.id}`);
  };

  if (!pieces.length) return <p className="text-sm text-graphite">No pieces to review yet.</p>;

  return (
    <div className="mx-auto max-w-4xl space-y-3">
      {current ? (
        <div className="overflow-hidden rounded-xl bg-panel">
          <div aria-live="polite" className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-3">
            <span className="text-xs text-graphite">Piece {position + 1} of {pieces.length}</span>
            <span className="text-sm font-semibold">{current.title}</span>
          </div>
          <div className="relative aspect-video bg-black">
          <video
            key={attempt}
            ref={video}
            src={media.proxy(id)}
            className="size-full cursor-pointer"
            preload="metadata"
            playsInline
            aria-label="Stream preview"
            onClick={toggle}
            onLoadedMetadata={(e) => {
              const v = e.currentTarget;
              v.volume = loadSetting("clipper.previewVolume", 1);
              v.playbackRate = rate;
              const at = clock.get();
              const resume = parts.find(([, b]) => at < b) ?? parts[0];
              if (resume) seek(Math.max(resume[0], at <= current.end ? at : resume[0]));
            }}
            muted={muted}
            onSeeked={(e) => {
              const v = e.currentTarget;
              const next = pendingSeek.current;
              pendingSeek.current = null;
              if (next !== null && Math.abs(next - v.currentTime) > 0.001) seek(next);
              else clock.set(v.currentTime);
            }}
            onPlay={() => setPlaying(true)}
            onPause={(e) => {
              setPlaying(false);
              if (!e.currentTarget.seeking) clock.set(e.currentTarget.currentTime);
            }}
            onEnded={() => setPlaying(false)}
            onError={() => {
              pendingSeek.current = null;
              setMediaError(true);
              setPlaying(false);
            }}
          />
          {mediaError && (
            <div className="absolute inset-0 grid place-content-center gap-2 bg-black/80 p-4 text-center">
              <p>The preview is unavailable. It may still be processing.</p>
              <Button onClick={() => { setMediaError(false); setAttempt((n) => n + 1); }}>Retry preview</Button>
            </div>
          )}
          </div>
          <div className="space-y-2 px-4 py-3">
            <PieceProgress clip={current} clock={clock} duration={duration} disabled={controlsDisabled} onSeek={seekPiece} />
            <div className="flex flex-wrap items-center gap-2">
              <Button disabled={controlsDisabled} onClick={toggle}>{playing ? "Pause" : "Play"}</Button>
              <Button disabled={controlsDisabled} aria-label="Back 10 seconds" onClick={() => skip(-10)}>−10s</Button>
              <Button disabled={controlsDisabled} aria-label="Forward 10 seconds" onClick={() => skip(10)}>+10s</Button>
              <div className="flex-1" />
              <select
                aria-label="Playback speed"
                className="rounded-md bg-panel-2 px-2 py-1.5 text-sm focus-visible:outline-mark disabled:opacity-60"
                disabled={controlsDisabled}
                value={rate}
                onChange={(e) => {
                  const speed = Number(e.target.value);
                  setRate(speed);
                  if (video.current) video.current.playbackRate = speed;
                }}
              >
                {RATES.map((speed) => <option key={speed} value={speed}>{speed}×</option>)}
              </select>
              <Button disabled={controlsDisabled} onClick={() => {
                setMuted(!muted);
                saveSetting("clipper.previewMuted", !muted);
              }}>{muted ? "Unmute" : "Mute"}</Button>
            </div>
          </div>
        </div>
      ) : (
        <div className="rounded-xl bg-panel p-5">
          <p className="font-semibold">All pieces reviewed</p>
          {compilationId && <Button className="mt-3" onClick={() => go(`clip/${compilationId}`)}>Open compilation</Button>}
        </div>
      )}
      <div ref={strip} className="flex gap-2 overflow-x-auto rounded-lg bg-panel p-2" aria-label="Pieces in stream order">
        {pieces.map((clip, index) => {
          const state = STATUS[clip.status as keyof typeof STATUS] ?? clip.status;
          return (
            <button
              key={clip.id}
              aria-current={current?.id === clip.id}
              aria-label={`Piece ${index + 1}, ${clip.title}, ${state}`}
              title={clip.title}
              disabled={busy || clip.status === "rejected"}
              className={cn(
                "shrink-0 rounded-md bg-panel-2 px-3 py-2 text-xs focus-visible:outline-mark disabled:opacity-60",
                current?.id === clip.id && "ring-2 ring-inset ring-mark",
                clip.status === "kept" && "text-cue",
                clip.status === "rejected" && "text-tally",
              )}
              onClick={() => move(clip)}
            >
              {index + 1} · {state}
            </button>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button disabled={busy || sequencePosition <= 0} onClick={() => moveBy(-1)}>Previous</Button>
        <Button disabled={busy || !playable[sequencePosition + 1]} onClick={() => moveBy(1)}>Next</Button>
        <div className="flex-1" />
        <Button disabled={busy || !current} onClick={edit}>Edit piece</Button>
        <Button disabled={busy || !undo} onClick={undoDecision}>Undo</Button>
        <Button tone="danger" disabled={!canDecide} onClick={() => decide("reject")}>Reject</Button>
        <Button tone="approve" disabled={!canDecide} onClick={() => decide("keep")}>Keep</Button>
      </div>
      <p className="text-xs text-graphite">← / → Previous / next · Space Play / pause · A Keep · R Reject · Z Undo</p>
    </div>
  );
}

function PieceProgress({ clip, clock, duration, disabled, onSeek }: {
  clip: Clip;
  clock: Clock;
  duration: number;
  disabled: boolean;
  onSeek: (time: number) => void;
}) {
  const elapsed = useClock(clock, (t) => clipTime(clip, t));
  const position = timecode(elapsed, 0);
  const total = timecode(duration, 0);
  return (
    <div className="flex items-center gap-3">
      <input
        type="range"
        aria-label="Piece playback position"
        aria-valuetext={`${position} of ${total}`}
        min={0}
        max={duration}
        step="any"
        value={elapsed}
        disabled={disabled}
        onChange={(e) => onSeek(e.target.valueAsNumber)}
        className="min-w-0 flex-1 cursor-pointer accent-mark focus-visible:outline-mark disabled:cursor-default"
      />
      <span className="shrink-0 font-mono text-xs tabular-nums">{position} / {total}</span>
    </div>
  );
}
