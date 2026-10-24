import { useState, type ReactNode } from "react";
import {
  api,
  errorText,
  type Channel,
  type ChannelFields,
  type ChannelVideo,
  type Destination,
  type Mode,
  type Show,
  type ShowFields,
} from "../api";
import { CopyIcon, LinkIcon, RefreshIcon, TvIcon, UploadIcon } from "../icons";
import { ago, applyTitlePattern, cn, copyText, go, length, titleDate, usePoll } from "../lib";
import { Button, DestinationSelect, ErrorNote, MODE_LABEL, Screen, useToast } from "../ui";
import { UploadBrowserStatus } from "./UploadKit";

const MAX_TITLE = 100;
const SAMPLE_TITLE = "Short title written by Claude";
const PLATFORM_NAME: Record<Channel["platform"], string> = { kick: "Kick", twitch: "Twitch", youtube: "YouTube" };

const hasClips = (mode: Mode) => mode !== "whole";
const hasEpisode = (mode: Mode) => mode !== "segment";

export function Shows() {
  const { data: shows, error } = usePoll(api.shows, 15000);
  const { data: destinations } = usePoll(api.destinations, 0);
  const destinationName = (id: number | null) => destinations?.find((d) => d.id === id)?.name;

  return (
    <Screen
      title="Shows"
      subtitle="Titles, context and channels to watch."
      actions={<Button tone="primary" onClick={() => go("shows/new")}>New show</Button>}
    >
      {error && <ErrorNote>{error}</ErrorNote>}
      <button
        onClick={() => go("shows/youtube")}
        className="mb-4 flex w-full items-center gap-3 rounded-2xl bg-zinc-900 p-4 text-left ring-1 ring-white/5 active:bg-zinc-800/70"
      >
        <UploadIcon className="size-5 text-zinc-400" />
        <span className="flex-1">
          <span className="block text-sm font-semibold">YouTube channels you upload to</span>
          <span className="block text-xs text-zinc-500">
            {destinations?.length ? destinations.map((d) => d.name).join(", ") : "None yet: add your clips channels here"}
          </span>
        </span>
      </button>

      {shows?.length === 0 && (
        <div className="mt-10 flex flex-col items-center text-center">
          <div className="rounded-full bg-zinc-900 p-5 ring-1 ring-white/5">
            <TvIcon className="size-8 text-zinc-500" />
          </div>
          <h2 className="mt-4 text-lg font-semibold">No shows yet</h2>
          <p className="mt-1 max-w-xs text-sm text-zinc-400">
            A show sets the titles, context for Claude and upload channel for its videos, and can watch Kick, Twitch
            and YouTube channels for new streams.
          </p>
        </div>
      )}
      <div className="space-y-3">
        {shows?.map((show) => {
          const pattern = hasClips(show.mode) ? show.title_template : show.episode_template || show.title_template;
          const goesTo = [
            hasEpisode(show.mode) && destinationName(show.episode_destination_id) && `episodes → ${destinationName(show.episode_destination_id)}`,
            hasClips(show.mode) && destinationName(show.clip_destination_id) && `clips → ${destinationName(show.clip_destination_id)}`,
          ].filter(Boolean);
          return (
            <button
              key={show.id}
              onClick={() => go(`shows/${show.id}`)}
              className="block w-full rounded-2xl bg-zinc-900 p-4 text-left ring-1 ring-white/5 active:bg-zinc-800/70"
            >
              <div className="flex items-center gap-2">
                <h3 className="min-w-0 flex-1 truncate font-semibold">{show.name}</h3>
                <span className="text-xs text-zinc-500">{MODE_LABEL[show.mode]}</span>
              </div>
              <p className="mt-1 line-clamp-2 text-sm text-zinc-400">{applyTitlePattern(pattern, SAMPLE_TITLE, titleDate())}</p>
              {goesTo.length > 0 && <p className="mt-1 text-xs text-zinc-500">{goesTo.join(" · ")}</p>}
              {show.channels.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {show.channels.map((c) => (
                    <ChannelBadge key={c.id} channel={c} />
                  ))}
                </div>
              )}
            </button>
          );
        })}
      </div>
    </Screen>
  );
}

