import { useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import {
  api,
  errorText,
  type Auto,
  type Box,
  type Channel,
  type ChannelFields,
  type ChannelVideo,
  type Destination,
  type Mode,
  type Show,
  type ShowFields,
  type VideoType,
  type Visibility,
} from "../api";
import { CopyIcon, LinkIcon, RefreshIcon, TvIcon, UploadIcon } from "../icons";
import { ago, applyTitlePattern, clamp, cn, copyText, go, length, pastDay, sitesOf, titleDate, usePoll, type Sites } from "../lib";
import { PlaylistSelect } from "../PlaylistSelect";
import { Button, DestinationSelect, ErrorNote, MODE_LABEL, Screen, Segmented, useToast, VIDEO_TYPE_LABEL } from "../ui";
import { UploadBrowserStatus } from "./UploadKit";

const MAX_TITLE = 100;
const SAMPLE_TITLE = "Short title written by the AI";
export const PLATFORM_NAME: Record<Channel["platform"], string> = {
  kick: "Kick",
  twitch: "Twitch",
  youtube: "YouTube",
  rumble: "Rumble",
  x: "X",
  folder: "Folder",
};

const VIDEO_NOUN: Record<VideoType, string> = { streams: "stream", episodes: "episode", other: "video" };
const VIDEO_TYPE_HINT: Record<VideoType, string> = {
  streams: "Livestreams: the AI looks for segments, topics and the best moments.",
  episodes: "The AI looks for scenes and jokes that work without the rest of the episode, mostly as Shorts. For Shorts only, set the clips offered per episode to 0.",
  other: "Podcasts, interviews, vlogs, talks and anything else: the AI looks for parts that stand on their own and the best moments for Shorts.",
};

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
      wide
    >
      {error && <ErrorNote>{error}</ErrorNote>}
      <button
        onClick={() => go("shows/youtube")}
        className="mb-4 flex w-full items-center gap-3 rounded-2xl bg-zinc-900 p-4 text-left ring-1 ring-white/5 hover:bg-zinc-800/40 active:bg-zinc-800/70"
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
            A show sets the titles, context for the AI and upload channel for its videos, and can watch Kick, Twitch,
            YouTube and Rumble channels for new streams, and X accounts for new videos.
          </p>
        </div>
      )}
      <div className="space-y-3 xl:grid xl:grid-cols-2 xl:items-start xl:gap-3 xl:space-y-0">
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
              className="block w-full rounded-2xl bg-zinc-900 p-4 text-left ring-1 ring-white/5 hover:bg-zinc-800/40 active:bg-zinc-800/70"
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

export const channelName = (c: Channel) => c.url.replace(/^https?:\/\/(www\.)?[^/]+\//, "");

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
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. Night Owls Clips" className={cn(input, "bg-zinc-800 ring-0")} />
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

// Google only sends its sign-in back to an https address on a real domain (not an IP address), or to localhost.
const GOOGLE_OK =
  ["localhost", "127.0.0.1"].includes(location.hostname) ||
  (location.protocol === "https:" && !/^[\d.]+$|^\[/.test(location.hostname));
const APP_ADDRESS = GOOGLE_OK ? location.origin : "https://<server>.<tailnet>.ts.net";
const APP_DOMAIN = GOOGLE_OK ? location.hostname.split(".").slice(1).join(".") : "<tailnet>.ts.net";

/** Uploading through the YouTube API: how to set it up in Google Cloud, or how connecting works once it is. */
function YouTubeApiPanel({ configured, callbackPath }: { configured: boolean; callbackPath: string }) {
  const toast = useToast();
  const redirect = `${APP_ADDRESS}${callbackPath}`;
  const link = "font-medium text-violet-300 underline-offset-2 active:underline";
  return (
    <div className="mb-4 space-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
      <p className="text-sm font-semibold">Upload through the YouTube API</p>
      {!GOOGLE_OK && (
        <p className="rounded-xl bg-amber-500/10 p-3 text-xs text-amber-200 ring-1 ring-amber-500/20">
          Google won't sign in to <b>{location.host}</b>: it needs an https address on a real domain, not an IP address.
          Open the app at its Tailscale address, <code>https://&lt;server&gt;.&lt;tailnet&gt;.ts.net</code> (README, setup
          step 7), and set this up and connect channels from there.
        </p>
      )}
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
              and tap <b>Get started</b>: any app name, your email, audience <b>External</b>. Under <b>Branding</b>, set the
              home page to <code>{APP_ADDRESS}</code>, the privacy policy to <code>{APP_ADDRESS}/privacy.html</code>, and
              add <code>{APP_DOMAIN}</code> to the authorized domains. Then under{" "}
              <b>Audience</b>, tap <b>Publish app</b>; while it's in testing, Google signs channels out after 7 days.
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
            <button
              onClick={connect}
              disabled={!GOOGLE_OK}
              className="rounded-lg bg-violet-600/80 px-3 py-1.5 text-xs font-semibold active:bg-violet-700 disabled:opacity-40"
            >
              {connected ? "Reconnect" : "Connect"}
            </button>
          )}
        </div>
      )}
      {destination.api_error && <p className="mt-2 text-xs text-red-300">{destination.api_error}</p>}
      <RumbleOption destination={destination} onChanged={onChanged} />
    </div>
  );
}

const SITES_LABEL: Record<Sites, string> = { youtube: "YouTube", rumble: "Rumble", both: "Both" };

