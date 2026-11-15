import type { ReactNode } from "react";
import { api, media, type Clip, type Destination, type Pauses, type Stats, type StatsUpload, type Summary, type Vod } from "../api";
import { AlertIcon, LinkIcon } from "../icons";
import { ago, cn, compact, firstSentence, length, platformName, total, upcoming, usePoll } from "../lib";
import { usePauses } from "../PauseBar";
import { ErrorNote, KIND_LABEL, Panel, ProgressBar, Screen, StatTile, VodStatusChip } from "../ui";
import { GOING_UP, inUploadOrder } from "./Approved";
import { LibraryPanel } from "./Library";
import { UploadBrowserStatus } from "./UploadKit";
import { ACTIVE, WITH_PROGRESS, waitingText } from "./Videos";

const DAY_MS = 86400000;
const LIST_MAX = 8; // rows a panel shows before "and N more"

/**
 * Everything at a glance, for a desktop's big screen: what's waiting for you, what goes up next and when, what's
 * being worked on, what needs a look, how recent uploads are doing, and how each channel uploads.
 */
export function Overview({ summary }: { summary?: Summary }) {
  const { data: clips, error } = usePoll(() => api.clips(["approved", "rendering", "failed", "rendered", "uploaded"]), 10000);
  const { data: vods } = usePoll(api.vods, 10000);
  const { data: stats } = usePoll(() => api.stats(null), 60000);
  const { data: destinations } = usePoll(api.destinations, 60000);
  const { pauses } = usePauses();

  const all = clips ?? [];
  const rendering = all.filter((c) => c.status === "approved" || c.status === "rendering");
  const renderFailed = all.filter((c) => c.status === "failed");
  const ready = all.filter((c) => c.status === "rendered" && !GOING_UP.includes(c.upload_state));
  const queue = all
    .filter((c) => (c.status === "rendered" || c.status === "uploaded") && GOING_UP.includes(c.upload_state))
    .sort(inUploadOrder);
  const uploadFailed = all.filter((c) => c.upload_state === "failed");
  const working = (vods ?? []).filter((v) => v.status !== "ready" && v.status !== "failed");
  const waitingTurn = working.filter((v) => v.status === "queued").length;
  const since = Date.now() - 7 * DAY_MS;
  const week = (stats?.uploads ?? []).filter((u) => u.finished_at && Date.parse(u.finished_at) >= since);
  const weekViews = total(week.map((u) => u.views));
  const scheduledNext = queue.find((c) => c.upload_state === "scheduled" && c.next_upload);
  const videoPauses = pauseWords(pauses, ["downloads", "processing"]);

  return (
    <Screen title="Overview" subtitle="What's waiting, what's going up, and how it's doing." wide>
      {error && <ErrorNote>{error}</ErrorNote>}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <StatTile label="To review" value={summary?.to_review ?? "–"} note="Clips in the inbox" href="#/inbox" />
        <StatTile
          label="Rendering"
          value={clips ? rendering.length : "–"}
          note={
            pauses?.renders ? (
              <Warn>Rendering is paused</Warn>
            ) : renderFailed.length ? (
              <Bad>{renderFailed.length} failed</Bad>
            ) : (
              "Approved clips"
            )
          }
          href="#/approved"
        />
        <StatTile
          label="Ready to upload"
          value={clips ? ready.length : "–"}
          note={uploadFailed.length ? <Bad>{uploadFailed.length} failed to upload</Bad> : "Upload not started"}
          href="#/approved"
        />
        <StatTile
          label="Upload queue"
          value={clips ? queue.length : "–"}
          note={
            pauses?.uploads ? (
              <Warn>Uploads are paused</Warn>
            ) : queue.some((c) => c.upload_state === "uploading") ? (
              "Uploading now"
            ) : scheduledNext ? (
              `Next ${upcoming(scheduledNext.next_upload!)}`
            ) : queue.length ? (
              "Next goes up soon"
            ) : (
              "Nothing queued"
            )
          }
          href="#/approved"
        />
        <StatTile
          label="Videos in progress"
          value={summary?.videos_in_progress ?? "–"}
          note={videoPauses ? <Warn>{videoPauses} paused</Warn> : waitingTurn ? `${waitingTurn} waiting their turn` : "Downloading to picking clips"}
          href="#/videos"
        />
        <StatTile
          label="On YouTube this week"
          value={stats ? week.length : "–"}
          note={weekViews !== null ? `${compact(weekViews)} views so far` : "Uploads in the last 7 days"}
          href="#/stats"
        />
      </div>

      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="min-w-0 space-y-6">
          <Attention clips={all} vods={vods ?? []} />
          <UploadQueue queue={queue} ready={ready.length} loaded={!!clips} />
          <InProgress vods={working} pauses={pauses} loaded={!!vods} />
        </div>
        <div className="min-w-0 space-y-6">
          <RecentUploads stats={stats} />
          <LibraryPanel />
          <Channels destinations={destinations} />
        </div>
      </div>
    </Screen>
  );
}

