import { useState, type FormEvent } from "react";
import { api, errorText, type AppSettings, type Clip, type Mode, type Pauses, type Show, type Vod } from "../api";
import { ClipCard } from "../ClipCard";
import { LinkIcon, PauseIcon, PlayIcon, TrashIcon } from "../icons";
import { ago, applyTitlePattern, cn, go, length, platformName, timeOfDay, titleDate, usePoll } from "../lib";
import { PauseBar } from "../PauseBar";
import { Button, ErrorNote, MODE_LABEL, ProgressBar, Screen, VodStatusChip, useToast } from "../ui";

const WITH_PROGRESS = ["downloading", "preparing", "transcribing"];
const ACTIVE = [...WITH_PROGRESS, "queued", "analysing", "segmenting"];
const PAUSABLE = [...ACTIVE, "waiting"];

export function Videos() {
  const { data: vods, error, refresh } = usePoll(api.vods, 4000);
  const { data: pauses, setData: setPauses } = usePoll(api.pauses, 10000);
  return (
    <Screen title="Videos" subtitle="Add a link and the clips land in your inbox.">
      <AddVideo onAdded={refresh} />
      <div className="mt-3">
        <PauseBar parts={["downloads", "processing", "renders", "uploads"]} pauses={pauses} onChange={setPauses} />
      </div>
      <DownloadAndDisk />
      {error && <ErrorNote>{error}</ErrorNote>}
      <div className="mt-6 space-y-3">
        {vods?.map((v) => <VodRow key={v.id} vod={v} pauses={pauses} onChanged={refresh} />)}
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

const EIGHT_HOUR_STREAM_GB = 25; // a typical 8-hour 1080p VOD, for the time estimate

/** Download speed, disk space and notifications. */
function DownloadAndDisk() {
  const { data: settings, setData } = usePoll(api.settings, 0);
  const { data: storage } = usePoll(api.storage, 60000);
  if (!settings) return null;

  const limit = settings.download_limit_mbps;
  const minutes = limit > 0 ? Math.round((EIGHT_HOUR_STREAM_GB * 8000) / limit / 60) : 0;
  const days = settings.cleanup_after_days;
  const lowSpace = storage?.free_gb != null && storage.free_gb < storage.min_free_gb;

  return (
    <div className="mt-3 space-y-1.5 px-1">
      <SettingLine
        text={
          limit > 0
            ? `Downloads are limited to ${limit} Mbit/s, so an 8-hour stream takes about ${minutes} minutes.`
            : "Downloads use your full internet speed."
        }
        label="Download speed limit (Mbit/s)"
        hint="Around half your internet speed keeps everything else usable. 0 means no limit, which is fastest: it downloads several pieces at once. A download that's running switches within a few seconds."
        value={limit}
        onSave={async (v) => setData(await api.updateSettings({ download_limit_mbps: v }))}
      />
      <FullSpeedHours settings={settings} onChange={setData} />
      <SettingLine
        text={
          (storage?.free_gb != null ? `${storage.free_gb} GB free on the data disk; streams use ${storage.videos_gb} GB. ` : "") +
          `A stream's video files are deleted ${days === 0 ? "as soon as" : `${days} day${days === 1 ? "" : "s"} after`} all its clips are uploaded or rejected.`
        }
        warning={lowSpace ? `Below ${storage!.min_free_gb} GB free: new downloads wait for space.` : undefined}
        label="Delete finished streams' files after (days)"
        hint="The transcript and Claude's picks are kept; the clips can't be re-cut after this. Each rendered clip is deleted as soon as it's uploaded."
        value={days}
        onSave={async (v) => setData(await api.updateSettings({ cleanup_after_days: v }))}
      />
      <Notifications />
    </div>
  );
}

const hourLabel = (h: number) => `${h % 12 || 12} ${h % 24 < 12 ? "AM" : "PM"}`;

/** "EDT" for America/New_York, or the zone's name if the browser doesn't know a short one. */
function zoneLabel(timezone: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: timezone, timeZoneName: "short" }).formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value ?? timezone;
  } catch {
    return timezone;
  }
}