/** Where a channel's uploads go (YouTube, Rumble or both), and under which of Rumble's categories. */
function RumbleOption({ destination, onChanged }: { destination: Destination; onChanged: () => void }) {
  const toast = useToast();
  const { data: rumble } = usePoll(api.rumble, 0);
  const save = async (fields: { youtube?: boolean; rumble?: boolean; rumble_category?: string }) => {
    try {
      await api.updateDestination(destination.id, { name: destination.name, url: destination.url, ...fields });
      onChanged();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };
  return (
    <div className="mt-3 space-y-2 border-t border-white/5 pt-3">
      <p className="text-xs text-zinc-500">Uploads go to</p>
      <Segmented
        options={SITES_LABEL}
        value={sitesOf(destination)}
        onChange={(v) => save({ youtube: v !== "rumble", rumble: v !== "youtube" })}
      />
      {destination.rumble && (
        <>
          <select
            value={destination.rumble_category}
            onChange={(e) => save({ rumble_category: e.target.value })}
            className={cn(input, "bg-zinc-800 ring-0")}
            aria-label="Rumble category"
          >
            {(rumble?.categories ?? [destination.rumble_category]).map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
          <p className="text-xs text-zinc-500">
            Rumble category for its uploads. They go to the channel of the Rumble account the upload browser is signed in
            to, under Rumble's “{rumble?.licence ?? "Rumble Only"}” licence (non-exclusive).
          </p>
        </>
      )}
      <p className="text-xs text-zinc-500">
        Shows post there unless they pick otherwise for their clips or full episodes. On a clip's Upload page it's what's
        picked to start with, and you can pick another for that upload.
      </p>
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
      <ShowForm
        key={show ? `${show.id}:${show.match}` : "new"}
        show={show ?? null}
        destinations={destinations}
        onChanged={refresh}
      />
      {show && <Channels show={show} onChanged={refresh} />}
    </Screen>
  );
}

export const BLANK_SHOW: ShowFields = {
  name: "",
  match: "",
  mode: "segment",
  video_type: "streams",
  title_template: "",
  episode_template: "",
  notes: "",
  clip_guidance: "",
  clip_destination_id: null,
  episode_destination_id: null,
  clip_playlist_id: "",
  episode_playlist_id: "",
  clip_sites: "",
  episode_sites: "",
  max_chapters: 5,
  max_shorts: 5,
  auto_episode: "review",
  auto_chapters: "review",
  auto_shorts: "review",
  auto_upload_score: 0,
  auto_visibility: "public",
  upload_gap_hours: 0,
  chapter_thumbnails: false,
  episode_thumbnails: false,
  thumbnail_guidance: "",
  learning_notes: "",
  save_transcripts: false,
  breakdown: "",
  short_video_top: null,
  short_video_width: 1,
  short_layout: "",
  short_webcams: [],
  short_game: null,
  overlay_episodes: false,
  overlay_x: null,
  overlay_y: null,
  overlay_scale: 1,
};

function ShowForm(props: { show: Show | null; destinations: Destination[] | undefined; onChanged: () => void }) {
  const { show, destinations } = props;
  const toast = useToast();
  const [fields, setFields] = useState<ShowFields>(() => {
    if (!show) return BLANK_SHOW;
    // The images are saved on their own, straight away, so they aren't part of the form.
    const { id: _i, channels: _c, video_count: _v, short_background: _b, overlay: _o, overlay_box: _x, overlay_screen: _s, ...rest } =
      show;
    return rest;
  });
  const [busy, setBusy] = useState(false);
  const { data: thumbnailSetup } = usePoll(api.thumbnails, 0);
  const set = (patch: Partial<ShowFields>) => setFields((f) => ({ ...f, ...patch }));

  const defaultPattern = fields.name ? `${fields.name.toUpperCase()} {date} - {title}` : "{title}";
  const clipPattern = fields.title_template || defaultPattern;
  const episodePattern = fields.episode_template || clipPattern;
  const patternsOk =
    (!hasClips(fields.mode) || clipPattern.includes("{title}")) && (!hasEpisode(fields.mode) || episodePattern.includes("{title}"));
  const shortsOk = fields.short_layout !== "split" || fields.short_webcams.length > 0;
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
        <input value={fields.name} onChange={(e) => set({ name: e.target.value })} placeholder="Night Owls" className={input} />
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
              segment: "The AI splits each stream into clips and highlights.",
              whole: "Each video is posted in full, like a pre-show from X.",
              both: "The full episode, with YouTube chapters in its description, plus the AI's clips and highlights.",
            }[fields.mode]
          }
        </p>
      </Field>

      {hasClips(fields.mode) && (
        <Field label="Its videos are">
          <div className="flex">
            <Segmented options={VIDEO_TYPE_LABEL} value={fields.video_type} onChange={(video_type) => set({ video_type })} />
          </div>
          <p className="mt-1.5 text-xs text-zinc-500">{VIDEO_TYPE_HINT[fields.video_type]}</p>
        </Field>
      )}

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
            <DestinationSelect
              value={fields.episode_destination_id}
              destinations={destinations}
              onChange={(v) => set({ episode_destination_id: v, episode_playlist_id: "" })}
            />
            <PlaylistSelect
              destination={destinations?.find((d) => d.id === fields.episode_destination_id)}
              value={fields.episode_playlist_id}
              onChange={(episode_playlist_id) => set({ episode_playlist_id })}
            />
            <SitesChoice
              channel={destinations?.find((d) => d.id === fields.episode_destination_id)}
              value={fields.episode_sites}
              onChange={(episode_sites) => set({ episode_sites })}
              what="full episodes"
            />
          </Field>
        )}
        {hasClips(fields.mode) && (
          <Field label="Clips go to">
            <DestinationSelect
              value={fields.clip_destination_id}
              destinations={destinations}
              onChange={(v) => set({ clip_destination_id: v, clip_playlist_id: "" })}
            />
            <PlaylistSelect
              destination={destinations?.find((d) => d.id === fields.clip_destination_id)}
              value={fields.clip_playlist_id}
              onChange={(clip_playlist_id) => set({ clip_playlist_id })}
            />
            {fields.clip_playlist_id && <p className="mt-2 text-xs text-zinc-500">Shorts never go in a playlist.</p>}
            <SitesChoice
              channel={destinations?.find((d) => d.id === fields.clip_destination_id)}
              value={fields.clip_sites}
              onChange={(clip_sites) => set({ clip_sites })}
              what="clips"
            />
          </Field>
        )}
      </div>
      <button type="button" onClick={() => go("shows/youtube")} className="-mt-2 text-sm font-medium text-violet-300">
        + Add or edit YouTube channels
      </button>

      {hasClips(fields.mode) && (
        <Group label={`Offered per ${VIDEO_NOUN[fields.video_type]}`} hint="best first, by the AI's score">
          <div className="grid grid-cols-2 gap-2">
            <CountBox label="Clips" value={fields.max_chapters} onChange={(max_chapters) => set({ max_chapters })} />
            <CountBox label="Shorts" value={fields.max_shorts} onChange={(max_shorts) => set({ max_shorts })} />
          </div>
          <p className="mt-1.5 text-xs text-zinc-500">
            {fields.max_chapters + fields.max_shorts} videos from each stream
            {hasEpisode(fields.mode) ? ", plus the full episode" : ""}. The AI's other picks wait on the video's page as
            extras. Raise these as your YouTube upload limit grows.
          </p>
        </Group>
      )}

      <Group label="Without review">
        <div className="space-y-2">
          {hasEpisode(fields.mode) && (
            <AutoChoice label="Full episode" value={fields.auto_episode} onChange={(auto_episode) => set({ auto_episode })} />
          )}
          {hasClips(fields.mode) && (
            <>
              <AutoChoice label="Clips" value={fields.auto_chapters} onChange={(auto_chapters) => set({ auto_chapters })} />
              <AutoChoice label="Shorts" value={fields.auto_shorts} onChange={(auto_shorts) => set({ auto_shorts })} />
            </>
          )}
        </div>
        <p className="mt-1.5 text-xs text-zinc-500">
          Review: waits in your inbox. Render: skips the inbox and waits in Ready to upload. Upload: goes straight to its
          YouTube channel (a highlight as its Short, if it has one).
        </p>
        {hasClips(fields.mode) && (
          <>
            <div className="mt-3 flex items-center gap-3">
              <span className="w-24 shrink-0 text-sm text-zinc-300">Standouts</span>
              <Segmented
                options={SCORE_LABEL}
                value={String(fields.auto_upload_score)}
                onChange={(v) => set({ auto_upload_score: Number(v) })}
              />
            </div>
            <p className="mt-1.5 text-xs text-zinc-500">
              {fields.auto_upload_score
                ? `Clips and Shorts the AI scores ${fields.auto_upload_score} or more upload straight away, even if set to Review above. `
                : "Upload clips and Shorts straight away when the AI scores them this high, even if set to Review above. "}
              Only the ones offered per stream count. The AI keeps 0.9 and up for standout moments.
            </p>
          </>
        )}
        {autoUploads(fields) && (
          <div className="mt-3 flex items-center gap-3">
            <span className="w-24 shrink-0 text-sm text-zinc-300">Uploaded as</span>
            <Segmented
              options={VISIBILITY_LABEL}
              value={fields.auto_visibility}
              onChange={(auto_visibility) => set({ auto_visibility })}
            />
          </div>
        )}
      </Group>

      {hasClips(fields.mode) && (
        <Group label="Space clips out" hint="per YouTube channel">
          <Segmented
            options={GAP_LABEL}
            value={String(fields.upload_gap_hours)}
            onChange={(v) => set({ upload_gap_hours: Number(v) })}
          />
          <p className="mt-1.5 text-xs text-zinc-500">
            {fields.upload_gap_hours
              ? `Each clip or Short goes up at least ${fields.upload_gap_hours} hour${fields.upload_gap_hours === 1 ? "" : "s"} after the last clip on its channel, whether it uploads by itself or you tap Upload. Full episodes go straight away.`
              : "Clips upload as soon as they're ready. Space them out so a stream's clips don't all land at once."}
          </p>
        </Group>
      )}

      <Group label="Thumbnails" hint="drawn with AI">
        <p className="mb-2 text-sm text-zinc-300">Make one for each, once it's rendered:</p>
        <div className="space-y-2">
          {hasClips(fields.mode) && (
            <Tick
              label="Clip"
              checked={fields.chapter_thumbnails}
              onChange={(chapter_thumbnails) => set({ chapter_thumbnails })}
            />
          )}
          {hasEpisode(fields.mode) && (
            <Tick
              label="Full episode"
              checked={fields.episode_thumbnails}
              onChange={(episode_thumbnails) => set({ episode_thumbnails })}
            />
          )}
        </div>
        <p className="mt-1.5 text-xs text-zinc-500">
          The AI comes up with a funny idea about what happens in the video, starring the people in it and the people it's
          about, and an image model on OpenRouter draws it from frames of the video and photos it looks up. Each costs a few
          cents. You can also make one for any video on its Upload page. Say what the regulars look like under About the
          show.
        </p>
        {thumbnailSetup && !thumbnailSetup.configured && (
          <p className="mt-1.5 text-xs text-amber-300">Needs CLIPPER_OPENROUTER_API_KEY in .env (see the README).</p>
        )}
        <label className="mt-3 block">
          <span className="mb-1 block text-xs text-zinc-500">What every thumbnail must have (the AI follows this over its defaults)</span>
          <textarea
            value={fields.thumbnail_guidance}
            onChange={(e) => set({ thumbnail_guidance: e.target.value })}
            rows={3}
            maxLength={2000}
            placeholder={'e.g. Must have the words "Night Owls Live" in a banner along the bottom. Bold, neon colours.'}
            className={input}
          />
        </label>
        <p className="mt-1.5 text-xs text-zinc-500">
          For thumbnails made from now on. One you draw again from its prompt keeps the prompt it had; Make another picks
          this up.
        </p>
      </Group>

      {hasClips(fields.mode) && (
        <ShortsLayout
          show={show}
          layout={fields.short_layout || "blur"}
          webcams={fields.short_webcams}
          game={fields.short_game}
          top={fields.short_video_top}
          width={fields.short_video_width}
          onChange={set}
          onChanged={props.onChanged}
        />
      )}
      <ClipOverlay
        show={show}
        episodes={hasEpisode(fields.mode) ? fields.overlay_episodes : null}
        place={{ x: fields.overlay_x, y: fields.overlay_y, scale: fields.overlay_scale }}
        onChange={set}
        onChanged={props.onChanged}
      />

      <Field label="About the show" hint="context for the AI">
        <textarea
          value={fields.notes}
          onChange={(e) => set({ notes: e.target.value })}
          rows={5}
          placeholder="Who hosts it and who the regulars are, how a typical episode runs, running jokes and nicknames, names the AI might mishear…"
          className={input}
        />
      </Field>

      {hasClips(fields.mode) && (
        <Field label="What the clips and highlights should be" hint="the AI follows this over its defaults">
          <textarea
            value={fields.clip_guidance}
            onChange={(e) => set({ clip_guidance: e.target.value })}
            rows={6}
            placeholder={
              "e.g. Clips follow the segments the hosts announce and run 20–90 minutes; give each guest their own clip.\n" +
              "Highlights: the biggest laughs and blow-ups, 30–90 seconds."
            }
            className={input}
          />
        </Field>
      )}

      {hasClips(fields.mode) && (
        <Field label="What to learn from past clips" hint="the AI follows this over the numbers">
          <textarea
            value={fields.learning_notes}
            onChange={(e) => set({ learning_notes: e.target.value })}
            rows={4}
            maxLength={4000}
            placeholder={
              "e.g. Clips that start right on the topic do better than ones with a lead-in.\n" +
              "Viewers stay for the long debates, so keep those whole."
            }
            className={input}
          />
          <p className="mt-1.5 text-xs text-zinc-500">
            With each new stream, the AI also gets how this show's past uploads did, and how you changed or turned down
            its picks. Stats shows what it's given.
          </p>
        </Field>
      )}

      {hasClips(fields.mode) && (
        <Group label="Transcripts" hint="for use outside the app">
          <Tick
            label="Save each stream's transcript as files"
            checked={fields.save_transcripts}
            onChange={(save_transcripts) => set({ save_transcripts })}
          />
          <p className="mt-1.5 text-xs text-zinc-500">
            Once the AI has been through a stream, its transcript is saved in the data folder under transcripts/, in a
            folder for the show: as .json with the stream's details and when each line was said, and as .txt to read. The
            save-transcripts command saves past streams' too.
          </p>
          {fields.save_transcripts && (
            <label className="mt-3 block">
              <span className="mb-1 block text-xs text-zinc-500">
                What each stream's time goes on (optional): one category per line, a name, a colon and what it covers
              </span>
              <textarea
                value={fields.breakdown}
                onChange={(e) => set({ breakdown: e.target.value })}
                rows={4}
                maxLength={4000}
                placeholder={
                  "e.g. Gameplay: playing a game\nNews: going through headlines and other people's stories\nSponsors: paid ad reads"
                }
                className={input}
              />
              <span className="mt-1.5 block text-xs text-zinc-500">
                The AI splits each stream into these, with anything else as Other, in the same request as picking the clips,
                so it costs little extra. The parts are saved in the .json, for working out what share of each stream goes
                on what. For streams from now on.
              </span>
            </label>
          )}
        </Group>
      )}

      <Field label="Links that belong to this show" hint="one per line">
        <textarea
          value={fields.match}
          onChange={(e) => set({ match: e.target.value })}
          rows={3}
          placeholder={"x.com/yourshow\nkick.com/yourshow"}
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
        <Button tone="primary" onClick={save} disabled={busy || !fields.name.trim() || !patternsOk || !shortsOk || !dirty} className="flex-1">
          {busy ? "Saving…" : show ? (dirty ? "Save show" : "Saved") : "Add show"}
        </Button>
      </div>
    </div>
  );
}

