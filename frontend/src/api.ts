// Types and calls for the backend API (backend/clipper/web/app.py).

import type { Sites } from "./lib";

export type VodStatus =
  | "queued"
  | "downloading"
  | "preparing"
  | "transcribing"
  | "analysing"
  | "segmenting"
  | "waiting"
  | "ready"
  | "failed";

/** "extra": picked by the AI but beyond the show's best few per stream, so not in the inbox. */
export type ClipStatus = "proposed" | "extra" | "approved" | "rejected" | "rendering" | "rendered" | "uploaded" | "failed";

/** "segment": clips (kind "chapter") and highlights; "whole": the full episode; "both": full episode plus those. */
export type Mode = "segment" | "whole" | "both";

/** What happens to a show's new videos of one kind: wait for review, render without review, or render and upload. */
export type Auto = "review" | "render" | "upload";

/** Parts of the work that can be paused as a whole. */
export type Pauses = { downloads: boolean; processing: boolean; renders: boolean; uploads: boolean };

export interface Vod {
  id: number;
  url: string;
  mode: Mode;
  notes: string;
  show_id: number | null;
  show_name: string;
  title: string;
  channel: string;
  platform: string;
  upload_date: string;
  duration: number;
  status: VodStatus;
  progress: number;
  error: string;
  retry_at: string | null;
  paused: boolean;
  downloaded: boolean; // so it only waits for processing
  cleaned: boolean;
  created_at: string;
  counts: Partial<Record<ClipStatus, number>>;
}

export interface Clip {
  id: number;
  vod_id: number;
  vod_title: string;
  vod_duration: number;
  key: string;
  kind: "chapter" | "highlight" | "whole";
  start: number;
  end: number;
  title: string;
  description: string;
  why: string;
  score: number;
  manual: boolean; // made in the app, so it has no score from the AI
  cuts: [number, number][]; // parts taken out of the middle, in the stream's seconds
  vertical_ok: boolean;
  make_short: boolean;
  status: ClipStatus;
  paused: boolean; // its render is held
  error: string;
  has_video: boolean;
  has_short: boolean;
  video_size: number;
  short_size: number;
  destination_id: number | null;
  destination_name: string;
  destination_url: string;
  playlist_id: string; // a playlist on that channel, or ""
  upload_to: Sites; // where its uploads go unless one is sent elsewhere: its show's choice, or else its channel's
  youtube_url: string;
  rumble_url: string; // once it's up on Rumble too
  reuploading: boolean; // rendering again, to be uploaded again where it went once it's done
  thumbnail_status: "" | "queued" | "making" | "done" | "failed";
  thumbnail_url: string; // "" until there's one
  thumbnail_hint: string; // your idea for it
  thumbnail_idea: string; // what the AI came up with
  thumbnail_prompt: string; // the image prompt from the latest thumbnail attempt, if one was made
  thumbnail_redrawing: boolean; // being drawn again from a prompt, rather than a new idea from the AI
  thumbnail_error: string;
  upload_state: UploadState;
  next_upload: string | null; // when a scheduled upload goes, or a failed one tries again by itself (in its turn)
  upload_error: string; // why a failed or retrying upload failed
  updated_at: string;
}

/** Where a clip's uploads stand: "queued" goes once the upload running is done; "scheduled" waits for its turn. */
export type UploadState = "" | "uploading" | "queued" | "scheduled" | "retrying" | "failed";

export interface Line {
  start: number;
  end: number;
  text: string;
  words?: [number, number, string][]; // start, end, text: asked for when snapping cuts to words
}

export type ClipPatch = Partial<
  Pick<Clip, "title" | "description" | "start" | "end" | "cuts" | "make_short" | "destination_id" | "playlist_id">
>;

/** A clip made in the app from any part of a stream. */
export interface NewClip {
  kind: "chapter" | "highlight";
  start: number;
  end: number;
  cuts: [number, number][];
  title: string;
  description: string;
  make_short: boolean;
  destination_id: number | null;
  playlist_id: string;
  approve: boolean; // straight to rendering, rather than into the inbox
}

/** What a clip made in the app starts with. */
export interface NewClipInfo {
  title_pattern: string; // the show's pattern with the stream's date in it, and {title} for the part you write
  title_room: number; // how long that part can be
  destination_id: number | null;
  playlist_id: string;
}

