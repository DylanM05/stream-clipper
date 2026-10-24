import { useState, type FormEvent } from "react";
import { api, errorText, type Mode, type Show, type Vod } from "../api";
import { ClipCard } from "../ClipCard";
import { LinkIcon } from "../icons";
import { ago, applyTitlePattern, cn, go, length, platformName, timeOfDay, titleDate, usePoll } from "../lib";
import { Button, ErrorNote, MODE_LABEL, ProgressBar, Screen, VodStatusChip, useToast } from "../ui";

const WITH_PROGRESS = ["downloading", "preparing", "transcribing"];
const ACTIVE = [...WITH_PROGRESS, "queued", "analysing", "segmenting"];

export function Videos() {
  const { data: vods, error, refresh } = usePoll(api.vods, 4000);
  return (
    <Screen title="Videos" subtitle="Add a link and the clips land in your inbox.">
      <AddVideo onAdded={refresh} />
      {error && <ErrorNote>{error}</ErrorNote>}
      <div className="mt-6 space-y-3">
        {vods?.map((v) => <VodRow key={v.id} vod={v} onChanged={refresh} />)}
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
          placeholder="Paste a Twitch, Kick, YouTube or X link"
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
          placeholder="Notes for Claude: who's on the show, running jokes, title style…"
          className="w-full rounded-xl bg-zinc-800 px-3.5 py-3 text-base outline-none ring-violet-500 placeholder:text-zinc-500 focus:ring-2"
        />
      ) : (
        <button type="button" onClick={() => setShowNotes(true)} className="text-sm font-medium text-violet-300">
          + Add notes for Claude
        </button>
      )}
      <Button type="submit" tone="primary" disabled={!url.trim() || busy} className="w-full">
        {busy ? "Adding…" : "Add video"}
      </Button>
    </form>
  );
}

function VodRow({ vod, onChanged }: { vod: Vod; onChanged: () => void }) {
  const toast = useToast();
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
      className="rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5 active:bg-zinc-800/70"
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="line-clamp-2 break-words font-semibold leading-snug">{vod.title || vod.url}</h3>
          <p className="mt-0.5 truncate text-xs text-zinc-500">{details}</p>
        </div>
        <VodStatusChip status={vod.status} />
      </div>
      {ACTIVE.includes(vod.status) && (
        <div className="mt-3">
          <ProgressBar value={WITH_PROGRESS.includes(vod.status) ? vod.progress : null} />
        </div>
      )}
      {vod.status === "waiting" && (
        <p className="mt-2 text-sm text-amber-300">
          Claude's usage limit was reached. It tries again {vod.retry_at ? `at ${timeOfDay(vod.retry_at)}` : "soon"}.
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
        </div>
      )}
      {vod.status === "ready" && counts.length > 0 && <p className="mt-2 text-sm text-zinc-400">{counts.join(" · ")}</p>}
    </div>
  );
}

export function VideoDetail({ id }: { id: number }) {
  const { data, error } = usePoll(() => api.vod(id), 5000, id);
  const vod = data?.vod;
  return (
    <Screen title={vod?.title || "Video"} subtitle={vod && <VodStatusChip status={vod.status} />} onBack={() => go("videos")}>
      {error && <ErrorNote>{error}</ErrorNote>}
      {vod && (
        <div className="mb-4 space-y-2 text-sm text-zinc-400">
          <a href={vod.url} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 text-violet-300">
            <LinkIcon className="size-4" /> Open the original
          </a>
          {vod.notes && <p className="rounded-xl bg-zinc-900 p-3 ring-1 ring-white/5">Notes for Claude: {vod.notes}</p>}
        </div>
      )}
      <div className="space-y-3">
        {data?.clips.map((c) => <ClipCard key={c.id} clip={c} showStatus />)}
        {data && data.clips.length === 0 && <p className="text-sm text-zinc-500">No clips yet.</p>}
      </div>
    </Screen>
  );
}