const AUTO_LABEL: Record<Auto, string> = { review: "Review", render: "Render", upload: "Upload" };
const VISIBILITY_LABEL: Record<Visibility, string> = { public: "Public", unlisted: "Unlisted", private: "Private" };
// The AI's score (0–1) from which a clip or Short uploads without review.
const SCORE_LABEL: Record<string, string> = { "0": "Off", "0.8": "0.8+", "0.85": "0.85+", "0.9": "0.9+", "0.95": "0.95+" };
// Hours between a channel's clip uploads.
const GAP_LABEL: Record<string, string> = { "0": "Off", "1": "1h", "2": "2h", "3": "3h", "6": "6h", "12": "12h" };

/** Whether any video the show makes is uploaded without review. */
const autoUploads = (f: ShowFields) =>
  (hasEpisode(f.mode) && f.auto_episode === "upload") ||
  (hasClips(f.mode) && (f.auto_chapters === "upload" || f.auto_shorts === "upload" || f.auto_upload_score > 0));

/**
 * After a show's channel: whether its uploads there go to YouTube, Rumble or both. Until it's picked for the show,
 * it goes by the channel's own setting (under YouTube channels), shown as what's picked.
 */
function SitesChoice(props: { channel?: Destination; value: Sites | ""; onChange: (v: Sites | "") => void; what: string }) {
  const { channel, value } = props;
  if (!channel) return null;
  const sites = value || sitesOf(channel);
  return (
    <div className="mt-2 space-y-1.5">
      <span className="block text-xs text-zinc-500">Post its {props.what} on</span>
      <Segmented options={SITES_LABEL} value={sites} onChange={props.onChange} />
      <p className="text-xs text-zinc-500">
        {value ? (
          <>
            Picked for this show.{" "}
            <button type="button" onClick={() => props.onChange("")} className="font-semibold text-violet-300">
              Go by the channel's setting
            </button>
          </>
        ) : (
          "As the channel is set. Pick one to choose for this show."
        )}
      </p>
      {sites !== "youtube" && (
        <p className="text-xs text-zinc-500">
          On Rumble, they go to the account the upload browser is signed in to, in the channel's category ({channel.rumble_category}).
        </p>
      )}
    </div>
  );
}