/** A playlist on a YouTube channel connected to the API. */
export interface Playlist {
  id: string;
  title: string;
  privacy: string;
  count: number;
}

/** A YouTube channel that videos get uploaded to. */
export interface Destination {
  id: number;
  name: string;
  url: string;
  channel_id: string;
  studio_upload_url: string;
  api_connected: boolean; // uploads go through the YouTube API; otherwise through YouTube Studio
  api_account: string; // the channel name Google reported when it was connected
  api_error: string; // why connecting failed, or why its sign-in stopped working
  youtube: boolean; // its uploads go to YouTube
  rumble: boolean; // and/or to Rumble, through the upload browser
  rumble_category: string; // Rumble's primary category for them
}

export type Site = "youtube" | "rumble";

/** One file of a clip being uploaded by the worker: to YouTube through its API or Studio, or to Rumble. */
export interface UploadJob {
  id: number;
  clip_id: number;
  which: "video" | "short";
  site: Site;
  visibility: Visibility;
  status: "queued" | "uploading" | "done" | "failed";
  method: "api" | "studio" | "browser" | "";
  progress: number;
  cancel_requested: boolean; // cancelled while going up: the worker is stopping it
  youtube_url: string; // its link once it's up, on whichever site
  error: string; // why it failed; on a finished upload, a note
  has_screenshot: boolean;
  rush: boolean; // sent with "Upload now"
  retry_at: string | null; // a failed upload that's tried again by itself then: its retry time, or its turn after that
  starts_at: string | null; // when it goes, if it's waiting for its turn
  wait_reason: "" | "gap" | "thumbnail" | "retry"; // "gap": the show's spacing puts it later
  views: number | null;
  likes: number | null;
  comments: number | null;
  views_day1: number | null;
  views_week1: number | null;
  created_at: string;
  finished_at: string | null;
}

/** How the app's uploads are doing on YouTube. */
export interface Stats {
  setup_needed: string; // why views can't be read, or ""
  checked_at: string | null;
  bands: { label: string; count: number; day1: number | null; week1: number | null; views: number | null }[];
  uploads: StatsUpload[];
}

export interface StatsUpload {
  job_id: number;
  clip_id: number;
  show_id: number | null;
  title: string;
  kind: Clip["kind"];
  which: "video" | "short";
  score: number;
  manual: boolean;
  youtube_url: string;
  finished_at: string | null;
  views: number | null;
  likes: number | null;
  comments: number | null;
  views_day1: number | null;
  views_week1: number | null;
}

export type Visibility = "public" | "unlisted" | "private";

/** What the worker last saw of the browser that uploads go through, which also reads X. */
export interface StudioStatus {
  browser: boolean;
  signed_in: boolean;
  x_signed_in: boolean;
  checked_at: string | null;
  browser_view_url: string;
}

export interface Channel {
  id: number;
  show_id: number;
  url: string;
  platform: "kick" | "twitch" | "youtube" | "rumble" | "x" | "folder";
  keywords: string;
  exclude: string;
  min_minutes: number;
  enabled: boolean;
  last_checked_at: string | null;
  last_error: string;
  waiting: number;
  // In the Library: its older videos are worked through, backlog_per_day a day, in order.
  backlog: boolean;
  backlog_per_day: number;
  backlog_paused: boolean;
  backlog_order: BacklogOrder;
  history_listed: boolean; // a channel's whole history has been listed for it
}

/** The order a backlog takes its videos in: from the first episode (or oldest video), from the latest, or at random. */
export type BacklogOrder = "first" | "latest" | "random";

/** What a show's videos are, for the AI: livestreams, TV episodes, or anything else (podcasts, vlogs, talks). */
export type VideoType = "streams" | "episodes" | "other";

/** A part of the picture: x, y, width and height, as fractions of it. */
export type Box = [number, number, number, number];

