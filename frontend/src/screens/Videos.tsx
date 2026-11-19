import { useState, type FormEvent, type ReactNode } from "react";
import { api, errorText, type Clip, type ClipStatus, type Mode, type Pauses, type Show, type Vod } from "../api";
import { ClipCard } from "../ClipCard";
import { ChevronIcon, LinkIcon, PauseIcon, PlayIcon, ScissorsIcon, TrashIcon } from "../icons";
import { ago, applyTitlePattern, cn, go, length, pastDay, platformName, titleDate, upcoming, useDesktop, usePoll } from "../lib";
import { usePauses, WorkStrip } from "../Work";
import { Button, ErrorNote, Fold, MODE_LABEL, ProgressBar, Screen, ShowMore, VodStatusChip, useToast } from "../ui";

export const WITH_PROGRESS = ["downloading", "preparing", "transcribing"];
const RUNNING = [...WITH_PROGRESS, "analysing", "segmenting"]; // being worked on right now
export const ACTIVE = [...RUNNING, "queued"];
const PAUSABLE = [...ACTIVE, "waiting"];

/** A video can be deleted unless it's being worked on (pause it first) or has clips on YouTube. */
const deletable = (vod: Vod) => !RUNNING.includes(vod.status) && !vod.counts.uploaded;

/** Deletes a video from the app after asking; resolves to whether it did. */
function useDeleteVod() {
  const toast = useToast();
  return async (vod: Vod): Promise<boolean> => {
    if (!confirm(`Delete “${vod.title || vod.url}”? Its clips and files go too, and a channel you watch won't queue it again.`)) {
      return false;
    }
    try {
      await api.deleteVod(vod.id);
      toast({ text: "Video deleted." });
      return true;
    } catch (e) {
      toast({ text: errorText(e) });
      return false;
    }
  };
}

/** The Videos list in groups, in the server's order (newest first) within each. */
function groupVods(vods: Vod[]) {
  const attention = vods.filter((v) => v.status === "failed");
  const working = vods.filter((v) => PAUSABLE.includes(v.status));
  const review = vods.filter((v) => v.status === "ready" && (v.counts.proposed ?? 0) > 0);
  const done = vods.filter((v) => !attention.includes(v) && !working.includes(v) && !review.includes(v));
  return [
    { id: "videos:attention", title: "Needs you", vods: attention, tone: "bg-tally/15 text-red-300" },
    { id: "videos:working", title: "In progress", vods: working, tone: undefined },
    { id: "videos:review", title: "To review", vods: review, tone: "bg-mark/15 text-mark" },
    { id: "videos:done", title: "Done", vods: done, tone: undefined },
  ].filter((g) => g.vods.length > 0);
}

export function Videos() {
  const { data: vods, error, refresh } = usePoll(api.vods, 4000);
  const { pauses } = usePauses();
  return (
    <Screen title="Videos" subtitle="Add a link and the clips land in your inbox." wide>
      {/* The work strip is in the sidebar on a desktop. */}
      <div className="lg:max-w-5xl">
        <AddVideo onAdded={refresh} />
        <WorkStrip stacked className="mt-3 rounded-2xl bg-panel p-1.5 lg:hidden" />
        {error && <ErrorNote>{error}</ErrorNote>}
        <div className="mt-2">
          {vods?.length === 0 && <p className="pt-10 text-center text-sm text-graphite">No videos yet. Paste a link to add one.</p>}
          {groupVods(vods ?? []).map((g) => (
            <Fold key={g.id} id={g.id} title={g.title} count={g.vods.length} tone={g.tone}>
              <ShowMore items={g.vods}>
                {(shown) => (
                  <ul className="divide-y divide-line">
                    {shown.map((v) => (
                      <li key={v.id}>
                        <VodRow vod={v} pauses={pauses} onChanged={refresh} />
                      </li>
                    ))}
                  </ul>
                )}
              </ShowMore>
            </Fold>
          ))}
        </div>
      </div>
    </Screen>
  );
}

const isXLink = (url: string) => /^(https?:\/\/)?(www\.|mobile\.)?(x|twitter)\.com\//i.test(url.trim());