function Tick(props: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="flex items-center gap-3">
      <input
        type="checkbox"
        checked={props.checked}
        onChange={(e) => props.onChange(e.target.checked)}
        className="size-5 shrink-0 accent-violet-500"
      />
      <span className="text-sm text-zinc-300">{props.label}</span>
    </label>
  );
}

/**
 * Where a show's Shorts put the video: over the show's background image, or over a blurred copy of the stream.
 * The preview shows the Short at a small size, with the captions where they'll go, under the video.
 */
function ShortsLayout(props: {
  show: Show | null;
  layout: "crop" | "blur" | "split";
  webcams: Box[];
  game: Box | null;
  top: number | null;
  width: number;
  onChange: (fields: Partial<ShowFields>) => void;
  onChanged: () => void;
}) {
  const { show, top, width } = props;
  const crop = props.layout === "crop";
  const height = (width * 1080 * 9) / 16 / 1920; // a 16:9 video, as a fraction of the Short's height
  const videoTop = top ?? (1 - height) / 2;
  const background = show?.short_background;

  if (props.layout === "split") {
    return (
      <Group label="Shorts" hint="the vertical videos made from highlights">
        <ShortsChoice layout={props.layout} onChange={props.onChange} />
        <SplitLayout show={show} webcams={props.webcams} game={props.game} onChange={props.onChange} />
      </Group>
    );
  }
  if (crop) {
    return (
      <Group label="Shorts" hint="the vertical videos made from highlights">
        <ShortsChoice layout={props.layout} onChange={props.onChange} />
        <div className="mt-3 flex gap-4">
          <div className="relative grid aspect-[9/16] w-36 shrink-0 place-items-center overflow-hidden rounded-xl bg-violet-500/60 text-[10px] font-semibold ring-1 ring-white/10">
            Middle of the video
            {/* in the lower third, as render.render_short puts them over a cropped Short */}
            <p className="absolute inset-x-0 top-[70%] text-center text-[9px] font-extrabold text-white [text-shadow:0_0_2px_black,0_0_2px_black]">
              <span className="text-yellow-300">YOUR</span> CAPTIONS
            </p>
          </div>
          <p className="min-w-0 flex-1 text-xs text-zinc-500">
            The video fills the Short: the middle of the picture, cut down to 9:16, so whatever's at the sides is left out.
            Best when what matters is in the middle, such as one person talking or a cartoon's characters. The captions go
            in the lower third.
          </p>
        </div>
      </Group>
    );
  }

  return (
    <Group label="Shorts" hint="the vertical videos made from highlights">
      <ShortsChoice layout={props.layout} onChange={props.onChange} />
      <div className="mt-3 flex gap-4">
        <div
          className="relative aspect-[9/16] w-36 shrink-0 overflow-hidden rounded-xl bg-gradient-to-b from-zinc-600 to-zinc-800 bg-cover bg-center ring-1 ring-white/10"
          style={background ? { backgroundImage: `url(${background})` } : undefined}
        >
          <div
            className="absolute left-1/2 grid -translate-x-1/2 place-items-center bg-violet-500/60 text-[10px] font-semibold ring-1 ring-white/50"
            style={{ top: `${videoTop * 100}%`, width: `${width * 100}%`, height: `${height * 100}%` }}
          >
            Video
          </div>
          <p
            className="absolute inset-x-0 text-center text-[9px] font-extrabold text-white [text-shadow:0_0_2px_black,0_0_2px_black]"
            style={{ top: `${(videoTop + height) * 100 + 1.9}%` }}
          >
            <span className="text-yellow-300">YOUR</span> CAPTIONS
          </p>
        </div>
        <div className="min-w-0 flex-1 space-y-4">
          <label className="block text-sm text-zinc-300">
            <span className="flex justify-between">
              Video position
              <span className="text-xs text-zinc-500">{top === null ? "centred" : `${Math.round(top * 100)}% down`}</span>
            </span>
            <input
              type="range"
              min={0}
              max={Math.floor((1 - height) * 200) / 2}
              step={0.5}
              value={videoTop * 100}
              onChange={(e) => props.onChange({ short_video_top: Number(e.target.value) / 100 })}
              className="mt-1 w-full accent-violet-500"
            />
          </label>
          {top !== null && (
            <button type="button" onClick={() => props.onChange({ short_video_top: null })} className="-mt-2 text-xs font-semibold text-violet-300">
              Centre it
            </button>
          )}
          <label className="block text-sm text-zinc-300">
            <span className="flex justify-between">
              Video size <span className="text-xs text-zinc-500">{Math.round(width * 100)}% wide</span>
            </span>
            <input
              type="range"
              min={40}
              max={100}
              value={Math.round(width * 100)}
              onChange={(e) => {
                const short_video_width = Number(e.target.value) / 100;
                const fits = 1 - (short_video_width * 1080 * 9) / 16 / 1920;
                props.onChange({ short_video_width, ...(top !== null && top > fits ? { short_video_top: fits } : {}) });
              }}
              className="mt-1 w-full accent-violet-500"
            />
          </label>
          <ImageButtons
            show={show}
            what="background"
            has={!!background}
            accept="image/png,image/jpeg,image/webp"
            upload={api.uploadShortBackground}
            remove={api.deleteShortBackground}
            onChanged={props.onChanged}
          />
        </div>
      </div>
      <p className="mt-2 text-xs text-zinc-500">
        {background
          ? "The video sits on this image. "
          : "Without a background image, the video sits on a dimmed, blurred copy of itself. "}
        Backgrounds are 1080×1920 (9:16): keep the top ~30% for your header, and the middle clear for the video and its
        captions. YouTube's own buttons and title cover the bottom and right edge. Position and size are saved with the
        show.
      </p>
    </Group>
  );
}