/** Hours of the day when downloads ignore the speed limit, such as when overnight streams end and nobody's online. */
function FullSpeedHours({ settings, onChange }: { settings: AppSettings; onChange: (s: AppSettings) => void }) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [from, setFrom] = useState(5);
  const [until, setUntil] = useState(11);
  const on = settings.full_speed_from !== settings.full_speed_until;
  const zone = zoneLabel(settings.timezone);

  const save = async (body: { full_speed_from: number; full_speed_until: number }, done: string) => {
    try {
      onChange(await api.updateSettings(body));
      setEditing(false);
      toast({ text: done });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  if (editing) {
    const select = (value: number, set: (v: number) => void, label: string) => (
      <label className="flex-1">
        <span className="mb-1 block text-xs text-zinc-500">{label}</span>
        <select
          value={value}
          onChange={(e) => set(Number(e.target.value))}
          className="w-full appearance-none rounded-xl bg-zinc-800 px-3.5 py-3 text-base outline-none focus:ring-2 focus:ring-violet-500"
        >
          {Array.from({ length: 24 }, (_, h) => (
            <option key={h} value={h}>
              {hourLabel(h)}
            </option>
          ))}
        </select>
      </label>
    );
    return (
      <div className="space-y-2 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
        <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Full speed hours ({zone})</p>
        <div className="flex gap-2">
          {select(from, setFrom, "From")}
          {select(until, setUntil, "Until")}
        </div>
        <div className="flex gap-2">
          {on && <Button onClick={() => save({ full_speed_from: 0, full_speed_until: 0 }, "Full speed hours off")}>Turn off</Button>}
          <Button onClick={() => setEditing(false)}>Cancel</Button>
          <Button tone="primary" disabled={from === until} onClick={() => save({ full_speed_from: from, full_speed_until: until }, "Saved.")} className="flex-1">
            Save
          </Button>
        </div>
        <p className="text-xs text-zinc-500">
          Between these hours downloads ignore the limit and use your whole connection, such as right after overnight streams
          end. A download that's running speeds up or slows down within a few seconds when they start and end.
        </p>
      </div>
    );
  }
  return (
    <button
      onClick={() => {
        if (on) {
          setFrom(settings.full_speed_from);
          setUntil(settings.full_speed_until);
        }
        setEditing(true);
      }}
      className="block w-full text-left text-xs text-zinc-500"
    >
      {on
        ? `Full speed from ${hourLabel(settings.full_speed_from)} to ${hourLabel(settings.full_speed_until)} (${zone}). `
        : "No full-speed hours. "}
      <span className="font-semibold text-violet-300">{on ? "Change" : "Set some"}</span>
    </button>
  );
}

function Notifications() {
  const toast = useToast();
  const { data } = usePoll(api.notifications, 0);
  if (!data) return null;
  const where = [data.ntfy && "ntfy", data.discord && "Discord"].filter(Boolean).join(" and ");
  if (!where) {
    return <p className="text-xs text-zinc-500">Notifications are off. Set them up for ntfy or Discord in the server's .env (README).</p>;
  }
  const test = async () => {
    try {
      await api.testNotification();
      toast({ text: `Sent a test to ${where}.` });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };
  return (
    <button onClick={test} className="block w-full text-left text-xs text-zinc-500">
      Notifications go to {where}.{!data.links && " Set CLIPPER_APP_URL so they open the app."}{" "}
      <span className="font-semibold text-violet-300">Send a test</span>
    </button>
  );
}

function SettingLine(props: {
  text: string;
  warning?: string;
  label: string;
  hint: string;
  value: number;
  onSave: (value: number) => Promise<void>;
}) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");

  const save = async () => {
    try {
      await props.onSave(Math.max(0, Number(value) || 0));
      setEditing(false);
      toast({ text: "Saved." });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  if (editing) {
    return (
      <div className="space-y-2 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
        <label className="block text-xs font-semibold uppercase tracking-wide text-zinc-500">{props.label}</label>
        <div className="flex gap-2">
          <input
            type="number"
            inputMode="decimal"
            min={0}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="min-w-0 flex-1 rounded-xl bg-zinc-800 px-3.5 py-3 text-base outline-none focus:ring-2 focus:ring-violet-500"
          />
          <Button onClick={() => setEditing(false)}>Cancel</Button>
          <Button tone="primary" onClick={save}>
            Save
          </Button>
        </div>
        <p className="text-xs text-zinc-500">{props.hint}</p>
      </div>
    );
  }
  return (
    <button
      onClick={() => {
        setValue(String(props.value));
        setEditing(true);
      }}
      className="block w-full text-left text-xs text-zinc-500"
    >
      {props.text} {props.warning && <span className="text-amber-300">{props.warning} </span>}
      <span className="font-semibold text-violet-300">Change</span>
    </button>
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
  const held = heldBy(vod, pauses);
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
      className="rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5 active:bg-zinc-800/70"
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
        </div>
      )}
      {vod.status === "ready" && counts.length > 0 && <p className="mt-2 text-sm text-zinc-400">{counts.join(" · ")}</p>}
      {vod.cleaned && <p className="mt-1 text-xs text-zinc-500">Video files deleted to free space; the clips can't be re-cut.</p>}
    </div>
  );
}

export function VideoDetail({ id }: { id: number }) {
  const toast = useToast();
  const { data, error, refresh } = usePoll(() => api.vod(id), 5000, id);
  const vod = data?.vod;
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
    >
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
        {offered.map((c) => <ClipCard key={c.id} clip={c} showStatus />)}
        {data && data.clips.length === 0 && <p className="text-sm text-zinc-500">No clips yet.</p>}
      </div>
      {extras.length > 0 && <Extras clips={extras} />}
      {vod && ["ready", "failed"].includes(vod.status) && !vod.cleaned && (
        <Button tone="danger" onClick={deleteFiles} className="mt-8 w-full">
          <TrashIcon className="size-4" /> Delete this stream's video files
        </Button>
      )}
      {vod?.cleaned && <p className="mt-8 text-center text-xs text-zinc-500">Video files deleted to free space.</p>}
    </Screen>
  );
}

/** Claude's picks beyond the show's best few: not in the inbox, but there to approve if you want more. */
function Extras({ clips }: { clips: Clip[] }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="mt-6">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-baseline gap-2 px-1 pb-2 text-left">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Extras · {clips.length}</h2>
        <span className="flex-1 text-xs text-zinc-500">Lower-scored picks, not in your inbox.</span>
        <span className="text-xs font-semibold text-violet-300">{open ? "Hide" : "Show"}</span>
      </button>
      {open && (
        <div className="space-y-3">
          {[...clips].sort((a, b) => b.score - a.score).map((c) => (
            <ClipCard key={c.id} clip={c} showStatus />
          ))}
        </div>
      )}
    </section>
  );
}