const Warn = ({ children }: { children: ReactNode }) => <span className="text-amber-300">{children}</span>;
const Bad = ({ children }: { children: ReactNode }) => <span className="text-red-300">{children}</span>;

/** "Downloads and processing", for whichever of those parts are paused; "" if none. */
function pauseWords(pauses: Pauses | undefined, parts: (keyof Pauses)[]): string {
  const names = parts.filter((p) => pauses?.[p]).map((p) => p[0].toUpperCase() + p.slice(1));
  return names.map((n, i) => (i ? n.toLowerCase() : n)).join(" and ");
}

function More({ count, href, label }: { count: number; href: string; label?: string }) {
  if (count <= 0) return null;
  return (
    <a href={href} className="block border-t border-white/5 px-4 py-2.5 text-xs font-semibold text-zinc-400 hover:text-zinc-200">
      and {count} more{label && ` ${label}`}
    </a>
  );
}

const Empty = ({ children }: { children: ReactNode }) => <p className="px-4 pb-4 pt-1 text-sm text-zinc-500">{children}</p>;

type Problem = { key: string; tone: "bad" | "warn"; title: string; detail: string; href: string };

/** Failures to deal with, and videos held up until something changes. Only there when there's something. */
function Attention({ clips, vods }: { clips: Clip[]; vods: Vod[] }) {
  const problems: Problem[] = [
    ...clips
      .filter((c) => c.upload_state === "failed")
      .map((c): Problem => ({
        key: `u${c.id}`,
        tone: "bad",
        title: c.title,
        detail: `Upload failed${c.destination_name ? ` to ${c.destination_name}` : ""}. Open it to see why and try again.`,
        href: `#/upload/${c.id}`,
      })),
    ...clips
      .filter((c) => c.status === "failed")
      .map((c): Problem => ({ key: `r${c.id}`, tone: "bad", title: c.title, detail: `Render failed: ${c.error}`, href: `#/clip/${c.id}` })),
    ...vods
      .filter((v) => v.status === "failed")
      .map((v): Problem => ({ key: `v${v.id}`, tone: "bad", title: v.title || v.url, detail: `Failed: ${v.error}`, href: `#/videos/${v.id}` })),
    ...vods
      .filter((v) => v.status === "waiting")
      .map((v): Problem => ({
        key: `w${v.id}`,
        tone: "warn",
        title: v.title || v.url,
        detail: waitingText(v),
        href: `#/videos/${v.id}`,
      })),
  ];
  if (!problems.length) return null;

  return (
    <Panel title={`Needs a look · ${problems.length}`}>
      <ul className="divide-y divide-white/5 border-t border-white/5">
        {problems.slice(0, LIST_MAX).map((p) => (
          <li key={p.key}>
            <a href={p.href} className="flex items-start gap-3 px-4 py-2.5 hover:bg-white/[0.03]">
              <AlertIcon className={cn("mt-0.5 size-4 shrink-0", p.tone === "bad" ? "text-red-300" : "text-amber-300")} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-semibold">{p.title}</span>
                <span className={cn("line-clamp-2 break-words text-xs", p.tone === "bad" ? "text-red-300" : "text-amber-300")}>
                  {p.detail}
                </span>
              </span>
            </a>
          </li>
        ))}
      </ul>
      <More count={problems.length - LIST_MAX} href="#/approved" />
    </Panel>
  );
}

