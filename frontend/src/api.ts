// Types and calls for the backend API (backend/clipper/web/app.py).

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

/** "extra": picked by Claude but beyond the show's best few per stream, so not in the inbox. */
export type ClipStatus = "proposed" | "extra" | "approved" | "rejected" | "rendering" | "rendered" | "uploaded" | "failed";

/** "segment": chapters and highlights; "whole": the full episode; "both": full episode plus clips. */
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
  youtube_url: string;
  thumbnail_status: "" | "queued" | "making" | "done" | "failed";
  thumbnail_url: string; // "" until there's one
  thumbnail_hint: string; // your idea for it
  thumbnail_idea: string; // what Claude came up with
  thumbnail_error: string;
  next_upload: string | null; // when its upload goes, if it's waiting for its turn
  updated_at: string;
}

export interface Line {
  start: number;
  end: number;
  text: string;
}

export type ClipPatch = Partial<
  Pick<Clip, "title" | "description" | "start" | "end" | "make_short" | "destination_id" | "playlist_id">
>;

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
}

/** One file of a clip being uploaded by the worker, through the YouTube API or YouTube Studio. */
export interface UploadJob {
  id: number;
  clip_id: number;
  which: "video" | "short";
  visibility: Visibility;
  status: "queued" | "uploading" | "done" | "failed";
  method: "api" | "studio" | "";
  progress: number;
  youtube_url: string;
  error: string; // why it failed; on a finished upload, a note
  has_screenshot: boolean;
  rush: boolean; // sent with "Upload now"
  starts_at: string | null; // when it goes, if it's waiting for its turn
  wait_reason: "" | "gap" | "thumbnail";
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
  platform: "kick" | "twitch" | "youtube" | "x";
  keywords: string;
  exclude: string;
  min_minutes: number;
  enabled: boolean;
  last_checked_at: string | null;
  last_error: string;
  waiting: number;
}

export interface Show {
  id: number;
  name: string;
  match: string;
  mode: Mode;
  title_template: string;
  episode_template: string;
  notes: string;
  clip_guidance: string;
  clip_destination_id: number | null;
  episode_destination_id: number | null;
  clip_playlist_id: string; // on the clips' channel; "" for none
  episode_playlist_id: string;
  max_chapters: number; // offered per stream, best first; the rest are extras
  max_shorts: number;
  auto_episode: Auto;
  auto_chapters: Auto;
  auto_shorts: Auto;
  auto_upload_score: number; // offered chapters and Shorts Claude scores at least this upload without review; 0 is off
  auto_visibility: Visibility; // for automatic uploads
  upload_gap_hours: number; // clips go up at least this long after the channel's last one; 0 = right away
  auto_thumbnails: boolean; // chapters and full episodes get a thumbnail once rendered
  short_video_top: number | null; // where the video's top sits on its Shorts, as a fraction; null centres it
  short_video_width: number; // fraction of the Short's width
  short_background: string; // the background image's address, or "" for a blurred copy of the stream
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

export interface ChannelVideo {
  id: number;
  channel_id: number;
  url: string;
  title: string;
  duration: number | null;
  status: "waiting" | "queued" | "skipped" | "older";
  reason: string;
  vod_id: number | null;
  first_seen_at: string;
}

/** Settings changed from the app. */
export interface AppSettings {
  download_limit_mbps: number; // 0 = no limit
  cleanup_after_days: number; // a finished stream's big files are deleted this long after its last change
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
  summary: () => request<{ to_review: number; to_upload: number; videos_in_progress: number }>("/api/summary"),
  settings: () => request<AppSettings>("/api/settings"),
  updateSettings: (body: Partial<AppSettings>) => patch<AppSettings>("/api/settings", body),
  storage: () =>
    request<{ free_gb: number | null; total_gb: number | null; videos_gb: number; min_free_gb: number }>("/api/storage"),
  pauses: () => request<Pauses>("/api/pauses"),
  setPauses: (body: Partial<Pauses>) => patch<Pauses>("/api/pauses", body),
  notifications: () => request<{ ntfy: boolean; discord: boolean; links: boolean }>("/api/notifications"),
  testNotification: () => post<{ sent: boolean }>("/api/notifications/test"),

