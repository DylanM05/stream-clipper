// The clip editor on a phone: the video and trim strip stay on top while the controls scroll under them.

import { useEffect, useRef, useState, type PointerEvent, type ReactNode } from "react";
import type { Line } from "../api";
import { BackIcon, MutedIcon, PauseIcon, PhoneIcon, PlayIcon, SparkIcon, TrashIcon, UploadIcon, VolumeIcon } from "../icons";
import { clamp, clock, cn, go, goBack, length } from "../lib";
import { PlaylistSelect } from "../PlaylistSelect";
import { Button, ClipStatusChip, DestinationSelect, ErrorNote, KIND_LABEL } from "../ui";
import { startAt, useClock, type Clock } from "./clock";
import { MAX_SHORT_S, type Range } from "./edit";
import type { Editor } from "./useEditor";

export function PhoneEditor({ ed }: { ed: Editor }) {
  const { clip, draft, setDraft, playing, duration, editable, destinations, busy, dirty, needsRender } = ed;
  const making = ed.source.mode === "new";
  const vodTitle = clip ? clip.vod_title : ed.source.mode === "new" ? ed.source.vod.title || ed.source.vod.url : "";

  return (
    <div className="mx-auto min-h-dvh max-w-2xl">
      {/* Video and trim bar stay on screen while scrolling the controls below. */}
      <div className="sticky top-0 z-20 bg-zinc-950 pt-[env(safe-area-inset-top)] shadow-[0_8px_16px_-8px_rgba(0,0,0,0.8)]">
        <div className="flex items-center gap-2 px-2 py-2">
          <button onClick={() => goBack(making ? `videos/${ed.vodId}` : "inbox")} className="rounded-full p-2 active:bg-white/10" aria-label="Back">
            <BackIcon className="size-6" />
          </button>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold">{vodTitle}</p>
            <p className="text-xs text-zinc-400">
              {making && "New "}
              {making ? KIND_LABEL[draft.kind].toLowerCase() : KIND_LABEL[draft.kind]} · {length(duration)}
              {draft.cuts.length > 0 && ` · ${draft.cuts.length} cut${draft.cuts.length === 1 ? "" : "s"}`}
            </p>
          </div>
          {clip && <ClipStatusChip status={clip.status} paused={clip.paused} upload={clip.upload_state} />}
        </div>
        <div className="relative aspect-video bg-black" onClick={ed.togglePlay}>
          <video {...ed.videoProps} className="size-full" />
          {!playing && !ed.videoError && (
            <div className="pointer-events-none absolute inset-0 grid place-items-center">
              <span className="rounded-full bg-black/60 p-4">
                <PlayIcon className="size-8" />
              </span>
            </div>
          )}
          {ed.videoError && (
            <p className="absolute inset-0 grid place-items-center p-6 text-center text-sm text-zinc-400">
              The preview is still being made, so this can't play yet. Read the transcript below, or come back in a bit.
            </p>
          )}
          <span className="absolute left-2 top-2 rounded bg-black/70 px-1.5 py-0.5 font-mono text-xs tabular-nums">
            <PlayheadTime clock={ed.clock} />
          </span>
        </div>
        {editable && (
          <TrimBar
            start={draft.start}
            end={draft.end}
            cuts={draft.cuts}
            clock={ed.clock}
            max={ed.length}
            onStart={(t) => ed.setStart(t, false)}
            onEnd={(t) => ed.setEnd(t, false)}
            onSeek={ed.seek}
          />
        )}
      </div>

      <div className="space-y-5 px-4 pb-40 pt-4">
        <div className="grid grid-cols-3 gap-2">
          <Button onClick={() => (playing ? ed.pause() : ed.playClip())}>
            {playing ? <PauseIcon className="size-4" /> : <PlayIcon className="size-4" />}
            {playing ? "Pause" : "Play clip"}
          </Button>
          <Button onClick={() => ed.playFirst(5)}>First 5s</Button>
          <Button onClick={() => ed.playLast(5)}>Last 5s</Button>
        </div>
        <div className="-mt-3 grid grid-cols-4 gap-2">
          {[-10, -5, 5, 10].map((seconds) => (
            <Button key={seconds} onClick={() => ed.skip(seconds)}>
              {seconds < 0 ? `« ${-seconds}s` : `${seconds}s »`}
            </Button>
          ))}
        </div>

        <div className="flex items-center gap-2 rounded-2xl bg-zinc-900 py-1.5 pl-1.5 pr-4 ring-1 ring-white/5">
          <button
            onClick={() => {
              if (ed.muted && ed.volume === 0) ed.setVolume(0.5);
              ed.setMuted((m) => !m);
            }}
            aria-label={ed.muted ? "Unmute preview" : "Mute preview"}
            className="rounded-xl p-2.5 text-zinc-300 active:bg-white/10"
          >
            {ed.muted || ed.volume === 0 ? <MutedIcon /> : <VolumeIcon />}
          </button>
          {ed.volumeAdjustable ? (
            <>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={ed.muted ? 0 : ed.volume}
                onChange={(e) => {
                  const level = Number(e.target.value);
                  ed.setVolume(level);
                  ed.setMuted(level === 0);
                }}
                aria-label="Preview volume"
                className="h-2 flex-1 accent-violet-500"
              />
              <span className="w-10 text-right text-xs tabular-nums text-zinc-400">{ed.muted ? 0 : Math.round(ed.volume * 100)}%</span>
            </>
          ) : (
            <span className="text-xs text-zinc-500">{ed.muted ? "Muted" : "Use your phone's volume buttons"}</span>
          )}
        </div>
        <p className="-mt-3 px-1 text-[11px] text-zinc-500">Preview volume only. The clip's own sound isn't changed.</p>

        {editable && (
          <>
            <CutPoint label="Start" value={draft.start} onNudge={(d) => ed.setStart(draft.start + d)} onHere={() => ed.setStart(ed.clock.get(), true, making)} />
            <CutPoint label="End" value={draft.end} onNudge={(d) => ed.setEnd(draft.end + d)} onHere={() => ed.setEnd(ed.clock.get(), true, making)} />
          </>
        )}

        {ed.source.mode === "new" && (
          <Field label="What it is">
            <div className="grid grid-cols-2 gap-1 rounded-xl bg-zinc-800 p-1">
              {(["highlight", "chapter"] as const).map((kind) => (
                <button
                  key={kind}
                  onClick={() => setDraft((d) => ({ ...d, kind, make_short: kind === "highlight" && d.make_short }))}
                  className={cn("rounded-lg py-2 text-sm font-semibold", draft.kind === kind ? "bg-zinc-600 text-white" : "text-zinc-400")}
                >
                  {kind === "highlight" ? "Highlight" : "Clip"}
                </button>
              ))}
            </div>
          </Field>
        )}

        {ed.source.mode === "new" ? (
          <Field label="Title" hint={`${draft.title.length}/${ed.source.info.title_room}`}>
            <input
              value={draft.title}
              maxLength={ed.source.info.title_room}
              onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))}
              placeholder="What happens in it"
              className="w-full rounded-xl bg-zinc-900 px-3.5 py-3 text-base font-semibold outline-none ring-1 ring-white/5 placeholder:font-normal placeholder:text-zinc-500 focus:ring-2 focus:ring-violet-500"
            />
            {ed.source.info.title_pattern !== "{title}" && (
              <span className="mt-1.5 block text-xs text-zinc-500">Goes up as “{ed.fullTitle || "…"}”</span>
            )}
          </Field>
        ) : (
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
        )}
        <Field label="Description">
          <textarea
            value={draft.description}
            rows={4}
            onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))}
            className="w-full rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 focus:ring-2 focus:ring-violet-500"
          />
          {clip?.description_footer && !draft.description.includes(clip.description_footer) && (
            <p className="mt-1.5 line-clamp-2 text-xs text-zinc-500">Goes up with the show's footer under it: {clip.description_footer}</p>
          )}
        </Field>

        {draft.kind === "highlight" && (
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

        {clip?.why && (
          <p className="flex gap-2 rounded-2xl bg-violet-500/10 p-4 text-sm text-violet-200 ring-1 ring-violet-500/20">
            <SparkIcon className="mt-0.5 size-4 shrink-0" />
            <span>
              <span className="font-semibold">Why the AI picked it: </span>
              {clip.why}
            </span>
          </p>
        )}
        {clip?.status === "extra" && (
          <p className="rounded-2xl bg-zinc-900 p-4 text-sm text-zinc-400 ring-1 ring-white/5">
            An extra: the AI scored it below this show's best few for the stream, so it isn't in your inbox. Approve it
            to use it anyway.
          </p>
        )}
        {clip?.status === "failed" && <ErrorNote>{clip.error}</ErrorNote>}

        {draft.cuts.length > 0 && (
          <p className="rounded-2xl bg-zinc-900 p-4 text-sm text-zinc-400 ring-1 ring-white/5">
            {draft.cuts.length} part{draft.cuts.length === 1 ? " is" : "s are"} cut out of the middle, striped on the strip
            above; playing jumps over {draft.cuts.length === 1 ? "it" : "them"}. Cut parts out on a computer.{" "}
            <button onClick={() => ed.commit({ start: draft.start, end: draft.end, cuts: [] })} className="font-semibold text-violet-300">
              Put {draft.cuts.length === 1 ? "it" : "them"} back
            </button>
          </p>
        )}

        {making ? (
          <StreamTranscript ed={ed} />
        ) : (
          ed.lines.length > 0 && (
            <Transcript
              lines={ed.lines}
              start={draft.start}
              end={draft.end}
              clock={ed.clock}
              onPlay={(l) => ed.playRange(l.start, l.end + 0.3)}
              onStartHere={(l) => ed.setStart(l.start - 0.3)}
              onEndHere={(l) => ed.setEnd(l.end + 0.6)}
            />
          )
        )}
      </div>

      {/* Actions */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-white/5 bg-zinc-950/90 pb-[env(safe-area-inset-bottom)] backdrop-blur">
        <div className="mx-auto flex max-w-2xl gap-2 p-3">
          {actions()}
        </div>
      </div>
    </div>
  );

  // A function rather than a component, so its buttons aren't remade each time the playhead moves.
  function actions() {
    if (ed.source.mode === "new") {
      const ready = !busy && !!draft.title.trim();
      return (
        <>
          <Button disabled={!ready} onClick={() => ed.create(false)}>
            Save to inbox
          </Button>
          <Button tone="approve" disabled={!ready} onClick={() => ed.create(true)} className="flex-1">
            Approve
          </Button>
        </>
      );
    }
    const c = ed.source.clip;
    return (
      <>
        {(c.status === "proposed" || c.status === "extra") && (
          <>
            <Button tone="danger" disabled={busy} onClick={() => ed.decide(false)}>
              Reject
            </Button>
            {dirty && (
              <Button disabled={busy} onClick={ed.save}>
                Save
              </Button>
            )}
            <Button tone="approve" disabled={busy || !draft.title.trim()} onClick={() => ed.decide(true)} className="flex-1">
              Approve
            </Button>
          </>
        )}
        {c.status === "rejected" && (
          <Button disabled={busy} onClick={ed.restore} className="flex-1">
            Back to inbox
          </Button>
        )}
        {["approved", "rendering", "failed", "rendered"].includes(c.status) && (
          <Button tone="danger" disabled={busy} onClick={ed.discard} className="px-3.5">
            <TrashIcon className="size-4" />
            <span className="sr-only">Delete</span>
          </Button>
        )}
        {["approved", "rendering", "failed"].includes(c.status) && (
          <Button tone="primary" disabled={busy || !dirty} onClick={ed.save} className="flex-1">
            {!dirty ? "No changes" : needsRender ? "Save and render again" : "Save"}
          </Button>
        )}
        {["rendered", "uploaded"].includes(c.status) && (
          <>
            {dirty && (
              <Button disabled={busy} onClick={ed.save} className={needsRender ? "flex-1" : undefined}>
                {needsRender ? "Save and render again" : "Save"}
              </Button>
            )}
            {!(dirty && needsRender) && (
              <Button tone="primary" onClick={() => go(`upload/${c.id}`)} className="flex-1">
                <UploadIcon className="size-4" /> {c.status === "uploaded" ? "Uploaded" : "Upload"}
              </Button>
            )}
          </>
        )}
      </>
    );
  }
}

/** A new clip's transcript: a search of the whole stream, or the lines around the playhead. */
function StreamTranscript({ ed }: { ed: Editor }) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const found = q.length >= 2 ? ed.lines.filter((l) => l.text.toLowerCase().includes(q)).slice(0, 60) : null;
  // Recentred every half minute of the playhead, rather than on its every move.
  const near = useClock(ed.clock, (t) => Math.floor(t / 30) * 30);
  const around = ed.lines.filter((l) => l.end > near - 60 && l.start < near + 120).slice(0, 80);
  return (
    <div className="space-y-2">
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search what was said in the stream"
        className="w-full rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 placeholder:text-zinc-500 focus:ring-2 focus:ring-violet-500"
      />
      {found && found.length === 0 && <p className="px-1 text-sm text-zinc-500">Nothing like that was said.</p>}
      {(found ?? around).length > 0 && (
        <Transcript
          lines={found ?? around}
          start={ed.draft.start}
          end={ed.draft.end}
          clock={ed.clock}
          onPlay={(l) => ed.playRange(l.start, l.end + 0.3)}
          onStartHere={(l) => ed.setStart(l.start - 0.3, true, true)}
          onEndHere={(l) => ed.setEnd(l.end + 0.6, true, true)}
          heading={found ? `${found.length}${found.length === 60 ? "+" : ""} found` : "around the playhead"}
        />
      )}
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

/** The clip on a strip of the surrounding stream, with handles to drag its start and end. Parts cut out of it are
 * striped. */
function TrimBar(props: {
  start: number;
  end: number;
  cuts: Range[];
  clock: Clock;
  max: number;
  onStart: (t: number) => void;
  onEnd: (t: number) => void;
  onSeek: (t: number) => void;
}) {
  const { start, end, max } = props;
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
        {props.cuts.map(([a, b]) => (
          <div
            key={a}
            className="pointer-events-none absolute top-2 bottom-2"
            style={{
              left: pct(a), width: `calc(${pct(b)} - ${pct(a)})`,
              background: "repeating-linear-gradient(135deg, rgba(239,68,68,0.5) 0 4px, rgba(9,9,11,0.7) 4px 8px)",
            }}
          />
        ))}
        <TrimPlayhead clock={props.clock} view={view} pct={pct} />
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
  clock: Clock;
  onPlay: (l: Line) => void;
  onStartHere: (l: Line) => void;
  onEndHere: (l: Line) => void;
  heading?: string;
}) {
  const mini = "rounded-md bg-zinc-800 px-2 py-1 text-[11px] font-semibold text-zinc-300 active:bg-zinc-700";
  const playing = useClock(props.clock, (t) => startAt(props.lines, t));
  return (
    <section>
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        Transcript <span className="font-normal normal-case">{`· ${props.heading ?? "tap a line to hear it"}`}</span>
      </h2>
      <ol className="divide-y divide-white/5 overflow-hidden rounded-2xl bg-zinc-900 ring-1 ring-white/5">
        {props.lines.map((l) => {
          const inside = l.end > props.start && l.start < props.end;
          const current = l.start === playing;
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

/** The playhead's time over the video, to the tenth: redrawn when that changes, not with the whole editor. */
function PlayheadTime({ clock: playhead }: { clock: Clock }) {
  return <>{useClock(playhead, (t) => clock(t, true))}</>;
}

/** The playhead on the trim strip, the one part of it that moves while playing. */
function TrimPlayhead(props: { clock: Clock; view: { from: number; to: number }; pct: (t: number) => string }) {
  const now = useClock(props.clock);
  if (now < props.view.from || now > props.view.to) return null;
  return <div className="pointer-events-none absolute inset-y-0 w-0.5 -translate-x-1/2 bg-white" style={{ left: props.pct(now) }} />;
}