function ChannelBadge({ channel }: { channel: Channel }) {
  const dot = !channel.enabled ? "bg-zinc-500" : channel.last_error ? "bg-red-400" : "bg-emerald-400";
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-zinc-800 px-2.5 py-1 text-xs text-zinc-300">
      <span className={cn("size-1.5 rounded-full", dot)} />
      {PLATFORM_NAME[channel.platform]} · {channelName(channel)}
    </span>
  );
}

const channelName = (c: Channel) => c.url.replace(/^https?:\/\/(www\.)?[^/]+\//, "");

// --- YouTube channels to upload to ---

export function UploadChannels() {
  const toast = useToast();
  const { data: destinations, refresh } = usePoll(api.destinations, 15000);
  const { data: youtube } = usePoll(api.youtube, 0);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");

  const add = async () => {
    try {
      await api.addDestination({ name: name.trim(), url: url.trim() });
      setName("");
      setUrl("");
      void refresh();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  return (
    <Screen title="YouTube channels" subtitle="Where your videos get uploaded." onBack={() => go("shows")}>
      {youtube && <YouTubeApiPanel configured={youtube.configured} callbackPath={youtube.callback_path} />}
      <div className="space-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. Kino Casino Clips" className={cn(input, "bg-zinc-800 ring-0")} />
        <input
          type="url"
          inputMode="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="Channel link, e.g. youtube.com/channel/UC…"
          className={cn(input, "bg-zinc-800 ring-0")}
        />
        <p className="text-xs text-zinc-500">
          The channel's link, like youtube.com/@name. Its channel ID is looked up from it, so uploads go to the right
          channel even when one Google account manages several.
        </p>
        <Button tone="primary" onClick={add} disabled={!name.trim()} className="w-full">
          Add channel
        </Button>
      </div>
      <div className="mt-4 space-y-3">
        {destinations?.map((d) => (
          <DestinationRow key={d.id} destination={d} apiReady={!!youtube?.configured} onChanged={refresh} />
        ))}
      </div>
      {destinations?.some((d) => !d.api_connected) && <SignInPanel />}
    </Screen>
  );
}

const HTTPS_OK = location.protocol === "https:" || ["localhost", "127.0.0.1"].includes(location.hostname);

/** Uploading through the YouTube API: how to set it up in Google Cloud, or how connecting works once it is. */
function YouTubeApiPanel({ configured, callbackPath }: { configured: boolean; callbackPath: string }) {
  const toast = useToast();
  const redirect = `${location.origin}${callbackPath}`;
  const link = "font-medium text-violet-300 underline-offset-2 active:underline";
  return (
    <div className="mb-4 space-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
      <p className="text-sm font-semibold">Upload through the YouTube API</p>
      {configured ? (
        <p className="text-xs text-zinc-500">
          Tap <b>Connect</b> on each channel below and pick that channel when Google asks which account to use. Google
          warns that it hasn't verified the app, since it's your own: tap <b>Advanced → Go to … (unsafe)</b> to continue.
          Channels you don't connect upload through the upload browser instead.
        </p>
      ) : (
        <>
          <p className="text-xs text-zinc-500">
            Set it up once in Google Cloud (free). It's sturdier than the upload browser and within YouTube's rules.
          </p>
          <ol className="list-decimal space-y-2 pl-5 text-xs text-zinc-300">
            <li>
              <a className={link} href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noreferrer">
                Create a project
              </a>
              , then{" "}
              <a className={link} href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank" rel="noreferrer">
                enable the YouTube Data API v3
              </a>{" "}
              in it.
            </li>
            <li>
              Open{" "}
              <a className={link} href="https://console.cloud.google.com/auth/overview" target="_blank" rel="noreferrer">
                Google Auth Platform
              </a>{" "}
              and tap <b>Get started</b>: any app name, your email, audience <b>External</b>. Then under <b>Audience</b>, tap{" "}
              <b>Publish app</b>; while it's in testing, Google signs channels out after 7 days.
            </li>
            <li>
              Under <b>Clients</b>, create a client of type <b>Web application</b> with this authorized redirect URI:
              <button
                onClick={async () => toast({ text: (await copyText(redirect)) ? "Redirect URI copied" : "Couldn't copy" })}
                className="mt-1.5 flex w-full items-center gap-2 rounded-lg bg-zinc-800 px-3 py-2 text-left font-mono text-[11px] active:bg-zinc-700"
              >
                <span className="min-w-0 flex-1 break-all">{redirect}</span>
                <CopyIcon className="size-3.5 shrink-0 text-violet-300" />
              </button>
            </li>
            <li>
              Put its client ID and secret in the server's <code>.env</code> as <code>CLIPPER_GOOGLE_CLIENT_ID</code> and{" "}
              <code>CLIPPER_GOOGLE_CLIENT_SECRET</code>, then run <code>docker compose up -d</code>.
            </li>
          </ol>
        </>
      )}
      {!HTTPS_OK && (
        <p className="text-xs text-amber-300">
          Google only signs in to https addresses: open the app through its https://….ts.net address to connect channels.
        </p>
      )}
    </div>
  );
}

/** The browser that uploads to channels not connected to the API go through, and whether it's signed in. */
function SignInPanel() {
  return (
    <div className="mt-6 space-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
      <p className="text-sm font-semibold">Upload browser</p>
      <p className="text-xs text-zinc-500">
        Channels that aren't connected to the API upload through YouTube Studio in a separate browser that this app
        controls. Open it and sign in to YouTube there once with the Google account that manages those channels; it
        stays signed in.
      </p>
      <UploadBrowserStatus />
    </div>
  );
}

function DestinationRow(props: { destination: Destination; apiReady: boolean; onChanged: () => void }) {
  const { destination, apiReady, onChanged } = props;
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(destination.name);
  const [url, setUrl] = useState(destination.url);

  const run = async (work: () => Promise<unknown>) => {
    try {
      await work();
      setEditing(false);
      onChanged();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  if (editing) {
    return (
      <div className="space-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
        <input value={name} onChange={(e) => setName(e.target.value)} className={cn(input, "bg-zinc-800 ring-0")} />
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="Channel link" className={cn(input, "bg-zinc-800 ring-0")} />
        <div className="flex gap-2">
          <Button onClick={() => setEditing(false)}>Cancel</Button>
          <Button tone="primary" className="flex-1" onClick={() => run(() => api.updateDestination(destination.id, { name, url }))}>
            Save
          </Button>
        </div>
      </div>
    );
  }
  const connect = async () => {
    try {
      location.href = (await api.connectDestination(destination.id)).url;
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };
  const connected = destination.api_connected;

  return (
    <div className="rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="truncate font-semibold">{destination.name}</p>
          {destination.url ? (
            <a href={destination.url} target="_blank" rel="noreferrer" className="block truncate text-xs text-violet-300">
              {destination.url}
            </a>
          ) : (
            <p className="text-xs text-zinc-500">No link</p>
          )}
          <p className={cn("text-xs", destination.channel_id || connected ? "text-zinc-500" : "text-amber-300")}>
            {destination.channel_id
              ? `Channel ID ${destination.channel_id}`
              : "Channel ID not found: uploads through the browser go to whichever channel it has selected"}
          </p>
        </div>
        <button onClick={() => setEditing(true)} className="rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-semibold active:bg-zinc-700">
          Edit
        </button>
        <button
          onClick={() =>
            confirm(`Remove "${destination.name}"? Shows and clips headed there will need a channel picked again.`) &&
            run(() => api.deleteDestination(destination.id))
          }
          className="rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-semibold text-red-300 active:bg-zinc-700"
        >
          Remove
        </button>
      </div>
      {(apiReady || connected) && (
        <div className="mt-3 flex items-center gap-2 border-t border-white/5 pt-3">
          <span className={cn("size-2 shrink-0 rounded-full", connected && !destination.api_error ? "bg-emerald-400" : "bg-zinc-500")} />
          <span className="min-w-0 flex-1 text-xs text-zinc-400">
            {connected ? `Uploads through the YouTube API as “${destination.api_account}”` : "Uploads through the upload browser"}
          </span>
          {connected && (
            <button
              onClick={() =>
                confirm(`Disconnect "${destination.name}" from the YouTube API? Uploads to it go back through the upload browser.`) &&
                run(() => api.disconnectDestination(destination.id))
              }
              className="rounded-lg px-2 py-1.5 text-xs font-semibold text-zinc-400"
            >
              Disconnect
            </button>
          )}
          {apiReady && (!connected || destination.api_error) && (
            <button onClick={connect} className="rounded-lg bg-violet-600/80 px-3 py-1.5 text-xs font-semibold active:bg-violet-700">
              {connected ? "Reconnect" : "Connect"}
            </button>
          )}
        </div>
      )}
      {destination.api_error && <p className="mt-2 text-xs text-red-300">{destination.api_error}</p>}
    </div>
  );
}

// --- One show: its settings and channels ---

export function ShowEditor({ id }: { id: number | null }) {
  const { data: show, error, refresh } = usePoll(() => (id ? api.show(id) : Promise.resolve(null)), id ? 10000 : 0, id);
  const { data: destinations } = usePoll(api.destinations, 0);
  if (id && !show) return error ? <div className="p-6"><ErrorNote>{error}</ErrorNote></div> : null;
  return (
    <Screen title={show ? show.name : "New show"} onBack={() => go("shows")}>
      {/* Reloads when the links change on the server (adding a channel adds its link). */}
      <ShowForm key={show ? `${show.id}:${show.match}` : "new"} show={show ?? null} destinations={destinations} />
      {show && <Channels show={show} onChanged={refresh} />}
    </Screen>
  );
}

const BLANK: ShowFields = {
  name: "",
  match: "",
  mode: "segment",
  title_template: "",
  episode_template: "",
  notes: "",
  clip_guidance: "",
  clip_destination_id: null,
  episode_destination_id: null,
};

function ShowForm({ show, destinations }: { show: Show | null; destinations: Destination[] | undefined }) {
  const toast = useToast();
  const [fields, setFields] = useState<ShowFields>(() => {
    if (!show) return BLANK;
    const { id: _id, channels: _channels, video_count: _count, ...rest } = show;
    return rest;
  });
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<ShowFields>) => setFields((f) => ({ ...f, ...patch }));

  const defaultPattern = fields.name ? `${fields.name.toUpperCase()} {date} - {title}` : "{title}";
  const clipPattern = fields.title_template || defaultPattern;
  const episodePattern = fields.episode_template || clipPattern;
  const patternsOk =
    (!hasClips(fields.mode) || clipPattern.includes("{title}")) && (!hasEpisode(fields.mode) || episodePattern.includes("{title}"));
  const dirty = !show || (Object.keys(fields) as (keyof ShowFields)[]).some((k) => fields[k] !== show[k]);

  const save = async () => {
    setBusy(true);
    try {
      const body = { ...fields, title_template: clipPattern };
      if (show) {
        await api.updateShow(show.id, body);
        toast({ text: "Saved. New videos use these settings." });
      } else {
        const created = await api.addShow(body);
        toast({ text: "Show added. Now add channels to watch." });
        go(`shows/${created.id}`);
      }
    } catch (e) {
      toast({ text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!show || !confirm(`Delete "${show.name}"? Its channels stop being watched. Videos and clips stay.`)) return;
    try {
      await api.deleteShow(show.id);
      go("shows");
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  return (
    <div className="space-y-5">
      <Field label="Name">
        <input value={fields.name} onChange={(e) => set({ name: e.target.value })} placeholder="Kino Casino" className={input} />
      </Field>

      <Field label="What to make from each video">
        <div className="grid grid-cols-3 gap-1 rounded-xl bg-zinc-800 p-1">
          {(["segment", "whole", "both"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => set({ mode })}
              className={cn("rounded-lg py-2 text-xs font-semibold", fields.mode === mode ? "bg-zinc-600 text-white" : "text-zinc-400")}
            >
              {mode === "both" ? "Both" : MODE_LABEL[mode]}
            </button>
          ))}
        </div>
        <p className="mt-1.5 text-xs text-zinc-500">
          {
            {
              segment: "Claude splits each stream into chapters and highlights.",
              whole: "Each video is posted in full, like a pre-show from X.",
              both: "The full episode, with YouTube chapters in its description, plus Claude's chapters and highlights.",
            }[fields.mode]
          }
        </p>
      </Field>

      {hasEpisode(fields.mode) && (
        <PatternField
          label="Full episode title"
          value={fields.episode_template}
          placeholder={fields.mode === "both" ? "Same as the clip pattern" : defaultPattern}
          effective={episodePattern}
          onChange={(v) => set({ episode_template: v })}
        />
      )}
      {hasClips(fields.mode) && (
        <PatternField
          label="Clip title"
          value={fields.title_template}
          placeholder={defaultPattern}
          effective={clipPattern}
          onChange={(v) => set({ title_template: v })}
        />
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        {hasEpisode(fields.mode) && (
          <Field label="Full episodes go to">
            <DestinationSelect value={fields.episode_destination_id} destinations={destinations} onChange={(v) => set({ episode_destination_id: v })} />
          </Field>
        )}
        {hasClips(fields.mode) && (
          <Field label="Clips go to">
            <DestinationSelect value={fields.clip_destination_id} destinations={destinations} onChange={(v) => set({ clip_destination_id: v })} />
          </Field>
        )}
      </div>
      <button type="button" onClick={() => go("shows/youtube")} className="-mt-2 text-sm font-medium text-violet-300">
        + Add or edit YouTube channels
      </button>

      <Field label="About the show" hint="context for Claude">
        <textarea
          value={fields.notes}
          onChange={(e) => set({ notes: e.target.value })}
          rows={5}
          placeholder="Who hosts it and who the regulars are, how a typical episode runs, running jokes and nicknames, names Claude might mishear…"
          className={input}
        />
      </Field>

      {hasClips(fields.mode) && (
        <Field label="What the chapters and clips should be" hint="Claude follows this over its defaults">
          <textarea
            value={fields.clip_guidance}
            onChange={(e) => set({ clip_guidance: e.target.value })}
            rows={6}
            placeholder={
              "e.g. Chapters follow the segments the hosts announce and run 20–90 minutes; give each of Jer's segments its own chapter.\n" +
              "Highlights: the biggest laughs and blow-ups, 30–90 seconds."
            }
            className={input}
          />
        </Field>
      )}

      <Field label="Links that belong to this show" hint="one per line">
        <textarea
          value={fields.match}
          onChange={(e) => set({ match: e.target.value })}
          rows={3}
          placeholder={"x.com/Andywarski\nkick.com/kinocasino"}
          className={input}
        />
        <p className="mt-1.5 text-xs text-zinc-500">
          A link you paste in Videos belongs to this show if it contains one of these, or comes from a channel with that
          name. Watched channels are added here automatically.
        </p>
      </Field>

      <div className="flex gap-2">
        {show && (
          <Button tone="danger" onClick={remove}>
            Delete
          </Button>
        )}
        <Button tone="primary" onClick={save} disabled={busy || !fields.name.trim() || !patternsOk || !dirty} className="flex-1">
          {busy ? "Saving…" : show ? (dirty ? "Save show" : "Saved") : "Add show"}
        </Button>
      </div>
    </div>
  );
}

function PatternField(props: { label: string; value: string; placeholder: string; effective: string; onChange: (v: string) => void }) {
  const date = titleDate();
  const room = MAX_TITLE - applyTitlePattern(props.effective, "", date).length - 1;
  const ok = props.effective.includes("{title}");
  return (
    <Field label={`${props.label} pattern`} hint="{date} = stream date, {title} = Claude's part">
      <input value={props.value} onChange={(e) => props.onChange(e.target.value)} placeholder={props.placeholder} className={input} />
      <div className="mt-2 rounded-xl bg-zinc-900 p-3 text-sm ring-1 ring-white/5">
        <p className="font-semibold leading-snug">{applyTitlePattern(props.effective, SAMPLE_TITLE, date)}</p>
        <p className={cn("mt-1 text-xs", ok ? "text-zinc-500" : "text-red-300")}>
          {ok ? `Claude gets ${Math.max(20, room)} characters for its part, so titles fit YouTube's 100.` : "Add {title} where Claude's part goes."}
        </p>
      </div>
    </Field>
  );
}

// --- Channels to watch ---

function Channels({ show, onChanged }: { show: Show; onChanged: () => void }) {
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [filters, setFilters] = useState<ChannelFields>({ keywords: "", exclude: "", min_minutes: 0 });
  const [showFilters, setShowFilters] = useState(false);
  const [busy, setBusy] = useState(false);

  const add = async () => {
    setBusy(true);
    try {
      await api.addChannel(show.id, { url: url.trim(), ...filters });
      setUrl("");
      setFilters({ keywords: "", exclude: "", min_minutes: 0 });
      setShowFilters(false);
      toast({ text: "Watching. The first check lists what's already there." });
      onChanged();
    } catch (e) {
      toast({ text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="mt-10">
      <h2 className="text-lg font-bold tracking-tight">Channels to watch</h2>
      <p className="mt-1 text-sm text-zinc-400">
        Checked every 10 minutes. A new stream's VOD is picked up once the stream has ended. Videos already on the channel
        are listed but not processed; tap one to process it.
      </p>

      <div className="mt-3 space-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
        <input
          type="url"
          inputMode="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="kick.com/name, twitch.tv/name or youtube.com/@name"
          className={cn(input, "bg-zinc-800 ring-0")}
        />
        {showFilters ? (
          <FilterFields value={filters} onChange={setFilters} />
        ) : (
          <button type="button" onClick={() => setShowFilters(true)} className="text-sm font-medium text-violet-300">
            + Filters (keywords, minimum length)
          </button>
        )}
        <Button tone="primary" onClick={add} disabled={!url.trim() || busy} className="w-full">
          {busy ? "Adding…" : "Watch channel"}
        </Button>
      </div>

      <div className="mt-3 space-y-3">
        {show.channels.map((c) => (
          <ChannelCard key={c.id} channel={c} onChanged={onChanged} />
        ))}
      </div>
    </section>
  );
}

function FilterFields({ value, onChange }: { value: ChannelFields; onChange: (v: ChannelFields) => void }) {
  return (
    <div className="space-y-3">
      <Field label="Only titles with" hint="comma-separated; empty means every video">
        <input value={value.keywords} onChange={(e) => onChange({ ...value, keywords: e.target.value })} placeholder="kino, casino" className={cn(input, "bg-zinc-800 ring-0")} />
      </Field>
      <Field label="Skip titles with" hint="comma-separated">
        <input value={value.exclude} onChange={(e) => onChange({ ...value, exclude: e.target.value })} placeholder="rerun, test stream" className={cn(input, "bg-zinc-800 ring-0")} />
      </Field>
      <Field label="Minimum length" hint="minutes">
        <input
          type="number"
          inputMode="numeric"
          min={0}
          value={value.min_minutes || ""}
          onChange={(e) => onChange({ ...value, min_minutes: Number(e.target.value) || 0 })}
          placeholder="0"
          className={cn(input, "bg-zinc-800 ring-0")}
        />
      </Field>
    </div>
  );
}

function ChannelCard({ channel, onChanged }: { channel: Channel; onChanged: () => void }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [filters, setFilters] = useState<ChannelFields>({
    keywords: channel.keywords,
    exclude: channel.exclude,
    min_minutes: channel.min_minutes,
  });

  const act = async (work: () => Promise<unknown>, done?: string) => {
    try {
      await work();
      if (done) toast({ text: done });
      onChanged();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  const status = !channel.enabled
    ? "Paused"
    : channel.last_checked_at
      ? `Checked ${ago(channel.last_checked_at)}`
      : "Checking soon…";
  const rules = [
    channel.keywords && `only “${channel.keywords}”`,
    channel.exclude && `not “${channel.exclude}”`,
    channel.min_minutes > 0 && `${channel.min_minutes}+ min`,
  ].filter(Boolean);

  return (
    <div className="rounded-2xl bg-zinc-900 ring-1 ring-white/5">
      <div className="flex items-start gap-3 p-4">
        <div className="min-w-0 flex-1">
          <a href={channel.url} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 font-semibold">
            {PLATFORM_NAME[channel.platform]} · {channelName(channel)}
            <LinkIcon className="size-3.5 text-zinc-500" />
          </a>
          <p className="mt-0.5 text-xs text-zinc-500">
            {status}
            {channel.waiting > 0 && ` · ${channel.waiting} waiting for a stream to end`}
          </p>
          {rules.length > 0 && <p className="mt-0.5 text-xs text-zinc-500">Filters: {rules.join(", ")}</p>}
          {channel.last_error && <p className="mt-1 line-clamp-3 break-words text-xs text-red-300">{channel.last_error}</p>}
        </div>
        <label className="flex shrink-0 items-center gap-2 text-xs text-zinc-400">
          {channel.enabled ? "On" : "Off"}
          <input
            type="checkbox"
            checked={channel.enabled}
            onChange={(e) => act(() => api.updateChannel(channel.id, { enabled: e.target.checked }))}
            className="size-5 accent-violet-500"
          />
        </label>
      </div>

      {editing && (
        <div className="space-y-3 border-t border-white/5 p-4">
          <FilterFields value={filters} onChange={setFilters} />
          <Button
            tone="primary"
            className="w-full"
            onClick={() => act(() => api.updateChannel(channel.id, filters), "Filters saved").then(() => setEditing(false))}
          >
            Save filters
          </Button>
        </div>
      )}

      <div className="flex divide-x divide-white/5 border-t border-white/5 text-sm">
        <CardAction onClick={() => setOpen((o) => !o)}>{open ? "Hide videos" : "Videos"}</CardAction>
        <CardAction onClick={() => act(() => api.checkChannel(channel.id), "Checking now")}>
          <RefreshIcon className="size-4" /> Check
        </CardAction>
        <CardAction onClick={() => setEditing((e) => !e)}>Filters</CardAction>
        <CardAction onClick={() => confirm("Stop watching this channel?") && act(() => api.deleteChannel(channel.id))} danger>
          Remove
        </CardAction>
      </div>

      {open && <ChannelVideos channelId={channel.id} />}
    </div>
  );
}

function CardAction({ onClick, danger, children }: { onClick: () => void; danger?: boolean; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex flex-1 items-center justify-center gap-1.5 py-2.5 font-medium active:bg-white/5",
        danger ? "text-red-300" : "text-zinc-300",
      )}
    >
      {children}
    </button>
  );
}

const ITEM_STATUS: Record<ChannelVideo["status"], [string, string]> = {
  waiting: ["Waiting", "text-amber-300"],
  queued: ["Processing", "text-emerald-300"],
  skipped: ["Skipped", "text-zinc-400"],
  older: ["Older", "text-zinc-500"],
};

function ChannelVideos({ channelId }: { channelId: number }) {
  const toast = useToast();
  const { data: videos, refresh } = usePoll(() => api.channelVideos(channelId), 10000, channelId);

  const process = async (item: ChannelVideo) => {
    try {
      await api.processChannelVideo(item.id);
      toast({ text: "Queued. Follow it under Videos." });
      void refresh();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  if (!videos) return <p className="border-t border-white/5 p-4 text-sm text-zinc-500">Loading…</p>;
  if (videos.length === 0)
    return <p className="border-t border-white/5 p-4 text-sm text-zinc-500">Nothing listed yet. The first check runs within a minute or so.</p>;

  return (
    <ul className="divide-y divide-white/5 border-t border-white/5">
      {videos.map((v) => {
        const [label, tone] = ITEM_STATUS[v.status];
        return (
          <li key={v.id} className="flex items-start gap-3 px-4 py-3">
            <div className="min-w-0 flex-1">
              <a href={v.url} target="_blank" rel="noreferrer" className="line-clamp-2 text-sm leading-snug">
                {v.title || v.url}
              </a>
              <p className="mt-0.5 text-xs text-zinc-500">
                <span className={cn("font-semibold", tone)}>{label}</span>
                {v.duration ? ` · ${length(v.duration)}` : ""}
                {v.reason ? ` · ${v.reason}` : ""}
              </p>
            </div>
            {v.status === "queued" && v.vod_id ? (
              <button onClick={() => go(`videos/${v.vod_id}`)} className="shrink-0 rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-semibold active:bg-zinc-700">
                Open
              </button>
            ) : (
              <button onClick={() => process(v)} className="shrink-0 rounded-lg bg-violet-600/80 px-3 py-1.5 text-xs font-semibold active:bg-violet-700">
                Process
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

const input =
  "w-full rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 placeholder:text-zinc-500 focus:ring-2 focus:ring-violet-500";

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 flex items-baseline justify-between gap-3 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        {label}
        {hint && <span className="text-right font-normal normal-case">{hint}</span>}
      </span>
      {children}
    </label>
  );
}
