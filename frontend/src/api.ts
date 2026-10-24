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

export type ClipStatus = "proposed" | "approved" | "rejected" | "rendering" | "rendered" | "uploaded" | "failed";

/** "segment": chapters and highlights; "whole": the full episode; "both": full episode plus clips. */
export type Mode = "segment" | "whole" | "both";

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
  error: string;
  has_video: boolean;
  has_short: boolean;
  video_size: number;
  short_size: number;
  destination_id: number | null;
  destination_name: string;
  destination_url: string;
  youtube_url: string;
  updated_at: string;
}

export interface Line {
  start: number;
  end: number;
  text: string;
}

export type ClipPatch = Partial<Pick<Clip, "title" | "description" | "start" | "end" | "make_short" | "destination_id">>;

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
  created_at: string;
}

export type Visibility = "public" | "unlisted" | "private";

/** What the worker last saw of the browser that uploads go through. */
export interface StudioStatus {
  browser: boolean;
  signed_in: boolean;
  checked_at: string | null;
  browser_view_url: string;
}

export interface Channel {
  id: number;
  show_id: number;
  url: string;
  platform: "kick" | "twitch" | "youtube";
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
  channels: Channel[];
  video_count: number;
}

export type ShowFields = Omit<Show, "id" | "channels" | "video_count">;
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

  vods: () => request<Vod[]>("/api/vods"),
  vod: (id: number) => request<{ vod: Vod; clips: Clip[] }>(`/api/vods/${id}`),
  addVod: (body: { url: string; mode?: Mode; notes?: string }) => post<Vod>("/api/vods", body),
  retryVod: (id: number) => post<Vod>(`/api/vods/${id}/retry`),

  clips: (statuses: ClipStatus[]) =>
    request<Clip[]>(`/api/clips?${statuses.map((s) => `status=${s}`).join("&")}`),
  clip: (id: number) => request<Clip>(`/api/clips/${id}`),
  updateClip: (id: number, fields: ClipPatch) => patch<Clip>(`/api/clips/${id}`, fields),
  approve: (id: number) => changed(post<Clip>(`/api/clips/${id}/approve`)),
  reject: (id: number) => changed(post<Clip>(`/api/clips/${id}/reject`)),
  restore: (id: number) => changed(post<Clip>(`/api/clips/${id}/restore`)),
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
  cancelUpload: (jobId: number) => remove(`/api/upload-jobs/${jobId}`),

  destinations: () => request<Destination[]>("/api/destinations"),
  addDestination: (body: { name: string; url: string }) => post<Destination>("/api/destinations", body),
  updateDestination: (id: number, body: { name: string; url: string }) => patch<Destination>(`/api/destinations/${id}`, body),
  deleteDestination: (id: number) => remove(`/api/destinations/${id}`),
  youtube: () => request<{ configured: boolean; callback_path: string }>("/api/youtube"),
  /** Google's sign-in page for the channel; it comes back to the app at this same address. */
  connectDestination: (id: number) =>
    post<{ url: string }>(`/api/destinations/${id}/connect`, { origin: location.origin }),
  disconnectDestination: (id: number) => post<Destination>(`/api/destinations/${id}/disconnect`),

  shows: () => request<Show[]>("/api/shows"),
  show: (id: number) => request<Show>(`/api/shows/${id}`),
  addShow: (fields: ShowFields) => post<Show>("/api/shows", fields),
  updateShow: (id: number, fields: Partial<ShowFields>) => patch<Show>(`/api/shows/${id}`, fields),
  deleteShow: (id: number) => remove(`/api/shows/${id}`),

  addChannel: (showId: number, body: { url: string } & ChannelFields) =>
    post<Channel>(`/api/shows/${showId}/channels`, body),
  updateChannel: (id: number, fields: Partial<ChannelFields & { enabled: boolean }>) =>
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
};

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