function ShortsChoice({ layout, onChange }: { layout: "crop" | "blur" | "split"; onChange: (fields: Partial<ShowFields>) => void }) {
  return (
    <div className="flex">
      <Segmented
        options={{ crop: "Cropped to fill", blur: "On a background", split: "Webcam on top" }}
        value={layout}
        onChange={(short_layout) => onChange({ short_layout })}
      />
    </div>
  );
}

/**
 * How a split Short's webcams sit across its top, as the server works it out (render.webcam_grid): side by side, or two
 * by two for four, each `width` x `height` of the Short's 1080x1920.
 */
function webcamGrid(webcams: Box[]): { columns: number; rows: number; width: number; height: number } {
  const cams: Box[] = webcams.length ? webcams : [[0, 0, 0.25, 0.25]];
  const columns = cams.length === 4 ? 2 : cams.length;
  const rows = Math.ceil(cams.length / columns);
  const width = Math.floor(1080 / columns);
  const tall = (rows * cams.reduce((sum, [, , w, h]) => sum + (width * h * 9) / (w * 16), 0)) / cams.length;
  return { columns, rows, width, height: Math.round(Math.min(960, Math.max(480, tall)) / rows / 2) * 2 };
}

const MAX_WEBCAMS = 4; // as the server allows
const WHOLE: Box = [0, 0, 1, 1];

/**
 * A split Short: the webcams across the top (one for each person on a show with several) and the game under them. Each
 * is marked by dragging over a still from the show's newest video; the game is all of the picture until a part of it
 * is marked.
 */