/** The show a pasted link belongs to, matched the way the server does it before downloading. */
function showForLink(shows: Show[] | undefined, url: string): Show | undefined {
  const link = url.trim().toLowerCase();
  if (!link) return undefined;
  return shows?.find((s) =>
    s.match
      .split(/[\n,]+/)
      .map((t) => t.trim().toLowerCase())
      .some((t) => t && link.includes(t)),
  );
}

function AddVideo({ onAdded }: { onAdded: () => void }) {
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [mode, setMode] = useState<"auto" | Mode>("auto");
  const [notes, setNotes] = useState("");
  const [showNotes, setShowNotes] = useState(false);
  const [busy, setBusy] = useState(false);
  const { data: shows } = usePoll(api.shows, 0);
  const canPaste = window.isSecureContext && !!navigator.clipboard?.readText;
  const show = showForLink(shows, url);
  const autoMode: Mode = show?.mode ?? (isXLink(url) ? "whole" : "segment");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.addVod({ url: url.trim(), mode: mode === "auto" ? undefined : mode, notes });
      setUrl("");
      setNotes("");
      setShowNotes(false);
      toast({ text: "Added. Processing starts shortly." });
      onAdded();
    } catch (err) {
      toast({ text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };

  const modes: [typeof mode, string][] = [
    ["auto", "Auto"],
    ["segment", "Clips"],
    ["whole", "Full"],
    ["both", "Both"],
  ];

  return (
    <form onSubmit={submit} className="space-y-3">
      <div className="flex gap-2">
        <label className="flex min-w-0 flex-1 items-center gap-2.5 rounded-xl bg-panel px-3.5 ring-1 ring-line focus-within:ring-2 focus-within:ring-mark">
          <LinkIcon className="size-4 shrink-0 text-dim" />
          <input
            type="url"
            inputMode="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="Paste a Twitch, Kick, YouTube, Rumble or X link"
            aria-label="Video link"
            className="min-w-0 flex-1 bg-transparent py-3 text-base outline-none placeholder:text-graphite focus-visible:outline-none"
          />
        </label>
        {canPaste && (
          <Button onClick={async () => setUrl((await navigator.clipboard.readText()).trim())}>Paste</Button>
        )}
        <span className="hidden shrink-0 lg:block">
          <Button type="submit" tone="primary" disabled={!url.trim() || busy} className="h-full">
            {busy ? "Adding…" : "Add video"}
          </Button>
        </span>
      </div>
      {show && (
        <p className="text-[13px] text-graphite">
          Show: <span className="font-semibold text-chalk">{show.name}</span>, with titles like “
          {applyTitlePattern(show.mode === "whole" ? show.episode_template || show.title_template : show.title_template, "…", titleDate())}”
        </p>
      )}
      <div className="lg:flex lg:items-center lg:gap-4">
        <div className="grid grid-cols-4 gap-1 rounded-xl bg-panel p-1 lg:w-96 lg:shrink-0">
          {modes.map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setMode(value)}
              className={cn(
                "rounded-lg px-2 py-2 text-xs font-semibold",
                mode === value ? "bg-zinc-700 text-chalk shadow-[inset_0_-2px_0_var(--color-mark)]" : "text-graphite hover:text-chalk",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <p className="mt-1.5 flex-1 text-[13px] text-graphite lg:mt-0">
          Makes: {MODE_LABEL[mode === "auto" ? autoMode : mode]}
          {mode === "auto" && (show ? " (from the show)" : " (from the link)")}
        </p>
        {!showNotes && (
          <button type="button" onClick={() => setShowNotes(true)} className="mt-2 text-sm font-semibold text-mark lg:mt-0">
            + Add notes for the AI
          </button>
        )}
      </div>
      {showNotes && (
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={3}
          placeholder="Notes for the AI: who's on the show, running jokes, title style…"
          aria-label="Notes for the AI"
          className="w-full rounded-xl bg-panel px-3.5 py-3 text-base outline-none ring-1 ring-line placeholder:text-graphite focus:ring-2 focus:ring-mark"
        />
      )}
      <Button type="submit" tone="primary" disabled={!url.trim() || busy} className="w-full lg:hidden">
        {busy ? "Adding…" : "Add video"}
      </Button>
    </form>
  );
}

/** Why a video is waiting, and when it tries again. */
export function waitingText(vod: Vod): string {
  return `${vod.error || "Waiting."} It tries again ${vod.retry_at ? upcoming(vod.retry_at) : "soon"}.`;
}

/** Why a queued video isn't moving, when a whole part of the work is paused. */
function heldBy(vod: Vod, pauses?: Pauses): string {
  if (vod.paused || vod.status !== "queued" || !pauses) return "";
  if (!vod.downloaded && pauses.downloads) return "Waiting: downloads are paused.";
  if (vod.downloaded && pauses.processing) return "Downloaded. Waiting: processing is paused.";
  return "";
}

function VodRow({ vod, pauses, onChanged }: { vod: Vod; pauses?: Pauses; onChanged: () => void }) {
  const toast = useToast();
  const deleteVod = useDeleteVod();
  const held = heldBy(vod, pauses);
  const deleteButton = deletable(vod) && (
    <RowButton tone="danger" onClick={() => void deleteVod(vod).then((deleted) => deleted && onChanged())}>
      <TrashIcon className="size-3.5" /> Delete
    </RowButton>
  );
  const togglePause = async () => {
    try {
      await (vod.paused ? api.resumeVod(vod.id) : api.pauseVod(vod.id));
      onChanged();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };
  const details = [
    vod.show_name,
    MODE_LABEL[vod.mode],
    vod.platform && platformName(vod.platform),
    vod.duration ? `${length(vod.duration)} long` : null,
    `Added ${ago(vod.created_at)}`,
  ].filter(Boolean);
  const counts = [
    vod.counts.proposed && `${vod.counts.proposed} to review`,
    (vod.counts.approved || vod.counts.rendering) && `${(vod.counts.approved ?? 0) + (vod.counts.rendering ?? 0)} rendering`,
    vod.counts.rendered && `${vod.counts.rendered} ready`,
    vod.counts.uploaded && `${vod.counts.uploaded} uploaded`,
    vod.counts.extra && `${vod.counts.extra} extra`,
    vod.counts.rejected && `${vod.counts.rejected} rejected`,
  ].filter(Boolean);

  const retry = async () => {
    try {
      await api.retryVod(vod.id);
      onChanged();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  // What's holding it up or went wrong, if anything, then its buttons.
  const message =
    vod.status === "failed" ? (
      <p className="line-clamp-3 break-words text-[13px] text-red-300">{vod.error}</p>
    ) : vod.status === "waiting" ? (
      <p className="text-[13px] text-amber-300">{waitingText(vod)}</p>
    ) : held ? (
      <p className="text-[13px] text-amber-300">{held}</p>
    ) : null;
  const pausable = held || PAUSABLE.includes(vod.status);
  const buttons = (pausable || vod.status === "failed") && (
    <>
      {/* Retrying now is for when it can go on sooner: another AI chosen in Settings, or space freed up. */}
      {(vod.status === "failed" || vod.status === "waiting") && (
        <RowButton onClick={retry}>{vod.status === "failed" ? "Retry" : "Retry now"}</RowButton>
      )}
      {pausable && (
        <RowButton onClick={togglePause}>
          {vod.paused ? <PlayIcon className="size-3.5" /> : <PauseIcon className="size-3.5" />}
          {vod.paused ? "Resume" : "Pause"}
        </RowButton>
      )}
      {deleteButton}
    </>
  );

  // The row opens the video; its buttons do their own thing.
  return (
    <div
      role="link"
      tabIndex={0}
      onClick={() => go(`videos/${vod.id}`)}
      onKeyDown={(e) => e.key === "Enter" && e.target === e.currentTarget && go(`videos/${vod.id}`)}
      className="group -mx-2 cursor-pointer rounded-lg px-2 py-3.5 hover:bg-white/[0.03]"
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="line-clamp-2 break-words font-semibold leading-snug">{vod.title || vod.url}</h3>
          <p className="mt-0.5 flex flex-wrap gap-x-3 text-[13px] text-graphite">
            {details.map((d) => (
              <span key={d as string}>{d}</span>
            ))}
          </p>
        </div>
        <VodStatusChip status={vod.status} paused={vod.paused} />
        <ChevronIcon className="mt-0.5 hidden size-4 shrink-0 -rotate-90 text-dim group-hover:text-graphite sm:block" />
      </div>
      {ACTIVE.includes(vod.status) && !vod.paused && !held && (
        <div className="mt-2.5 max-w-md">
          <ProgressBar value={WITH_PROGRESS.includes(vod.status) ? vod.progress : null} />
        </div>
      )}
      {(message || buttons) && (
        <div className="mt-2 space-y-2">
          {message}
          {buttons && <div className="flex flex-wrap gap-1.5">{buttons}</div>}
        </div>
      )}
      {vod.status === "ready" && counts.length > 0 && (
        <p className="mt-1.5 flex flex-wrap gap-x-3 text-[13px] text-chalk">
          {counts.map((c) => (
            <span key={c as string}>{c}</span>
          ))}
        </p>
      )}
      {vod.cleaned && <p className="mt-1 text-xs text-graphite">Video files deleted to free space; the clips can't be re-cut.</p>}
    </div>
  );
}

/** A small button on a video's row, which doesn't also open the video. */
function RowButton({ onClick, tone, children }: { onClick: () => void; tone?: "danger"; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={cn(
        "flex min-h-8 shrink-0 items-center gap-1.5 rounded-lg bg-panel-2 px-3 text-[13px] font-semibold hover:bg-zinc-700 active:bg-zinc-700",
        tone === "danger" && "text-red-300",
      )}
    >
      {children}
    </button>
  );
}

export function VideoDetail({ id }: { id: number }) {
  const toast = useToast();
  const desktop = useDesktop();
  const deleteVod = useDeleteVod();
  const { data, error, refresh } = usePoll(() => api.vod(id), 5000, id);
  const vod = data?.vod;
  const canDeleteFiles = vod && ["ready", "failed"].includes(vod.status) && !vod.cleaned;
  const offered = data?.clips.filter((c) => c.status !== "extra") ?? [];
  const extras = data?.clips.filter((c) => c.status === "extra") ?? [];

  const retry = async () => {
    try {
      await api.retryVod(id);
      void refresh();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  const deleteFiles = async () => {
    // Kept clips stay kept once the compilation they're in is uploaded; otherwise they're rejected too.
    const compiled = data?.clips.some((c) => c.kind === "compilation" && c.status === "uploaded");
    const stays: ClipStatus[] = compiled ? ["uploaded", "rejected", "kept"] : ["uploaded", "rejected"];
    const dropped = data?.clips.filter((c) => !stays.includes(c.status)).length ?? 0;
    const warning = dropped ? ` ${dropped} clip${dropped === 1 ? " that isn't" : "s that aren't"} uploaded can't be cut any more and will be rejected.` : "";
    if (!confirm(`Delete this stream's video files now to free space?${warning}`)) return;
    try {
      await api.deleteVodFiles(id);
      toast({ text: "Video files deleted." });
      void refresh();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  return (
    <Screen
      title={vod?.title || "Video"}
      subtitle={vod && <VodStatusChip status={vod.status} paused={vod.paused} />}
      onBack={() => go("videos")}
      wide
    >
      {error && <ErrorNote>{error}</ErrorNote>}
      {vod && (vod.status === "waiting" || vod.status === "failed") && (
        <div className="mb-4 flex items-start gap-3 rounded-xl bg-zinc-900 p-3 ring-1 ring-white/5">
          <p className={cn("min-w-0 flex-1 break-words text-sm", vod.status === "failed" ? "text-red-300" : "text-amber-300")}>
            {vod.status === "failed" ? vod.error : waitingText(vod)}
          </p>
          <button onClick={retry} className="shrink-0 rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-semibold active:bg-zinc-700">
            {vod.status === "failed" ? "Retry" : "Retry now"}
          </button>
        </div>
      )}
      {vod && (
        <div className="mb-4 space-y-2 text-sm text-zinc-400">
          <a href={vod.url} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 text-violet-300 hover:text-violet-200">
            <LinkIcon className="size-4" /> Open the original
          </a>
          {vod.status === "ready" && !vod.cleaned && (
            <button onClick={() => go(`new/${vod.id}`)} className="flex items-center gap-1.5 text-violet-300 hover:text-violet-200">
              <ScissorsIcon className="size-4" /> Make a clip from any part of it
            </button>
          )}
          {desktop && <VodFacts vod={vod} />}
          {vod.notes && <p className="rounded-xl bg-zinc-900 p-3 ring-1 ring-white/5">Notes for the AI: {vod.notes}</p>}
        </div>
      )}
      <div className="space-y-3 xl:grid xl:grid-cols-2 xl:items-start xl:gap-3 xl:space-y-0">
        {offered.map((c) => <ClipCard key={c.id} clip={c} showStatus />)}
        {data && data.clips.length === 0 && <p className="text-sm text-zinc-500">No clips yet.</p>}
      </div>
      {extras.length > 0 && <Extras clips={extras} />}
      {(canDeleteFiles || (vod && deletable(vod))) && (
        // Full width, one above the other, on a phone; side by side on a desktop.
        <div className="mt-8 flex flex-col gap-3 lg:flex-row">
          {canDeleteFiles && (
            <Button tone="danger" onClick={deleteFiles} className="w-full lg:w-auto">
              <TrashIcon className="size-4" /> Delete this stream's video files
            </Button>
          )}
          {vod && deletable(vod) && (
            <Button
              tone="danger"
              onClick={() => void deleteVod(vod).then((deleted) => deleted && go("videos"))}
              className="w-full lg:w-auto"
            >
              <TrashIcon className="size-4" /> Delete this video
            </Button>
          )}
        </div>
      )}
      {vod?.cleaned && <p className="mt-8 text-center text-xs text-zinc-500">Video files deleted to free space.</p>}
    </Screen>
  );
}

/** More about a video, on a desktop where there's room: where it's from, how long it is, and where its clips stand. */
function VodFacts({ vod }: { vod: Vod }) {
  const c = vod.counts;
  const clips = [
    c.proposed && `${c.proposed} to review`,
    (c.approved || c.rendering) && `${(c.approved ?? 0) + (c.rendering ?? 0)} rendering`,
    c.failed && `${c.failed} failed to render`,
    c.rendered && `${c.rendered} ready`,
    c.uploaded && `${c.uploaded} uploaded`,
    c.extra && `${c.extra} extra`,
    c.rejected && `${c.rejected} rejected`,
  ].filter(Boolean);
  const facts: [string, string][] = [
    ["Show", vod.show_name || "None matched"],
    ["Makes", MODE_LABEL[vod.mode]],
    ["From", [vod.platform && platformName(vod.platform), vod.channel].filter(Boolean).join(" · ") || "–"],
    ["Length", vod.duration ? length(vod.duration) : "–"],
    ["Added", `${pastDay(vod.created_at)}, ${ago(vod.created_at)}`],
    ["Clips", clips.join(", ") || "None yet"],
  ];
  return (
    <dl className="grid grid-cols-5 gap-x-6 gap-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5 xl:grid-cols-7">
      {facts.map(([label, value]) => (
        // The clip counts run long: a row of their own, or two columns on a wide screen.
        <div key={label} className={cn("min-w-0", label === "Clips" && "col-span-5 xl:col-span-2")}>
          <dt className="text-xs font-medium text-zinc-500">{label}</dt>
          <dd className="mt-0.5 break-words text-sm text-zinc-200">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The AI's picks beyond the show's best few: not in the inbox, but there to approve if you want more. */
function Extras({ clips }: { clips: Clip[] }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="mt-6">
      <button onClick={() => setOpen((o) => !o)} className="group flex w-full items-baseline gap-2 px-1 pb-2 text-left">
        <h2 className="text-xs font-medium text-zinc-500">Extras · {clips.length}</h2>
        <span className="flex-1 text-xs text-zinc-500">Lower-scored picks, not in your inbox.</span>
        <span className="text-xs font-semibold text-violet-300 group-hover:text-violet-200">{open ? "Hide" : "Show"}</span>
      </button>
      {open && (
        <div className="space-y-3 xl:grid xl:grid-cols-2 xl:items-start xl:gap-3 xl:space-y-0">
          {[...clips].sort((a, b) => b.score - a.score).map((c) => (
            <ClipCard key={c.id} clip={c} showStatus />
          ))}
        </div>
      )}
    </section>
  );
}
