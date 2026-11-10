import { useState, type FormEvent } from "react";
import { api, errorText, type Clip, type Mode, type Pauses, type Show, type Vod } from "../api";
import { ClipCard } from "../ClipCard";
import { LinkIcon, PauseIcon, PlayIcon, ScissorsIcon, TrashIcon } from "../icons";
import { ago, applyTitlePattern, cn, go, length, pastDay, platformName, timeOfDay, titleDate, useDesktop, usePoll } from "../lib";
import { PauseBar, usePauses } from "../PauseBar";
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
    { id: "videos:attention", title: "Needs attention", vods: attention, tone: "bg-red-500/15 text-red-300" },
    { id: "videos:working", title: "In progress", vods: working, tone: "bg-sky-500/15 text-sky-300" },
    { id: "videos:review", title: "Clips to review", vods: review, tone: "bg-violet-500/15 text-violet-300" },
    { id: "videos:done", title: "Done", vods: done, tone: undefined },
  ].filter((g) => g.vods.length > 0);
}

export function Videos() {
  const { data: vods, error, refresh } = usePoll(api.vods, 4000);
  const { pauses, setPauses } = usePauses();
  return (
    <Screen title="Videos" subtitle="Add a link and the clips land in your inbox." wide>
      {/* On a wide screen, adding a video and the pause switches sit beside the list instead of above it. */}
      <div className="xl:grid xl:grid-cols-[24rem_minmax(0,1fr)] xl:items-start xl:gap-8">
        <div>
          <AddVideo onAdded={refresh} />
          <div className="mt-3">
            <PauseBar parts={["downloads", "processing", "renders", "uploads"]} pauses={pauses} onChange={setPauses} />
          </div>
        </div>
        <div>
          {error && <ErrorNote>{error}</ErrorNote>}
          <div className={cn("mt-3", !error && "xl:-mt-3")}>
            {vods?.length === 0 && <p className="hidden pt-10 text-center text-sm text-zinc-500 xl:block">No videos yet. Paste a link to add one.</p>}
            {groupVods(vods ?? []).map((g) => (
              <Fold key={g.id} id={g.id} title={g.title} count={g.vods.length} tone={g.tone}>
                <ShowMore items={g.vods}>
                  {(shown) => (
                    <div className="space-y-3">
                      {shown.map((v) => <VodRow key={v.id} vod={v} pauses={pauses} onChanged={refresh} />)}
                    </div>
                  )}
                </ShowMore>
              </Fold>
            ))}
          </div>
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
    <form onSubmit={submit} className="space-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
      <div className="flex gap-2">
        <input
          type="url"
          inputMode="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="Paste a Twitch, Kick, YouTube, Rumble or X link"
          className="min-w-0 flex-1 rounded-xl bg-zinc-800 px-3.5 py-3 text-base outline-none ring-violet-500 placeholder:text-zinc-500 focus:ring-2"
        />
        {canPaste && (
          <Button onClick={async () => setUrl((await navigator.clipboard.readText()).trim())}>Paste</Button>
        )}
      </div>
      {show && (
        <p className="text-xs text-zinc-400">
          Show: <span className="font-semibold text-zinc-200">{show.name}</span> · titles like “
          {applyTitlePattern(show.mode === "whole" ? show.episode_template || show.title_template : show.title_template, "…", titleDate())}”
        </p>
      )}
      <div>
        <div className="grid grid-cols-4 gap-1 rounded-xl bg-zinc-800 p-1">
          {modes.map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setMode(value)}
              className={cn(
                "rounded-lg px-2 py-2 text-xs font-semibold",
                mode === value ? "bg-zinc-600 text-white" : "text-zinc-400",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <p className="mt-1.5 text-xs text-zinc-500">
          Makes: {MODE_LABEL[mode === "auto" ? autoMode : mode]}
          {mode === "auto" && (show ? " (from the show)" : " (from the link)")}
        </p>
      </div>
      {showNotes ? (
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={3}
          placeholder="Notes for the AI: who's on the show, running jokes, title style…"
          className="w-full rounded-xl bg-zinc-800 px-3.5 py-3 text-base outline-none ring-violet-500 placeholder:text-zinc-500 focus:ring-2"
        />
      ) : (
        <button type="button" onClick={() => setShowNotes(true)} className="text-sm font-medium text-violet-300">
          + Add notes for the AI
        </button>
      )}
      <Button type="submit" tone="primary" disabled={!url.trim() || busy} className="w-full">
        {busy ? "Adding…" : "Add video"}
      </Button>
    </form>
  );
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
    <button
      onClick={(e) => {
        e.stopPropagation();
        void deleteVod(vod).then((deleted) => deleted && onChanged());
      }}
      className="flex shrink-0 items-center gap-1.5 rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-semibold text-red-300 active:bg-zinc-700"
    >
      <TrashIcon className="size-3.5" /> Delete
    </button>
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
    vod.duration ? length(vod.duration) : null,
    ago(vod.created_at),
  ]
    .filter(Boolean)
    .join(" · ");
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

  return (
    <div
      role="button"
      onClick={() => go(`videos/${vod.id}`)}
      className="rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5 hover:bg-zinc-800/40 active:bg-zinc-800/70"
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="line-clamp-2 break-words font-semibold leading-snug">{vod.title || vod.url}</h3>
          <p className="mt-0.5 truncate text-xs text-zinc-500">{details}</p>
        </div>
        <VodStatusChip status={vod.status} paused={vod.paused} />
      </div>
      {ACTIVE.includes(vod.status) && !vod.paused && !held && (
        <div className="mt-3">
          <ProgressBar value={WITH_PROGRESS.includes(vod.status) ? vod.progress : null} />
        </div>
      )}
      {(held || PAUSABLE.includes(vod.status)) && (
        <div className="mt-2 flex items-center gap-3">
          <p className="min-w-0 flex-1 text-sm text-amber-300">{held}</p>
          <button
            onClick={(e) => {
              e.stopPropagation();
              void togglePause();
            }}
            className="flex shrink-0 items-center gap-1.5 rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-semibold active:bg-zinc-700"
          >
            {vod.paused ? <PlayIcon className="size-3.5" /> : <PauseIcon className="size-3.5" />}
            {vod.paused ? "Resume" : "Pause"}
          </button>
          {deleteButton}
        </div>
      )}
      {vod.status === "waiting" && (
        <p className="mt-2 text-sm text-amber-300">
          {vod.error || "Waiting."} It tries again {vod.retry_at ? `at ${timeOfDay(vod.retry_at)}` : "soon"}.
        </p>
      )}
      {vod.status === "failed" && (
        <div className="mt-2 flex items-start gap-3">
          <p className="line-clamp-3 flex-1 break-words text-sm text-red-300">{vod.error}</p>
          <button
            onClick={(e) => {
              e.stopPropagation();
              void retry();
            }}
            className="shrink-0 rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-semibold active:bg-zinc-700"
          >
            Retry
          </button>
          {deleteButton}
        </div>
      )}
      {vod.status === "ready" && counts.length > 0 && <p className="mt-2 text-sm text-zinc-400">{counts.join(" · ")}</p>}
      {vod.cleaned && <p className="mt-1 text-xs text-zinc-500">Video files deleted to free space; the clips can't be re-cut.</p>}
    </div>
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

  const deleteFiles = async () => {
    const dropped = data?.clips.filter((c) => !["uploaded", "rejected"].includes(c.status)).length ?? 0;
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
          <dt className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">{label}</dt>
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
        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Extras · {clips.length}</h2>
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
