import { useEffect, useRef, useState, type PointerEvent, type ReactNode } from "react";
import { api, errorText, media, type Clip, type ClipPatch, type Line } from "../api";
import { BackIcon, MutedIcon, PauseIcon, PhoneIcon, PlayIcon, SparkIcon, TrashIcon, UploadIcon, VolumeIcon } from "../icons";
import { clamp, clock, cn, go, goBack, length, loadSetting, saveSetting, usePoll } from "../lib";
import { PlaylistSelect } from "../PlaylistSelect";
import { Button, ClipStatusChip, DestinationSelect, ErrorNote, KIND_LABEL, useToast } from "../ui";

const MAX_SHORT_S = 180;
const MIN_CLIP_S = 1;

type Draft = Required<ClipPatch>;
const toDraft = (c: Clip): Draft => ({
  title: c.title,
  description: c.description,
  start: c.start,
  end: c.end,
  make_short: c.make_short,
  destination_id: c.destination_id,
  playlist_id: c.playlist_id,
});
// Changes to these need a new render; the title, description and channel don't.
const RENDER_FIELDS = ["start", "end", "make_short"];

export function ClipEditor({ id }: { id: number }) {
  const { data: clip, error, setData } = usePoll(() => api.clip(id), 5000, id);
  const [lines, setLines] = useState<Line[]>([]);
  useEffect(() => {
    api.transcript(id).then(setLines, () => setLines([]));
  }, [id]);

  if (error && !clip) return <div className="p-6"><ErrorNote>{error}</ErrorNote></div>;
  if (!clip) return null;
  return <Editor clip={clip} lines={lines} onSaved={setData} />;
}