function SplitLayout(props: { show: Show | null; webcams: Box[]; game: Box | null; onChange: (fields: Partial<ShowFields>) => void }) {
  const { show, webcams, game } = props;
  // What a drag marks: a webcam by its number from 0 (one past the last adds one), or "game".
  const [marking, setMarking] = useState("0");
  const [moment, setMoment] = useState(0); // which still: another one when this one doesn't show the webcam
  const [missing, setMissing] = useState(false);
  const picture = useRef<HTMLDivElement>(null);
  const from = useRef<{ x: number; y: number } | null>(null);
  if (!show) return <p className="mt-3 text-xs text-zinc-500">Add the show first, then mark where the webcam is on one of its videos.</p>;

  const still = `/media/shows/${show.id}/still.jpg?n=${moment}`;
  const camera = marking === "game" ? -1 : Number(marking);
  const adding = camera >= webcams.length; // a new one, not drawn yet
  const count = Math.max(1, webcams.length + (adding ? 1 : 0));
  const name = (i: number) => (count === 1 ? "Webcam" : `Cam ${i + 1}`); // short enough for five tabs on a phone
  const options: Record<string, string> = {};
  for (let i = 0; i < count; i++) options[String(i)] = name(i);
  options.game = "Game";
  const grid = webcamGrid(webcams);
  const share = (grid.rows * grid.height) / 1920;
  const point = (e: ReactPointerEvent) => {
    const area = picture.current!.getBoundingClientRect();
    return { x: clamp((e.clientX - area.left) / area.width, 0, 1), y: clamp((e.clientY - area.top) / area.height, 0, 1) };
  };
  const round = (v: number) => Math.round(v * 10000) / 10000;
  const mark = (box: Box) => {
    if (camera < 0) return props.onChange({ short_game: box });
    const next = [...webcams];
    next[camera] = box;
    props.onChange({ short_webcams: next });
  };

  return (
    <div className="mt-3 space-y-3">
      <div className="flex items-center gap-3">
        <span className="shrink-0 text-sm text-zinc-300">Mark the</span>
        <Segmented options={options} value={marking} onChange={setMarking} />
      </div>
      <div
        ref={picture}
        className="relative aspect-video w-full max-w-md cursor-crosshair touch-none select-none overflow-hidden rounded-xl bg-black ring-1 ring-white/10"
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          from.current = point(e);
        }}
        onPointerMove={(e) => {
          const start = from.current;
          if (!start) return;
          const to = point(e);
          const box: Box = [round(Math.min(start.x, to.x)), round(Math.min(start.y, to.y)), round(Math.abs(to.x - start.x)), round(Math.abs(to.y - start.y))];
          if (box[2] >= 0.02 && box[3] >= 0.02) mark(box);
        }}
        onPointerUp={() => (from.current = null)}
        onPointerCancel={() => (from.current = null)}
      >
        {missing ? (
          <p className="absolute inset-0 grid place-items-center p-4 text-center text-xs text-zinc-400">
            None of the show's videos has its preview yet. Add one in Videos, then mark the webcam on it.
          </p>
        ) : (
          <img src={still} alt="" draggable={false} onError={() => setMissing(true)} className="pointer-events-none size-full" />
        )}
        {game && <Marked box={game} label="Game" className={cn("outline-sky-300", camera < 0 ? "bg-sky-400/30" : "bg-sky-400/10")} />}
        {webcams.map((box, i) => (
          <Marked key={i} box={box} label={name(i)} className={cn("outline-violet-300", camera === i ? "bg-violet-400/35" : "bg-violet-400/10")} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs font-semibold text-violet-300">
        <button type="button" onClick={() => { setMissing(false); setMoment((m) => m + 1); }}>
          Another frame
        </button>
        {webcams.length > 0 && webcams.length < MAX_WEBCAMS && !adding && (
          <button type="button" onClick={() => setMarking(String(webcams.length))}>
            + Add a webcam
          </button>
        )}
        {camera >= 0 && camera < webcams.length && (
          <button
            type="button"
            onClick={() => {
              props.onChange({ short_webcams: webcams.filter((_, i) => i !== camera) });
              setMarking("0");
            }}
            className="text-red-300"
          >
            Remove {count === 1 ? "webcam" : `cam ${camera + 1}`}
          </button>
        )}
        {game && (
          <button type="button" onClick={() => props.onChange({ short_game: null })}>
            Game: all of the picture
          </button>
        )}
      </div>
      {adding && webcams.length > 0 && <p className="text-xs text-amber-300">Drag over {name(camera)} on the picture.</p>}

      <div className="flex gap-4">
        <div className="relative flex aspect-[9/16] w-28 shrink-0 flex-col overflow-hidden rounded-xl bg-zinc-800 ring-1 ring-white/10">
          <div
            className="grid shrink-0 bg-violet-500/40"
            style={{ height: `${share * 100}%`, gridTemplateColumns: `repeat(${grid.columns}, 1fr)`, gridTemplateRows: `repeat(${grid.rows}, 1fr)` }}
          >
            {webcams.map((box, i) => (
              <div key={i} className="relative overflow-hidden">
                {!missing && <PartOf src={still} box={box} aspect={grid.width / grid.height} />}
                {webcams.length > 1 && (
                  <span className="absolute left-0.5 top-0.5 rounded bg-black/60 px-0.5 text-[7px] font-bold text-white">{i + 1}</span>
                )}
              </div>
            ))}
          </div>
          <div className="relative min-h-0 flex-1 overflow-hidden bg-sky-500/30">
            {!missing && <PartOf src={still} box={game ?? WHOLE} aspect={1080 / ((1 - share) * 1920)} />}
          </div>
          {/* just under where they meet, as render.render_short puts them on a split Short */}
          <p
            className="absolute inset-x-0 text-center text-[8px] font-extrabold text-white [text-shadow:0_0_2px_black,0_0_2px_black]"
            style={{ top: `${((share * 1920 + 250) / 1920) * 100}%` }}
          >
            <span className="text-yellow-300">YOUR</span> CAPTIONS
          </p>
        </div>
        <p className="min-w-0 flex-1 text-xs text-zinc-500">
          {webcams.length === 0 ? (
            <span className="text-amber-300">Drag over the webcam on the picture to mark it.</span>
          ) : webcams.length === 1 ? (
            <>
              The webcam goes across the top, as tall as its shape makes it (between a quarter and half of the Short).
              For a show with more than one person on camera, add a webcam for each, up to four.
            </>
          ) : (
            <>
              The webcams go across the top in the order they're numbered{webcams.length === 4 ? ", two by two" : ", side by side"},
              each cropped to fill its place (keep the faces in the middle of what you mark).
            </>
          )}{" "}
          The {game ? "part marked as the game" : "middle of the picture"} fills the rest, with the captions just under
          where they meet. The same spots are used for every Short of the show, so check them if the stream's layout
          changes.
        </p>
      </div>
    </div>
  );
}

/** A marked part of the still, outlined. */
function Marked({ box: [x, y, w, h], label, className }: { box: Box; label: string; className: string }) {
  return (
    <div
      className={cn("pointer-events-none absolute outline-2 outline-dashed", className)}
      style={{ left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` }}
    >
      <span className="absolute left-1 top-1 whitespace-nowrap rounded bg-black/70 px-1 text-[10px] font-semibold text-white">{label}</span>
    </div>
  );
}

/** A part of the still filling a box of this shape (width ÷ height), cropped rather than stretched, as the render does. */
function PartOf({ src, box: [x, y, w, h], aspect }: { src: string; box: Box; aspect: number }) {
  const width = Math.max(1 / w, 16 / (9 * h * aspect)); // the still's width, in widths of the box
  const left = 0.5 - (x + w / 2) * width;
  const top = 0.5 - (y + h / 2) * width * (9 / 16) * aspect; // in heights of the box
  return (
    <img
      src={src}
      alt=""
      draggable={false}
      className="pointer-events-none absolute max-w-none"
      style={{ width: `${width * 100}%`, left: `${left * 100}%`, top: `${top * 100}%` }}
    />
  );
}

/**
 * The show's branding on its landscape videos. A frame (its design goes all the way round a transparent screen) has
 * the clip shrunk into its screen; anything else, like a corner logo, goes on top of the full-size clip.
 */
function ClipOverlay(props: {
  show: Show | null;
  episodes: boolean | null; // the full-episode switch, when the show makes full episodes
  place: { x: number | null; y: number | null; scale: number }; // where a logo goes (the form's values)
  onChange: (fields: Partial<ShowFields>) => void;
  onChanged: () => void;
}) {
  const { show, place } = props;
  const overlay = show?.overlay;
  const screen = show?.overlay_screen;
  const box = !screen ? show?.overlay_box : null; // a logo's visible part, which can be moved and resized
  const picture = useRef<HTMLDivElement>(null);
  const drag = useRef<{ px: number; py: number; left: number; top: number } | null>(null);
  const clip = screen
    ? { left: `${(screen[0] / 1920) * 100}%`, top: `${(screen[1] / 1080) * 100}%`, width: `${(screen[2] / 1920) * 100}%`, height: `${(screen[3] / 1080) * 100}%` }
    : { inset: 0 };

  // The logo in the 1920×1080 picture, kept inside it the way the server keeps it.
  const logo = box && (() => {
    const w = Math.min(1920, box[2] * place.scale);
    const h = Math.min(1080, box[3] * place.scale);
    const left = clamp(place.x === null ? box[0] : place.x * 1920, 0, 1920 - w);
    const top = clamp(place.y === null ? box[1] : place.y * 1080, 0, 1080 - h);
    return { w, h, left, top };
  })();
  const maxScale = box ? Math.min(1920 / box[2], 1080 / box[3], 3) : 1;
  const resize = (scale: number) => {
    if (!box || !logo) return;
    const w = box[2] * scale, h = box[3] * scale; // keep it centred where it is
    props.onChange({
      overlay_scale: scale,
      overlay_x: clamp(logo.left + logo.w / 2 - w / 2, 0, 1920 - w) / 1920,
      overlay_y: clamp(logo.top + logo.h / 2 - h / 2, 0, 1080 - h) / 1080,
    });
  };

  return (
    <Group label="Overlay on clips" hint="the creator's branding on the landscape videos">
      <div ref={picture} className="relative aspect-video w-full max-w-sm overflow-hidden rounded-xl bg-black ring-1 ring-white/10">
        <div className="absolute grid place-items-center bg-gradient-to-br from-zinc-600 to-zinc-800 text-xs font-semibold text-zinc-300" style={clip}>
          Clip
        </div>
        {overlay && !logo && <img src={overlay} alt="" className="absolute inset-0 size-full" />}
        {overlay && box && logo && (
          // Only the visible part of the image: drag it to move it.
          <div
            className="absolute cursor-move touch-none overflow-hidden outline-dashed outline-1 outline-white/60"
            style={{ left: `${(logo.left / 1920) * 100}%`, top: `${(logo.top / 1080) * 100}%`, width: `${(logo.w / 1920) * 100}%`, height: `${(logo.h / 1080) * 100}%` }}
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId);
              drag.current = { px: e.clientX, py: e.clientY, left: logo.left, top: logo.top };
            }}
            onPointerMove={(e) => {
              const start = drag.current;
              const area = picture.current?.getBoundingClientRect();
              if (!start || !area) return;
              props.onChange({
                overlay_x: clamp(start.left + ((e.clientX - start.px) * 1920) / area.width, 0, 1920 - logo.w) / 1920,
                overlay_y: clamp(start.top + ((e.clientY - start.py) * 1080) / area.height, 0, 1080 - logo.h) / 1080,
              });
            }}
            onPointerUp={() => (drag.current = null)}
            onPointerCancel={() => (drag.current = null)}
          >
            <img
              src={overlay}
              alt=""
              draggable={false}
              className="pointer-events-none absolute max-w-none"
              style={{ width: `${(1920 / box[2]) * 100}%`, left: `${(-box[0] / box[2]) * 100}%`, top: `${(-box[1] / box[3]) * 100}%` }}
            />
          </div>
        )}
      </div>
      <p className="mt-2 text-xs text-zinc-400">
        {!overlay
          ? "None yet."
          : screen
            ? "A frame: each clip is shrunk to fit its screen, so none of the stream is hidden. It fills the picture, so it stays put."
            : "A logo: it goes on top of the full-size clip. Drag it to move it."}
      </p>
      {overlay && box && (
        <div className="mt-2 flex items-center gap-3">
          <label className="flex min-w-0 flex-1 items-center gap-3 text-sm text-zinc-300">
            Size
            <input
              type="range"
              min={20}
              max={Math.round(maxScale * 100)}
              value={Math.round(place.scale * 100)}
              onChange={(e) => resize(Number(e.target.value) / 100)}
              className="min-w-0 flex-1 accent-violet-500"
            />
            <span className="w-10 text-right text-xs tabular-nums text-zinc-500">{Math.round(place.scale * 100)}%</span>
          </label>
          <button
            type="button"
            onClick={() => props.onChange({ overlay_x: null, overlay_y: null, overlay_scale: 1 })}
            className="text-xs font-semibold text-violet-300"
          >
            Reset
          </button>
        </div>
      )}
      <div className="mt-2">
        <ImageButtons
          show={show}
          what="overlay"
          has={!!overlay}
          accept="image/png"
          upload={api.uploadOverlay}
          remove={api.deleteOverlay}
          onChanged={props.onChanged}
        />
      </div>
      {props.episodes !== null && (
        <label className="mt-3 flex items-start gap-3">
          <input
            type="checkbox"
            checked={props.episodes}
            onChange={(e) => props.onChange({ overlay_episodes: e.target.checked })}
            className="mt-0.5 size-5 shrink-0 accent-violet-500"
          />
          <span className="text-sm text-zinc-300">
            Also on full episodes
            <span className="block text-xs text-zinc-500">
              That means re-encoding the whole stream: a minute or two for a short pre-show, an hour or two of GPU time for
              an 8-hour stream, and a second copy on disk until it's uploaded. Off, full episodes go up as they are.
            </span>
          </span>
        </label>
      )}
      <p className="mt-2 text-xs text-zinc-500">
        Use a 1920×1080 PNG with a transparent background. Clips and highlights get it; Shorts use the background above.
        Where it goes and its size are saved with the show.
      </p>
    </Group>
  );
}

/** Add, change or remove one of a show's images. Unlike the show's other settings, it's saved straight away. */
function ImageButtons(props: {
  show: Show | null;
  what: string;
  has: boolean;
  accept: string;
  upload: (showId: number, image: File) => Promise<Show>;
  remove: (showId: number) => Promise<Show>;
  onChanged: () => void;
}) {
  const { show, what } = props;
  const toast = useToast();
  const file = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  if (!show) return <p className="text-xs text-zinc-500">Add the show first, then its {what}.</p>;

  const run = async (work: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await work();
      toast({ text: done });
      props.onChanged();
    } catch (e) {
      toast({ text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-wrap gap-2">
      <input
        ref={file}
        type="file"
        accept={props.accept}
        className="hidden"
        onChange={(e) => {
          const image = e.target.files?.[0];
          e.target.value = "";
          if (image) void run(() => props.upload(show.id, image), `${what[0].toUpperCase()}${what.slice(1)} saved`);
        }}
      />
      <button
        type="button"
        disabled={busy}
        onClick={() => file.current?.click()}
        className="rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-semibold active:bg-zinc-700 disabled:opacity-40"
      >
        {busy ? "Saving…" : props.has ? `Change ${what}` : `Add ${what === "overlay" ? "an" : "a"} ${what}`}
      </button>
      {props.has && (
        <button
          type="button"
          disabled={busy}
          onClick={() => run(() => props.remove(show.id), `${what[0].toUpperCase()}${what.slice(1)} removed`)}
          className="rounded-lg px-3 py-1.5 text-xs font-semibold text-red-300 disabled:opacity-40"
        >
          Remove
        </button>
      )}
    </div>
  );
}

function AutoChoice({ label, value, onChange }: { label: string; value: Auto; onChange: (v: Auto) => void }) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-24 shrink-0 text-sm text-zinc-300">{label}</span>
      <Segmented options={AUTO_LABEL} value={value} onChange={onChange} />
    </div>
  );
}

function CountBox({ label, value, onChange }: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <label className="flex items-center gap-2 rounded-xl bg-zinc-900 px-3.5 py-2 ring-1 ring-white/5 focus-within:ring-2 focus-within:ring-violet-500">
      <span className="flex-1 text-sm text-zinc-300">{label}</span>
      <input
        type="number"
        inputMode="numeric"
        min={0}
        max={100}
        value={value}
        onChange={(e) => onChange(Math.min(100, Math.max(0, Math.round(Number(e.target.value) || 0))))}
        className="w-16 bg-transparent py-1 text-right text-base font-semibold tabular-nums outline-none"
      />
    </label>
  );
}

function PatternField(props: { label: string; value: string; placeholder: string; effective: string; onChange: (v: string) => void }) {
  const date = titleDate();
  const room = MAX_TITLE - applyTitlePattern(props.effective, "", date).length - 1;
  const ok = props.effective.includes("{title}");
  return (
    <Field label={`${props.label} pattern`} hint="{date} = stream date, {title} = the AI's part">
      <input value={props.value} onChange={(e) => props.onChange(e.target.value)} placeholder={props.placeholder} className={input} />
      <div className="mt-2 rounded-xl bg-zinc-900 p-3 text-sm ring-1 ring-white/5">
        <p className="font-semibold leading-snug">{applyTitlePattern(props.effective, SAMPLE_TITLE, date)}</p>
        <p className={cn("mt-1 text-xs", ok ? "text-zinc-500" : "text-red-300")}>
          {ok ? `The AI gets ${Math.max(20, room)} characters for its part, so titles fit YouTube's 100.` : "Add {title} where the AI's part goes."}
        </p>
      </div>
    </Field>
  );
}

// --- Channels to watch ---

function Channels({ show, onChanged }: { show: Show; onChanged: () => void }) {
  const toast = useToast();
  const { data: shows } = usePoll(api.shows, 0);
  const otherShows = shows?.filter((s) => s.id !== show.id) ?? [];
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
        Checked every 10 minutes. A new stream's VOD is picked up once the stream has ended, and a video posted on X as
        soon as it's seen. Videos already there are listed but not processed; tap one to process it, or add the channel
        to the <a href="#/library" className="font-medium text-violet-300">Library</a> to work through them a few a day.
        Folders of videos on the server are added there too.
      </p>

      <div className="mt-3 space-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
        <input
          type="url"
          inputMode="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="kick.com/name, twitch.tv/name, youtube.com/@name, rumble.com/c/name or x.com/name"
          className={cn(input, "bg-zinc-800 ring-0")}
        />
        {showFilters ? (
          <FilterFields value={filters} onChange={setFilters} posts={/(^|\/\/|\.)(x|twitter)\.com\//i.test(url.trim())} />
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
          <ChannelCard key={c.id} channel={c} otherShows={otherShows} onChanged={onChanged} />
        ))}
      </div>
    </section>
  );
}

/** `posts`: an X account, whose filters match the text of each post. */
function FilterFields({ value, onChange, posts }: { value: ChannelFields; onChange: (v: ChannelFields) => void; posts?: boolean }) {
  const what = posts ? "posts" : "titles";
  return (
    <div className="space-y-3">
      <Field label={`Only ${what} with`} hint={`comma-separated; empty means every ${posts ? "post with a video" : "video"}`}>
        <input value={value.keywords} onChange={(e) => onChange({ ...value, keywords: e.target.value })} placeholder={posts ? "tonight on + night owls" : "night owls, full show"} className={cn(input, "bg-zinc-800 ring-0")} />
        <p className="mt-1.5 text-xs text-zinc-500">
          Any one keyword will do. Join words with + to need all of them, e.g. “tonight on + night owls”.
        </p>
      </Field>
      <Field label={`Skip ${what} with`} hint="comma-separated">
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

function ChannelCard({ channel, otherShows, onChanged }: { channel: Channel; otherShows: Show[]; onChanged: () => void }) {
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
  const folder = channel.platform === "folder";
  const rules = [
    channel.keywords && `only “${channel.keywords}”`,
    channel.exclude && `not “${channel.exclude}”`,
    channel.min_minutes > 0 && `${channel.min_minutes}+ min`,
  ].filter(Boolean);

  return (
    <div className="rounded-2xl bg-zinc-900 ring-1 ring-white/5">
      <div className="flex items-start gap-3 p-4">
        <div className="min-w-0 flex-1">
          {folder ? (
            <p className="break-words font-semibold">{PLATFORM_NAME[channel.platform]} · {channelName(channel)}</p>
          ) : (
            <a href={channel.url} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 font-semibold">
              {PLATFORM_NAME[channel.platform]} · {channelName(channel)}
              <LinkIcon className="size-3.5 text-zinc-500" />
            </a>
          )}
          <p className="mt-0.5 text-xs text-zinc-500">
            {status}
            {channel.waiting > 0 && ` · ${channel.waiting} waiting for ${folder ? "copying to finish" : "a stream to end"}`}
          </p>
          {channel.backlog && (
            <a href={`#/library/${channel.id}`} className="mt-0.5 block text-xs font-medium text-violet-300">
              In the Library: {channel.backlog_paused ? "paused" : `${channel.backlog_per_day} older ${folder ? "episodes" : "videos"} a day`}
            </a>
          )}
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

      {channel.platform === "x" && channel.enabled && (
        <div className="space-y-2 border-t border-white/5 p-4">
          <p className="text-xs text-zinc-500">
            X only shows posts to signed-in visitors, so the upload browser reads them. Sign it in at x.com with a spare X
            account, not your main one: X doesn't allow automated access and could lock the account.
          </p>
          <UploadBrowserStatus site="x" />
        </div>
      )}

      {editing && (
        <div className="space-y-3 border-t border-white/5 p-4">
          {!folder && (
            <>
              <FilterFields value={filters} onChange={setFilters} posts={channel.platform === "x"} />
              <Button
                tone="primary"
                className="w-full"
                onClick={() => act(() => api.updateChannel(channel.id, filters), "Filters saved").then(() => setEditing(false))}
              >
                Save filters
              </Button>
            </>
          )}
          {otherShows.length > 0 && (
            <Field label="Belongs to another show?">
              <select
                value=""
                onChange={(e) => {
                  const to = otherShows.find((s) => s.id === Number(e.target.value));
                  if (to && confirm(`Move ${channelName(channel)} to ${to.name}? Its new videos then get ${to.name}'s settings.`)) {
                    void act(() => api.updateChannel(channel.id, { show_id: to.id }), `Moved to ${to.name}`);
                  }
                }}
                className={cn(input, "appearance-none bg-zinc-800 ring-0")}
              >
                <option value="">Move it to…</option>
                {otherShows.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </Field>
          )}
        </div>
      )}

      <div className="flex divide-x divide-white/5 border-t border-white/5 text-sm">
        <CardAction onClick={() => setOpen((o) => !o)}>{open ? "Hide videos" : "Videos"}</CardAction>
        <CardAction onClick={() => act(() => api.checkChannel(channel.id), "Checking now")}>
          <RefreshIcon className="size-4" /> Check
        </CardAction>
        <CardAction onClick={() => setEditing((e) => !e)}>Settings</CardAction>
        <CardAction onClick={() => confirm("Stop watching this channel?") && act(() => api.deleteChannel(channel.id))} danger>
          Remove
        </CardAction>
      </div>

      {open && <ChannelVideos channelId={channel.id} roughDates={channel.platform === "youtube"} files={folder} />}
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

/** What the watcher found on a channel, newest first, each with when it was posted where the platform says.
 * `files`: a folder's, which have no page to link to. */
function ChannelVideos({ channelId, roughDates, files }: { channelId: number; roughDates: boolean; files: boolean }) {
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
              {files ? (
                <p className="line-clamp-2 break-words text-sm leading-snug">{v.title || v.url}</p>
              ) : (
                <a href={v.url} target="_blank" rel="noreferrer" className="line-clamp-2 text-sm leading-snug">
                  {v.title || v.url}
                </a>
              )}
              <p className="mt-0.5 text-xs text-zinc-500">
                <span className={cn("font-semibold", tone)}>{label}</span>
                {/* YouTube only says "3 days ago", so its dates are rough */}
                {v.published_at ? ` · ${roughDates ? `about ${pastDay(v.published_at, false)}` : pastDay(v.published_at)}` : ""}
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

/** Like Field, for a group of controls: a label element would pass taps on its caption to the first button. */
function Group({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div>
      <p className="mb-1.5 flex items-baseline justify-between gap-3 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        {label}
        {hint && <span className="text-right font-normal normal-case">{hint}</span>}
      </p>
      {children}
    </div>
  );
}