export interface Show {
  id: number;
  name: string;
  match: string;
  mode: Mode;
  video_type: VideoType;
  title_template: string;
  episode_template: string;
  notes: string;
  clip_guidance: string;
  clip_destination_id: number | null;
  episode_destination_id: number | null;
  clip_playlist_id: string; // on the clips' channel; "" for none
  episode_playlist_id: string;
  clip_sites: Sites | ""; // where uploads to the clips' channel go; "" goes by the channel's own setting
  episode_sites: Sites | "";
  max_chapters: number; // offered per stream, best first; the rest are extras
  max_shorts: number;
  auto_episode: Auto;
  auto_chapters: Auto;
  auto_shorts: Auto;
  auto_upload_score: number; // offered clips and Shorts the AI scores at least this upload without review; 0 is off
  auto_visibility: Visibility; // for automatic uploads
  upload_gap_hours: number; // clips go up at least this long after the channel's last one; 0 = right away
  chapter_thumbnails: boolean; // clips (kind "chapter") get a thumbnail once rendered
  episode_thumbnails: boolean; // and full episodes
  thumbnail_guidance: string; // what every thumbnail of the show must have, e.g. words on it
  learning_notes: string; // what you've noticed works, which the AI follows over how past clips did
  save_transcripts: boolean; // each stream's transcript is also saved as files, for use outside the app
  breakdown: string; // categories the AI sorts each stream's time into, saved with it: one per line, "Name: what it covers"
  short_video_top: number | null; // where the video's top sits on its Shorts, as a fraction; null centres it
  short_video_width: number; // fraction of the Short's width
  short_background: string; // the background image's address, or "" for a blurred copy of the stream
  // Its Shorts: "crop" fills them with the middle of the picture, "blur" puts the whole picture on the background,
  // "split" puts the webcam across the top and the game under it. A new show sends "" for the server's CLIPPER_SHORT_LAYOUT.
  short_layout: "" | "crop" | "blur" | "split";
  short_webcam: Box | null; // for "split": where the webcam is in the picture
  short_game: Box | null; // and the part the game is in; null for all of it
  overlay: string; // the overlay image's address, or "" for none
  overlay_box: [number, number, number, number] | null; // x, y, w, h of its visible part, as drawn, in its 1920×1080
  overlay_screen: [number, number, number, number] | null; // x, y, w, h of a frame's screen
  overlay_episodes: boolean; // full episodes get the overlay too (re-encoding the whole stream)
  overlay_x: number | null; // a logo's top-left as fractions of the picture; null: where it was drawn
  overlay_y: number | null;
  overlay_scale: number; // a logo's size compared with as drawn
  channels: Channel[];
  video_count: number;
}

export type ShowFields = Omit<
  Show,
  "id" | "channels" | "video_count" | "short_background" | "overlay" | "overlay_box" | "overlay_screen"
>;
export type ChannelFields = Pick<Channel, "keywords" | "exclude" | "min_minutes">;
export type BacklogFields = Pick<Channel, "backlog_per_day" | "backlog_paused" | "backlog_order">;

export interface ChannelVideo {
  id: number;
  channel_id: number;
  url: string;
  title: string;
  duration: number | null;
  status: "waiting" | "queued" | "skipped" | "older";
  reason: string;
  vod_id: number | null;
  published_at: string | null; // when it was posted or streamed, if the platform says (roughly, on YouTube)
  first_seen_at: string;
}

/** A folder in the server's media folder, for choosing one to watch. */
export interface Folder {
  path: string;
  parent: string | null; // null in the media folder itself
  folders: string[];
  videos: number; // video files in it, not counting its folders'
}

/** A channel or folder in the Library, and how far through its videos it is. */
export interface Backlog {
  channel: Channel;
  show_name: string;
  video_type: VideoType;
  name: string;
  total: number; // not counting skipped ones
  done: number;
  working: number;
  failed: number;
  left: number;
  skipped: number;
  copying: number; // files being copied in, or streams not finished
  next: string; // the next one it takes; "" when none are left
  today: number; // how many it took in the last 24 hours, of its backlog_per_day
}

export interface BacklogItem {
  id: number; // the ChannelVideo's
  title: string;
  url: string;
  group: string; // a folder's subfolder ("Season 1"), or the month a channel's video was posted
  state: "done" | "working" | "failed" | "left" | "skipped" | "copying";
  reason: string;
  published_at: string | null;
  vod_id: number | null;
  vod_status: VodStatus | "";
  vod_progress: number;
  counts: Partial<Record<ClipStatus, number>>;
  place: number | null; // one still to go: its place in the line, 1 being next
}

export interface BacklogDetail extends Backlog {
  items: BacklogItem[]; // in the order it works through them; at random, in season and episode order (or oldest first)
}