/** When each started upload goes, in order. */
function queueTime(c: Clip): [string, string] {
  const at = c.next_upload ? upcoming(c.next_upload) : "";
  if (c.upload_state === "uploading") return ["Uploading now", "text-sky-300"];
  if (c.upload_state === "queued") return ["Up next", "text-indigo-300"];
  if (c.upload_state === "retrying") return [at ? `Retries ${at}` : "Retries later", "text-amber-300"];
  return [at || "Scheduled", "text-zinc-300"];
}

function UploadQueue({ queue, ready, loaded }: { queue: Clip[]; ready: number; loaded: boolean }) {
  return (
    <Panel title="Upload queue" link={{ href: "#/approved", label: "Open Upload" }}>
      {loaded && queue.length === 0 && (
        <Empty>
          Nothing is queued to upload.
          {ready > 0 && ` ${ready} clip${ready === 1 ? " is" : "s are"} ready: start ${ready === 1 ? "it" : "them"} from Upload.`}
        </Empty>
      )}
      {queue.length > 0 && (
        <ol className="divide-y divide-white/5 border-t border-white/5">
          {queue.slice(0, LIST_MAX).map((c) => {
            const [when, tone] = queueTime(c);
            return (
              <li key={c.id}>
                <a href={`#/upload/${c.id}`} className="flex items-center gap-3 px-4 py-2.5 hover:bg-white/[0.03]">
                  <img src={media.thumb(c)} alt="" loading="lazy" className="aspect-video w-20 shrink-0 rounded-md bg-zinc-800 object-cover" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-semibold">{c.title}</span>
                    <span className="block truncate text-xs text-zinc-500">
                      {KIND_LABEL[c.kind]}
                      {c.has_short && " + Short"} · {c.destination_name || "No channel chosen"}
                    </span>
                    {c.upload_state === "retrying" && c.upload_error && (
                      <span className="block truncate text-xs text-amber-300/80">{firstSentence(c.upload_error)}</span>
                    )}
                  </span>
                  <span className={cn("shrink-0 text-right text-xs font-semibold", tone)}>{when}</span>
                </a>
              </li>
            );
          })}
        </ol>
      )}
      <More count={queue.length - LIST_MAX} href="#/approved" label="on Upload" />
    </Panel>
  );
}

function InProgress({ vods, pauses, loaded }: { vods: Vod[]; pauses?: Pauses; loaded: boolean }) {
  return (
    <Panel title="Videos in progress" link={{ href: "#/videos", label: "Open Videos" }}>
      {loaded && vods.length === 0 && <Empty>No videos being worked on. Add one on Videos.</Empty>}
      {vods.length > 0 && (
        <ul className="divide-y divide-white/5 border-t border-white/5">
          {vods.slice(0, LIST_MAX).map((v) => {
            const held = v.status === "queued" && !!pauses && (v.downloaded ? pauses.processing : pauses.downloads);
            const details = [v.show_name, v.platform && platformName(v.platform), v.duration ? length(v.duration) : null, ago(v.created_at)]
              .filter(Boolean)
              .join(" · ");
            return (
              <li key={v.id}>
                <a href={`#/videos/${v.id}`} className="block px-4 py-2.5 hover:bg-white/[0.03]">
                  <span className="flex items-center gap-3">
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-semibold">{v.title || v.url}</span>
                      <span className="block truncate text-xs text-zinc-500">{details}</span>
                    </span>
                    <VodStatusChip status={v.status} paused={v.paused || held} />
                  </span>
                  {ACTIVE.includes(v.status) && !v.paused && !held && (
                    <span className="mt-2 block">
                      <ProgressBar value={WITH_PROGRESS.includes(v.status) ? v.progress : null} />
                    </span>
                  )}
                </a>
              </li>
            );
          })}
        </ul>
      )}
      <More count={vods.length - LIST_MAX} href="#/videos" label="on Videos" />
    </Panel>
  );
}