  vods: () => request<Vod[]>("/api/vods"),
  vod: (id: number) => request<{ vod: Vod; clips: Clip[] }>(`/api/vods/${id}`),
  addVod: (body: { url: string; mode?: Mode; notes?: string }) => post<Vod>("/api/vods", body),
  retryVod: (id: number) => post<Vod>(`/api/vods/${id}/retry`),
  pauseVod: (id: number) => post<Vod>(`/api/vods/${id}/pause`),
  resumeVod: (id: number) => post<Vod>(`/api/vods/${id}/resume`),
  deleteVodFiles: (id: number) => changed(post<Vod>(`/api/vods/${id}/delete-files`)),

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
  transcript: (id: number) => request<Line[]>(`/api/clips/${id}/transcript`),

  studio: () => request<StudioStatus>("/api/studio"),
  openStudioBrowser: () => post<{ opened: boolean }>("/api/studio/open"),
  checkStudio: () => post<StudioStatus>("/api/studio/check"),
  uploads: (clipId: number) => request<UploadJob[]>(`/api/clips/${clipId}/uploads`),
  startUpload: (clipId: number, which: UploadJob["which"], visibility: Visibility) =>
    post<UploadJob>(`/api/clips/${clipId}/uploads`, { which, visibility }),
  retryUpload: (jobId: number) => post<UploadJob>(`/api/upload-jobs/${jobId}/retry`),
  uploadNow: (jobId: number) => post<UploadJob>(`/api/upload-jobs/${jobId}/now`),
  cancelUpload: (jobId: number) => remove(`/api/upload-jobs/${jobId}`),

  thumbnails: () => request<{ configured: boolean; model: string }>("/api/thumbnails"),
  makeThumbnail: (clipId: number, hint: string) => post<Clip>(`/api/clips/${clipId}/thumbnail`, { hint }),
  applyThumbnail: (clipId: number) => post<{ applied: boolean }>(`/api/clips/${clipId}/thumbnail/apply`),
  stats: (showId: number | null) => request<Stats>(`/api/stats${showId === null ? "" : `?show_id=${showId}`}`),

  destinations: () => request<Destination[]>("/api/destinations"),
  addDestination: (body: { name: string; url: string }) => post<Destination>("/api/destinations", body),
  updateDestination: (id: number, body: { name: string; url: string }) => patch<Destination>(`/api/destinations/${id}`, body),
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

  addChannel: (showId: number, body: { url: string } & ChannelFields) =>
    post<Channel>(`/api/shows/${showId}/channels`, body),
  updateChannel: (id: number, fields: Partial<ChannelFields & { enabled: boolean; show_id: number }>) =>
    patch<Channel>(`/api/channels/${id}`, fields),
  deleteChannel: (id: number) => remove(`/api/channels/${id}`),
  checkChannel: (id: number) => post<Channel>(`/api/channels/${id}/check`),
  channelVideos: (id: number) => request<ChannelVideo[]>(`/api/channels/${id}/videos`),
  processChannelVideo: (id: number) => post<ChannelVideo>(`/api/channel-videos/${id}/process`),
};

export const media = {
  proxy: (vodId: number) => `/media/vods/${vodId}/proxy.mp4`,
  thumb: (c: Clip) => `/media/clips/${c.id}/thumb.jpg?t=${Math.round(c.start * 10)}`,
  file: (c: Clip, which: "video" | "short", download = false) =>
    `/media/clips/${c.id}/${which}.mp4?v=${encodeURIComponent(c.updated_at)}${download ? "&download=true" : ""}`,
  uploadScreenshot: (job: UploadJob) => `/media/upload-jobs/${job.id}/screenshot.png`,
  thumbnailDownload: (c: Clip) => `/media/clips/${c.id}/thumbnail.jpg?download=true`,
};

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