/** Counts for the tab badges. */
export interface Summary {
  to_review: number;
  to_upload: number; // rendered clips, whether or not their upload has started
  videos_in_progress: number;
}

/** Space on the data disk, and how much the streams' files take. */
export interface Storage {
  free_gb: number | null;
  total_gb: number | null;
  videos_gb: number;
  min_free_gb: number; // below this, new downloads wait for space
}

/** The main AI, which picks the clips and writes preview titles and thumbnail ideas unless a task has its own. */
export type AiProviderId = "claude" | "codex" | "openrouter";

export interface AiTask {
  id: string; // "clips", "breakdown", "titles", "thumbnails"
  name: string;
  hint: string;
  provider: AiProviderId | ""; // its own AI, or "" for the main one
  model: string; // its own model, or "" for that AI's
  uses: string; // the AI and model it uses, e.g. "Codex (gpt-6-luna)"
  problem: string; // what stops that AI from working; "" if nothing does
}

export interface AiSetup {
  provider: AiProviderId;
  providers: { id: AiProviderId; name: string; model: string; default_model: string }[]; // Codex's "" is its default
  problem: string; // what stops the chosen one from working, e.g. no sign-in; "" if nothing does
  tasks: AiTask[];
}

/** Settings changed from the app. */
export interface AppSettings {
  download_limit_mbps: number; // 0 = no limit
  full_speed_from: number; // hours of the day when downloads ignore the limit; the same for both is off
  full_speed_until: number;
  cleanup_after_days: number; // a finished stream's big files are deleted this long after its last change
  timezone: string; // what those hours are in
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    credentials: "same-origin",
  });
  if (res.status === 401 && !path.startsWith("/api/login")) {
    window.dispatchEvent(new Event("clipper:signed-out"));
  }
  if (!res.ok) {
    let message = res.statusText;
    try {
      const body = await res.json();
      message = typeof body.detail === "string" ? body.detail : (body.detail?.[0]?.msg ?? message);
    } catch {
      // not JSON: keep the status text
    }
    throw new ApiError(res.status, message);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const patch = <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
const remove = (path: string) => request<void>(path, { method: "DELETE" });

/** Clip actions announce themselves so the tab badges update right away. */
const changed = <T>(promise: Promise<T>) =>
  promise.then((result) => {
    window.dispatchEvent(new Event("clipper:changed"));
    return result;
  });

export const api = {
  session: () => request<{ authenticated: boolean; password_required: boolean }>("/api/session"),
  login: (password: string) => post<void>("/api/login", { password }),
  logout: () => post<void>("/api/logout"),
  summary: () => request<Summary>("/api/summary"),
  settings: () => request<AppSettings>("/api/settings"),
  updateSettings: (body: Partial<Omit<AppSettings, "timezone">>) => patch<AppSettings>("/api/settings", body),
  storage: () => request<Storage>("/api/storage"),
  pauses: () => request<Pauses>("/api/pauses"),
  setPauses: (body: Partial<Pauses>) => patch<Pauses>("/api/pauses", body),
  notifications: () => request<{ ntfy: boolean; discord: boolean; links: boolean }>("/api/notifications"),
  ai: () => request<AiSetup>("/api/ai"),
  setAi: (body: { task?: string; provider?: AiProviderId | ""; model?: string }) => patch<AiSetup>("/api/ai", body),
  testNotification: () => post<{ sent: boolean }>("/api/notifications/test"),

  vods: () => request<Vod[]>("/api/vods"),
  vod: (id: number) => request<{ vod: Vod; clips: Clip[] }>(`/api/vods/${id}`),
  addVod: (body: { url: string; mode?: Mode; notes?: string }) => post<Vod>("/api/vods", body),
  retryVod: (id: number) => post<Vod>(`/api/vods/${id}/retry`),
  pauseVod: (id: number) => post<Vod>(`/api/vods/${id}/pause`),
  resumeVod: (id: number) => post<Vod>(`/api/vods/${id}/resume`),
  deleteVodFiles: (id: number) => changed(post<Vod>(`/api/vods/${id}/delete-files`)),
  deleteVod: (id: number) => changed(remove(`/api/vods/${id}`)),

  clips: (statuses: ClipStatus[]) =>
    request<Clip[]>(`/api/clips?${statuses.map((s) => `status=${s}`).join("&")}`),
  clip: (id: number) => request<Clip>(`/api/clips/${id}`),
  updateClip: (id: number, fields: ClipPatch) => patch<Clip>(`/api/clips/${id}`, fields),
  approve: (id: number) => changed(post<Clip>(`/api/clips/${id}/approve`)),
  reject: (id: number) => changed(post<Clip>(`/api/clips/${id}/reject`)),
  restore: (id: number) => changed(post<Clip>(`/api/clips/${id}/restore`)),
  pauseClip: (id: number) => post<Clip>(`/api/clips/${id}/pause`),
  resumeClip: (id: number) => post<Clip>(`/api/clips/${id}/resume`),
  markUploaded: (id: number, youtube_url: string) => changed(post<Clip>(`/api/clips/${id}/uploaded`, { youtube_url })),
  unmarkUploaded: (id: number) => changed(post<Clip>(`/api/clips/${id}/not-uploaded`)),
  reupload: (id: number) => changed(post<Clip>(`/api/clips/${id}/reupload`)),
  transcript: (id: number) => request<Line[]>(`/api/clips/${id}/transcript`),

  /** The stream's transcript: all of it, or from start to end, with each word's timing if asked. */
  vodTranscript: (vodId: number, part?: { start: number; end: number; words?: boolean }) =>
    request<Line[]>(
      `/api/vods/${vodId}/transcript${part ? `?start=${part.start}&end=${part.end}${part.words ? "&words=true" : ""}` : ""}`,
    ),
  /** How loud the stream is across `count` parts of start to end, from 0 to 1. */
  peaks: (vodId: number, start: number, end: number, count: number) =>
    request<{ start: number; end: number; peaks: number[] }>(
      `/api/vods/${vodId}/peaks?start=${start}&end=${end}&count=${count}`,
    ),
  newClipInfo: (vodId: number) => request<NewClipInfo>(`/api/vods/${vodId}/new-clip`),
  makeClip: (vodId: number, body: NewClip) => changed(post<Clip>(`/api/vods/${vodId}/clips`, body)),

  studio: () => request<StudioStatus>("/api/studio"),
  openStudioBrowser: () => post<{ opened: boolean }>("/api/studio/open"),
  checkStudio: () => post<StudioStatus>("/api/studio/check"),
  uploads: (clipId: number) => request<UploadJob[]>(`/api/clips/${clipId}/uploads`),
  startUpload: (clipId: number, which: UploadJob["which"], visibility: Visibility, site: Site) =>
    post<UploadJob>(`/api/clips/${clipId}/uploads`, { which, visibility, site }),
  retryUpload: (jobId: number) => post<UploadJob>(`/api/upload-jobs/${jobId}/retry`),
  uploadNow: (jobId: number) => post<UploadJob>(`/api/upload-jobs/${jobId}/now`),
  cancelUpload: (jobId: number) => remove(`/api/upload-jobs/${jobId}`),

  thumbnails: () => request<{ configured: boolean; model: string }>("/api/thumbnails"),
  makeThumbnail: (clipId: number, hint: string) => post<Clip>(`/api/clips/${clipId}/thumbnail`, { hint }),
  redrawThumbnail: (clipId: number, prompt: string) => post<Clip>(`/api/clips/${clipId}/thumbnail`, { prompt }),
  applyThumbnail: (clipId: number) => post<{ applied: boolean }>(`/api/clips/${clipId}/thumbnail/apply`),
  stats: (showId: number | null) => request<Stats>(`/api/stats${showId === null ? "" : `?show_id=${showId}`}`),

  destinations: () => request<Destination[]>("/api/destinations"),
  addDestination: (body: { name: string; url: string }) => post<Destination>("/api/destinations", body),
  updateDestination: (
    id: number,
    body: { name: string; url: string; youtube?: boolean; rumble?: boolean; rumble_category?: string },
  ) =>
    patch<Destination>(`/api/destinations/${id}`, body),
  rumble: () => request<{ categories: string[]; licence: string }>("/api/rumble"),
  deleteDestination: (id: number) => remove(`/api/destinations/${id}`),
  youtube: () => request<{ configured: boolean; callback_path: string }>("/api/youtube"),
  /** Google's sign-in page for the channel; it comes back to the app at this same address. */
  connectDestination: (id: number) =>
    post<{ url: string }>(`/api/destinations/${id}/connect`, { origin: location.origin }),
  disconnectDestination: (id: number) => post<Destination>(`/api/destinations/${id}/disconnect`),
  playlists: (destId: number) => request<Playlist[]>(`/api/destinations/${destId}/playlists`),
  createPlaylist: (destId: number, title: string, privacy: Visibility) =>
    post<Playlist>(`/api/destinations/${destId}/playlists`, { title, privacy }),

  shows: () => request<Show[]>("/api/shows"),
  show: (id: number) => request<Show>(`/api/shows/${id}`),
  /** What the show's past clips teach the AI, as it's given with each new stream. */
  trackRecord: (id: number) => request<{ text: string }>(`/api/shows/${id}/track-record`),
  addShow: (fields: ShowFields) => post<Show>("/api/shows", fields),
  updateShow: (id: number, fields: Partial<ShowFields>) => patch<Show>(`/api/shows/${id}`, fields),
  deleteShow: (id: number) => remove(`/api/shows/${id}`),
  /** The image file itself is the body; the server makes it 1080×1920. */
  uploadShortBackground: (id: number, image: File) =>
    request<Show>(`/api/shows/${id}/short-background`, {
      method: "PUT",
      body: image,
      headers: { "Content-Type": image.type || "application/octet-stream" },
    }),
  deleteShortBackground: (id: number) => request<Show>(`/api/shows/${id}/short-background`, { method: "DELETE" }),
  /** A PNG with transparent parts; the server makes it 1920×1080 and works out whether it's a frame. */
  uploadOverlay: (id: number, image: File) =>
    request<Show>(`/api/shows/${id}/overlay`, {
      method: "PUT",
      body: image,
      headers: { "Content-Type": image.type || "application/octet-stream" },
    }),
  deleteOverlay: (id: number) => request<Show>(`/api/shows/${id}/overlay`, { method: "DELETE" }),

  addChannel: (showId: number, body: { url: string } & Partial<ChannelFields & BacklogFields & { backlog: boolean }>) =>
    post<Channel>(`/api/shows/${showId}/channels`, body),
  updateChannel: (id: number, fields: Partial<ChannelFields & BacklogFields & { enabled: boolean; show_id: number; backlog: boolean }>) =>
    patch<Channel>(`/api/channels/${id}`, fields),
  deleteChannel: (id: number) => remove(`/api/channels/${id}`),
  checkChannel: (id: number) => post<Channel>(`/api/channels/${id}/check`),
  channelVideos: (id: number) => request<ChannelVideo[]>(`/api/channels/${id}/videos`),
  processChannelVideo: (id: number) => post<ChannelVideo>(`/api/channel-videos/${id}/process`),

  folders: (path?: string) => request<Folder>(`/api/folders${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  /** What watching the folder would find: how many videos, and the first few in the order they'd go. */
  folderVideos: (path: string) =>
    request<{ videos: number; first: string[] }>(`/api/folders/videos?path=${encodeURIComponent(path)}`),
  library: () => request<Backlog[]>("/api/library"),
  backlog: (channelId: number) => request<BacklogDetail>(`/api/channels/${channelId}/backlog`),
  /** Skip older videos so the backlog passes over them, or put skipped ones back. */
  skipInBacklog: (channelId: number, ids: number[], skip: boolean) =>
    post<void>(`/api/channels/${channelId}/backlog/skip`, { ids, skip }),
};

export const media = {
  proxy: (vodId: number) => `/media/vods/${vodId}/proxy.mp4`,
  /** A small still of the stream, for the editor's timeline. */
  frame: (vodId: number, t: number) => `/media/vods/${vodId}/frame.jpg?t=${t.toFixed(1)}`,
  thumb: (c: Clip) => `/media/clips/${c.id}/thumb.jpg?t=${Math.round(c.start * 10)}`,
  file: (c: Clip, which: "video" | "short", download = false) =>
    `/media/clips/${c.id}/${which}.mp4?v=${encodeURIComponent(c.updated_at)}${download ? "&download=true" : ""}`,
  uploadScreenshot: (job: UploadJob) => `/media/upload-jobs/${job.id}/screenshot.png`,
  thumbnailDownload: (c: Clip) => `/media/clips/${c.id}/thumbnail.jpg?download=true`,
};

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