const uploadKind = (u: StatsUpload) => (u.which === "short" ? "Short" : KIND_LABEL[u.kind]);

/** The latest uploads' views, and the month's most viewed. */
function RecentUploads({ stats }: { stats?: Stats }) {
  const since = Date.now() - 30 * DAY_MS;
  const top = (stats?.uploads ?? [])
    .filter((u) => u.views !== null && u.finished_at && Date.parse(u.finished_at) >= since)
    .sort((a, b) => b.views! - a.views!)
    .slice(0, 3);
  return (
    <>
      <Panel title="Latest uploads" link={{ href: "#/stats", label: "Open Stats" }}>
        {stats?.setup_needed && <p className="mx-4 mb-3 rounded-xl bg-amber-500/10 p-3 text-xs text-amber-200">{stats.setup_needed}</p>}
        {stats && stats.uploads.length === 0 && <Empty>No uploads by the app yet.</Empty>}
        <UploadList uploads={stats?.uploads.slice(0, 6) ?? []} />
        {stats?.checked_at && <p className="border-t border-white/5 px-4 py-2.5 text-xs text-zinc-500">Views as of {ago(stats.checked_at)}</p>}
      </Panel>
      {top.length > 0 && (
        <Panel title="Most viewed, last 30 days">
          <UploadList uploads={top} />
        </Panel>
      )}
    </>
  );
}

function UploadList({ uploads }: { uploads: StatsUpload[] }) {
  if (!uploads.length) return null;
  return (
    <ul className="divide-y divide-white/5 border-t border-white/5">
      {uploads.map((u) => (
        <li key={u.job_id}>
          <a href={u.youtube_url} target="_blank" rel="noreferrer" className="flex items-center gap-3 px-4 py-2.5 hover:bg-white/[0.03]">
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-semibold">{u.title}</span>
              <span className="flex items-center gap-1 text-xs text-zinc-500">
                <LinkIcon className="size-3 shrink-0" />
                {uploadKind(u)}
                {u.finished_at && ` · ${ago(u.finished_at)}`}
              </span>
            </span>
            <span className="shrink-0 text-right">
              <span className="block text-sm font-bold tabular-nums">{u.views === null ? "–" : compact(u.views)}</span>
              <span className="block text-[11px] text-zinc-500">views</span>
            </span>
          </a>
        </li>
      ))}
    </ul>
  );
}

/** Each channel uploaded to, and how: the YouTube API, or YouTube Studio and Rumble through the upload browser. */
function Channels({ destinations }: { destinations?: Destination[] }) {
  if (!destinations) return null;
  const usesBrowser = destinations.some((d) => (d.youtube && !d.api_connected) || d.rumble);
  return (
    <Panel title="Channels you upload to" link={{ href: "#/shows/youtube", label: "Manage" }}>
      {destinations.length === 0 && <Empty>None yet. Add your clips channels under Shows.</Empty>}
      {destinations.length > 0 && (
        <ul className="divide-y divide-white/5 border-t border-white/5">
          {destinations.map((d) => {
            const how = [d.youtube && (d.api_connected ? "YouTube API" : "YouTube Studio"), d.rumble && "Rumble"].filter(Boolean).join(" and ");
            return (
              <li key={d.id} className="flex items-start gap-2.5 px-4 py-2.5">
                <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", d.api_error ? "bg-red-400" : "bg-emerald-400")} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-semibold">{d.name}</span>
                  <span className="block text-xs text-zinc-500">Through {how}</span>
                  {d.api_error && <span className="block break-words text-xs text-red-300">{d.api_error}</span>}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {usesBrowser && (
        <div className="border-t border-white/5 px-4 py-3">
          <UploadBrowserStatus />
        </div>
      )}
    </Panel>
  );
}