function Editor({ clip, lines, onSaved }: { clip: Clip; lines: Line[]; onSaved: (c: Clip) => void }) {
  const toast = useToast();
  const [draft, setDraft] = useState<Draft>(() => toDraft(clip));
  const [busy, setBusy] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const [now, setNow] = useState(clip.start);
  const [playing, setPlaying] = useState(false);
  const [videoError, setVideoError] = useState(false);
  const stopAt = useRef<number | null>(null);

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

  const saved = toDraft(clip);
  const changes = Object.fromEntries(
    Object.entries(draft).filter(([k, v]) => saved[k as keyof Draft] !== v),
  ) as ClipPatch;
  const dirty = Object.keys(changes).length > 0;
  const needsRender = Object.keys(changes).some((k) => RENDER_FIELDS.includes(k));
  const { data: destinations } = usePoll(api.destinations, 0);
  const duration = draft.end - draft.start;
  const maxEnd = clip.vod_duration || clip.end + 600;
  const editable = clip.kind !== "whole";

  // While playing, follow the playhead every frame and stop at the end of a preview.
  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    const tick = () => {
      const v = video.current;
      if (v) {
        setNow(v.currentTime);
        if (stopAt.current !== null && v.currentTime >= stopAt.current) {
          v.pause();
          stopAt.current = null;
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing]);

  const seek = (t: number) => {
    const v = video.current;
    if (!v) return;
    v.currentTime = clamp(t, 0, maxEnd);
    setNow(v.currentTime);
  };
  const playRange = (from: number, to: number) => {
    const v = video.current;
    if (!v) return;
    seek(from);
    stopAt.current = to;
    void v.play();
  };
  // Jump back or ahead while watching. It keeps playing, even past the end of a preview.
  const skip = (seconds: number) => {
    const v = video.current;
    if (!v) return;
    stopAt.current = null;
    seek(v.currentTime + seconds);
  };
  const togglePlay = () => {
    const v = video.current;
    if (!v) return;
    if (v.paused) {
      stopAt.current = null;
      void v.play();
    } else v.pause();
  };

  // Changing a cut point plays the few seconds around it, so you hear exactly where it lands.
  const setStart = (t: number, preview = true) => {
    const start = Math.round(clamp(t, 0, draft.end - MIN_CLIP_S) * 100) / 100;
    setDraft((d) => ({ ...d, start }));
    if (preview) playRange(start, Math.min(start + 3, draft.end));
    else seek(start);
  };
  const setEnd = (t: number, preview = true) => {
    const end = Math.round(clamp(t, draft.start + MIN_CLIP_S, maxEnd) * 100) / 100;
    setDraft((d) => ({ ...d, end, make_short: d.make_short && end - d.start <= MAX_SHORT_S }));
    if (preview) playRange(Math.max(draft.start, end - 3), end);
    else seek(end);
  };

  const nextClipAfter = async (): Promise<string> => {
    const inbox = await api.clips(["proposed"]);
    const i = inbox.findIndex((c) => c.id === clip.id);
    const next = inbox[i + 1] ?? inbox.find((c) => c.id !== clip.id);
    return next ? `clip/${next.id}` : "inbox";
  };

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

  const save = () =>
    run(async () => {
      onSaved(await api.updateClip(clip.id, changes));
      toast({ text: clip.status !== "proposed" && needsRender ? "Saved: it will render again" : "Saved" });
    });

  const decide = (approve: boolean) =>
    run(async () => {
      // Extras are opened from their video, so that's where to go back to.
      const next = clip.status === "proposed" ? await nextClipAfter() : null;
      if (dirty) await api.updateClip(clip.id, changes);
      await (approve ? api.approve(clip.id) : api.reject(clip.id));
      toast({
        text: approve ? "Approved: it will render shortly" : "Rejected",
        action: { label: "Undo", run: () => void api.restore(clip.id).then(() => go(`clip/${clip.id}`)) },
      });
      if (next) go(next);
      else goBack("videos");
    });

  const restore = () => run(async () => onSaved(await api.restore(clip.id)));

  // Approved clips can still be thrown away, render and all.
  const discard = () =>
    run(async () => {
      await api.reject(clip.id);
      toast({ text: "Deleted", action: { label: "Undo", run: () => void api.approve(clip.id) } });
      goBack("approved");
    });

  return (
    <div className="mx-auto min-h-dvh max-w-2xl">
      {/* Video and trim bar stay on screen while scrolling the controls below. */}
      <div className="sticky top-0 z-20 bg-zinc-950 pt-[env(safe-area-inset-top)] shadow-[0_8px_16px_-8px_rgba(0,0,0,0.8)]">
        <div className="flex items-center gap-2 px-2 py-2">
          <button onClick={() => goBack("inbox")} className="rounded-full p-2 active:bg-white/10" aria-label="Back">
            <BackIcon className="size-6" />
          </button>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold">{clip.vod_title}</p>
            <p className="text-xs text-zinc-400">
              {KIND_LABEL[clip.kind]} · {length(duration)}
            </p>
          </div>
          <ClipStatusChip status={clip.status} paused={clip.paused} />
        </div>
        <div className="relative aspect-video bg-black" onClick={togglePlay}>
          <video
            ref={video}
            src={media.proxy(clip.vod_id)}
            playsInline
            preload="auto"
            className="size-full"
            onLoadedMetadata={(e) => {
              // iPhones ignore volume set from a page (only the hardware buttons work); offer mute only.
              const v = e.currentTarget;
              v.volume = 0.5;
              setVolumeAdjustable(v.volume === 0.5);
              v.volume = volume;
              v.muted = muted;
              seek(draft.start);
            }}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onSeeked={() => video.current && setNow(video.current.currentTime)}
            onError={() => setVideoError(true)}
          />
          {!playing && !videoError && (
            <div className="pointer-events-none absolute inset-0 grid place-items-center">
              <span className="rounded-full bg-black/60 p-4">
                <PlayIcon className="size-8" />
              </span>
            </div>
          )}
          {videoError && (
            <p className="absolute inset-0 grid place-items-center p-6 text-center text-sm text-zinc-400">
              The preview is still being made, so this can't play yet. Read the transcript below, or come back in a bit.
            </p>
          )}
          <span className="absolute left-2 top-2 rounded bg-black/70 px-1.5 py-0.5 font-mono text-xs tabular-nums">
            {clock(now, true)}
          </span>
        </div>
        {editable && (
          <TrimBar
            start={draft.start}
            end={draft.end}
            now={now}
            max={maxEnd}
            onStart={(t) => setStart(t, false)}
            onEnd={(t) => setEnd(t, false)}
            onSeek={seek}
          />
        )}
      </div>

      <div className="space-y-5 px-4 pb-40 pt-4">
        <div className="grid grid-cols-3 gap-2">
          <Button onClick={() => (playing ? video.current?.pause() : playRange(draft.start, draft.end))}>
            {playing ? <PauseIcon className="size-4" /> : <PlayIcon className="size-4" />}
            {playing ? "Pause" : "Play clip"}
          </Button>
          <Button onClick={() => playRange(draft.start, Math.min(draft.end, draft.start + 5))}>First 5s</Button>
          <Button onClick={() => playRange(Math.max(draft.start, draft.end - 5), draft.end)}>Last 5s</Button>
        </div>
        <div className="-mt-3 grid grid-cols-4 gap-2">
          {[-10, -5, 5, 10].map((seconds) => (
            <Button key={seconds} onClick={() => skip(seconds)}>
              {seconds < 0 ? `« ${-seconds}s` : `${seconds}s »`}
            </Button>
          ))}
        </div>

        <div className="flex items-center gap-2 rounded-2xl bg-zinc-900 py-1.5 pl-1.5 pr-4 ring-1 ring-white/5">
          <button
            onClick={() => {
              if (muted && volume === 0) setVolume(0.5);
              setMuted((m) => !m);
            }}
            aria-label={muted ? "Unmute preview" : "Mute preview"}
            className="rounded-xl p-2.5 text-zinc-300 active:bg-white/10"
          >
            {muted || volume === 0 ? <MutedIcon /> : <VolumeIcon />}
          </button>
          {volumeAdjustable ? (
            <>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={muted ? 0 : volume}
                onChange={(e) => {
                  const level = Number(e.target.value);
                  setVolume(level);
                  setMuted(level === 0);
                }}
                aria-label="Preview volume"
                className="h-2 flex-1 accent-violet-500"
              />
              <span className="w-10 text-right text-xs tabular-nums text-zinc-400">{muted ? 0 : Math.round(volume * 100)}%</span>
            </>
          ) : (
            <span className="text-xs text-zinc-500">{muted ? "Muted" : "Use your phone's volume buttons"}</span>
          )}
        </div>
        <p className="-mt-3 px-1 text-[11px] text-zinc-500">Preview volume only. The clip's own sound isn't changed.</p>

        {editable && (
          <>
            <CutPoint label="Start" value={draft.start} onNudge={(d) => setStart(draft.start + d)} onHere={() => setStart(now)} />
            <CutPoint label="End" value={draft.end} onNudge={(d) => setEnd(draft.end + d)} onHere={() => setEnd(now)} />
          </>
        )}

        <Field label="Title" hint={`${draft.title.length}/100`}>
          {/* Two lines so a full 100-character title is visible; line breaks aren't allowed in titles. */}
          <textarea
            value={draft.title}
            maxLength={100}
            rows={2}
            onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value.replace(/\s*\n\s*/g, " ") }))}
            className="w-full resize-none rounded-xl bg-zinc-900 px-3.5 py-3 text-base font-semibold leading-snug outline-none ring-1 ring-white/5 focus:ring-2 focus:ring-violet-500"
          />
        </Field>
        <Field label="Description">
          <textarea
            value={draft.description}
            rows={4}
            onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))}
            className="w-full rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 focus:ring-2 focus:ring-violet-500"
          />
        </Field>

        {clip.kind === "highlight" && (
          <label className="flex items-center gap-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
            <PhoneIcon className="size-5 text-zinc-400" />
            <span className="flex-1">
              <span className="block text-sm font-semibold">Also make a vertical Short</span>
              <span className="block text-xs text-zinc-500">
                {duration > MAX_SHORT_S ? "Shorts can be at most 3 minutes" : "1080×1920 with word-by-word captions"}
              </span>
            </span>
            <input
              type="checkbox"
              checked={draft.make_short}
              disabled={duration > MAX_SHORT_S}
              onChange={(e) => setDraft((d) => ({ ...d, make_short: e.target.checked }))}
              className="size-5 accent-violet-500"
            />
          </label>
        )}

        <Field label="Upload to">
          <DestinationSelect
            value={draft.destination_id}
            destinations={destinations}
            onChange={(destination_id) => setDraft((d) => ({ ...d, destination_id, playlist_id: "" }))}
          />
          <PlaylistSelect
            destination={destinations?.find((d) => d.id === draft.destination_id)}
            value={draft.playlist_id}
            onChange={(playlist_id) => setDraft((d) => ({ ...d, playlist_id }))}
          />
        </Field>

        {clip.why && (
          <p className="flex gap-2 rounded-2xl bg-violet-500/10 p-4 text-sm text-violet-200 ring-1 ring-violet-500/20">
            <SparkIcon className="mt-0.5 size-4 shrink-0" />
            <span>
              <span className="font-semibold">Why Claude picked it: </span>
              {clip.why}
            </span>
          </p>
        )}
        {clip.status === "extra" && (
          <p className="rounded-2xl bg-zinc-900 p-4 text-sm text-zinc-400 ring-1 ring-white/5">
            An extra: Claude scored it below this show's best few for the stream, so it isn't in your inbox. Approve it
            to use it anyway.
          </p>
        )}
        {clip.status === "failed" && <ErrorNote>{clip.error}</ErrorNote>}

        {lines.length > 0 && (
          <Transcript
            lines={lines}
            start={draft.start}
            end={draft.end}
            now={now}
            onPlay={(l) => playRange(l.start, l.end + 0.3)}
            onStartHere={(l) => setStart(l.start - 0.3)}
            onEndHere={(l) => setEnd(l.end + 0.6)}
          />
        )}
      </div>

      {/* Actions */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-white/5 bg-zinc-950/90 pb-[env(safe-area-inset-bottom)] backdrop-blur">
        <div className="mx-auto flex max-w-2xl gap-2 p-3">
          {(clip.status === "proposed" || clip.status === "extra") && (
            <>
              <Button tone="danger" disabled={busy} onClick={() => decide(false)}>
                Reject
              </Button>
              {dirty && (
                <Button disabled={busy} onClick={save}>
                  Save
                </Button>
              )}
              <Button tone="approve" disabled={busy || !draft.title.trim()} onClick={() => decide(true)} className="flex-1">
                Approve
              </Button>
            </>
          )}
          {clip.status === "rejected" && (
            <Button disabled={busy} onClick={restore} className="flex-1">
              Back to inbox
            </Button>
          )}
          {["approved", "rendering", "failed", "rendered"].includes(clip.status) && (
            <Button tone="danger" disabled={busy} onClick={discard} className="px-3.5">
              <TrashIcon className="size-4" />
              <span className="sr-only">Delete</span>
            </Button>
          )}
          {["approved", "rendering", "failed"].includes(clip.status) && (
            <Button tone="primary" disabled={busy || !dirty} onClick={save} className="flex-1">
              {!dirty ? "No changes" : needsRender ? "Save and render again" : "Save"}
            </Button>
          )}
          {["rendered", "uploaded"].includes(clip.status) && (
            <>
              {dirty && (
                <Button disabled={busy} onClick={save} className={needsRender ? "flex-1" : undefined}>
                  {needsRender ? "Save and render again" : "Save"}
                </Button>
              )}
              {!(dirty && needsRender) && (
                <Button tone="primary" onClick={() => go(`upload/${clip.id}`)} className="flex-1">
                  <UploadIcon className="size-4" /> {clip.status === "uploaded" ? "Uploaded" : "Upload"}
                </Button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 flex justify-between text-xs font-semibold uppercase tracking-wide text-zinc-500">
        {label}
        {hint && <span className="font-normal normal-case tabular-nums">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

function CutPoint(props: { label: string; value: number; onNudge: (delta: number) => void; onHere: () => void }) {
  const nudge = "rounded-lg bg-zinc-800 py-2.5 text-sm font-semibold tabular-nums active:bg-zinc-700";
  return (
    <div className="rounded-2xl bg-zinc-900 p-3 ring-1 ring-white/5">
      <div className="mb-2 flex items-baseline justify-between px-1">
        <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">{props.label}</span>
        <span className="font-mono text-lg tabular-nums">{clock(props.value, true)}</span>
      </div>
      <div className="grid grid-cols-5 gap-1.5">
        <button className={nudge} onClick={() => props.onNudge(-1)}>−1s</button>
        <button className={nudge} onClick={() => props.onNudge(-0.2)}>−0.2</button>
        <button className={cn(nudge, "bg-violet-600/80 active:bg-violet-700")} onClick={props.onHere}>
          Here
        </button>
        <button className={nudge} onClick={() => props.onNudge(0.2)}>+0.2</button>
        <button className={nudge} onClick={() => props.onNudge(1)}>+1s</button>
      </div>
    </div>
  );
}

/** The clip on a strip of the surrounding stream, with handles to drag its start and end. */
function TrimBar(props: {
  start: number;
  end: number;
  now: number;
  max: number;
  onStart: (t: number) => void;
  onEnd: (t: number) => void;
  onSeek: (t: number) => void;
}) {
  const { start, end, now, max } = props;
  const track = useRef<HTMLDivElement>(null);
  const dragging = useRef<"start" | "end" | null>(null);
  const windowFor = (s: number, e: number) => {
    const pad = Math.max(10, (e - s) * 0.25);
    return { from: Math.max(0, s - pad), to: Math.min(max, e + pad) };
  };
  const [view, setView] = useState(() => windowFor(start, end));

  // Re-frame the strip once a drag ends near (or past) its edges.
  useEffect(() => {
    if (dragging.current) return;
    const span = view.to - view.from;
    if (start < view.from + span * 0.03 || end > view.to - span * 0.03 || end - start < span * 0.2) {
      setView(windowFor(start, end));
    }
  }, [start, end]); // eslint-disable-line react-hooks/exhaustive-deps -- re-frame only when the cut points move

  const span = view.to - view.from || 1;
  const pct = (t: number) => `${((clamp(t, view.from, view.to) - view.from) / span) * 100}%`;
  const timeAt = (clientX: number) => {
    const r = track.current!.getBoundingClientRect();
    return view.from + clamp((clientX - r.left) / r.width, 0, 1) * span;
  };

  const handle = (which: "start" | "end") => ({
    onPointerDown: (e: PointerEvent<HTMLDivElement>) => {
      e.stopPropagation();
      e.currentTarget.setPointerCapture(e.pointerId);
      dragging.current = which;
    },
    onPointerMove: (e: PointerEvent<HTMLDivElement>) => {
      if (dragging.current !== which) return;
      (which === "start" ? props.onStart : props.onEnd)(timeAt(e.clientX));
    },
    onPointerUp: () => {
      dragging.current = null;
      setView((v) => (start < v.from || end > v.to ? windowFor(start, end) : v));
    },
  });

  return (
    <div className="select-none px-4 pb-3 pt-3">
      <div ref={track} className="relative h-12 touch-none" onPointerDown={(e) => props.onSeek(timeAt(e.clientX))}>
        <div className="absolute inset-x-0 top-1 bottom-1 rounded-lg bg-zinc-800" />
        <div
          className="absolute top-1 bottom-1 rounded-lg bg-violet-500/30 ring-2 ring-inset ring-violet-400"
          style={{ left: pct(start), width: `calc(${pct(end)} - ${pct(start)})` }}
        />
        {now >= view.from && now <= view.to && (
          <div className="pointer-events-none absolute inset-y-0 w-0.5 -translate-x-1/2 bg-white" style={{ left: pct(now) }} />
        )}
        {(["start", "end"] as const).map((which) => (
          <div
            key={which}
            {...handle(which)}
            role="slider"
            aria-label={which === "start" ? "Clip start" : "Clip end"}
            aria-valuenow={which === "start" ? start : end}
            className="absolute inset-y-0 flex w-10 -translate-x-1/2 cursor-ew-resize touch-none items-center justify-center"
            style={{ left: pct(which === "start" ? start : end) }}
          >
            <div className="h-12 w-2.5 rounded-full bg-violet-300 shadow-lg shadow-black/50" />
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between font-mono text-[11px] tabular-nums text-zinc-500">
        <span>{clock(view.from)}</span>
        <span>{clock(view.to)}</span>
      </div>
    </div>
  );
}

function Transcript(props: {
  lines: Line[];
  start: number;
  end: number;
  now: number;
  onPlay: (l: Line) => void;
  onStartHere: (l: Line) => void;
  onEndHere: (l: Line) => void;
}) {
  const mini = "rounded-md bg-zinc-800 px-2 py-1 text-[11px] font-semibold text-zinc-300 active:bg-zinc-700";
  return (
    <section>
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        Transcript <span className="font-normal normal-case">· tap a line to hear it</span>
      </h2>
      <ol className="divide-y divide-white/5 overflow-hidden rounded-2xl bg-zinc-900 ring-1 ring-white/5">
        {props.lines.map((l) => {
          const inside = l.end > props.start && l.start < props.end;
          const current = props.now >= l.start && props.now < l.end;
          return (
            <li key={l.start} className={cn("flex items-start gap-2 px-3 py-2", current && "bg-violet-500/15")}>
              <button onClick={() => props.onPlay(l)} className={cn("flex-1 text-left", !inside && "opacity-40")}>
                <span className="mr-2 font-mono text-[11px] tabular-nums text-zinc-500">{clock(l.start)}</span>
                <span className="text-sm leading-snug">{l.text}</span>
              </button>
              <div className="flex shrink-0 gap-1 pt-0.5">
                <button className={mini} onClick={() => props.onStartHere(l)}>Start</button>
                <button className={mini} onClick={() => props.onEndHere(l)}>End</button>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
