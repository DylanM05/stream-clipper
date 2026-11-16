import type { ReactNode } from "react";
import { api, type Clip, type Destination, type Pauses, type Stats, type StatsUpload, type Storage, type Summary, type Vod } from "../api";
import { Thumb } from "../ClipCard";
import { LinkIcon } from "../icons";
import { ago, cn, compact, firstSentence, length, platformName, total, upcoming, usePoll } from "../lib";
import { usePauses, WorkStrip } from "../Work";
import { ErrorNote, KIND_LABEL, Panel, ProgressBar, Screen, VodStatusChip } from "../ui";
import { GOING_UP, inUploadOrder } from "./Approved";
import { LibraryPanel } from "./Library";
import { UploadBrowserStatus } from "./UploadKit";
import { ACTIVE, WITH_PROGRESS, waitingText } from "./Videos";

const DAY_MS = 86400000;
const LIST_MAX = 8; // rows a panel shows before "and N more"

/**
 * Everything at a glance: the work, a part at a time, then what needs you (clips to review, ones ready to upload,
 * failures, videos held up, a full disk), what goes up next and when, what's being worked on, how recent uploads
 * are doing, and how each channel uploads.
 */
export function Overview({ summary }: { summary?: Summary }) {
  const { data: clips, error } = usePoll(() => api.clips(["approved", "rendering", "failed", "rendered", "uploaded"]), 10000);
  const { data: vods } = usePoll(api.vods, 10000);
  const { data: stats } = usePoll(() => api.stats(null), 60000);
  const { data: destinations } = usePoll(api.destinations, 60000);
  const { data: storage } = usePoll(api.storage, 120000);
  const { pauses } = usePauses();

  const all = clips ?? [];
  const ready = all.filter((c) => c.status === "rendered" && !GOING_UP.includes(c.upload_state) && c.upload_state !== "failed");
  const queue = all
    .filter((c) => (c.status === "rendered" || c.status === "uploaded") && GOING_UP.includes(c.upload_state))
    .sort(inUploadOrder);
  const working = (vods ?? []).filter((v) => v.status !== "ready" && v.status !== "failed");

  return (
    <Screen title="Overview" subtitle="What's running and what needs you." wide>
      {error && <ErrorNote>{error}</ErrorNote>}
      {/* Across the page on a desktop; one above the other, the compact way, on a phone. */}
      <WorkStrip summary={summary} className="mb-8 hidden lg:grid" />
      <WorkStrip summary={summary} stacked className="mb-8 rounded-2xl bg-panel p-1.5 lg:hidden" />

      <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="min-w-0 space-y-8">
          <NeedsYou summary={summary} clips={all} ready={ready.length} vods={vods ?? []} storage={storage} loaded={!!clips && !!vods} />
          <UploadQueue queue={queue} ready={ready.length} loaded={!!clips} />
          <InProgress vods={working} pauses={pauses} loaded={!!vods} />
        </div>
        <div className="min-w-0 space-y-8">
          <RecentUploads stats={stats} />
          <LibraryPanel />
          <Channels destinations={destinations} />
        </div>
      </div>
    </Screen>
  );
}

function More({ count, href, label }: { count: number; href: string; label?: string }) {
  if (count <= 0) return null;
  return (
    <a href={href} className="block py-2.5 text-[13px] font-semibold text-graphite hover:text-chalk">
      and {count} more{label && ` ${label}`}
    </a>
  );
}

const Empty = ({ children }: { children: ReactNode }) => <p className="py-3 text-sm text-graphite">{children}</p>;

/** A row in one of the overview's lists: it opens what it's about. */
const ROW = "-mx-2 flex items-center gap-3 rounded-lg px-2 py-3 hover:bg-white/[0.03]";

type Need = { key: string; tone: "todo" | "bad" | "warn"; title: string; detail: string; href: string; action: string };

const DOT: Record<Need["tone"], string> = { todo: "bg-mark", bad: "bg-tally", warn: "bg-amber-400" };

/**
 * What's waiting on you, most pressing first: failures, then what's held up, then clips to review and to upload.
 * Each says what's wrong or waiting and has a button to the page that deals with it.
 */
function NeedsYou(props: { summary?: Summary; clips: Clip[]; ready: number; vods: Vod[]; storage?: Storage; loaded: boolean }) {
  const { summary, clips, ready, vods, storage } = props;
  const toReview = summary?.to_review ?? 0;
  const needs: Need[] = [
    ...clips
      .filter((c) => c.upload_state === "failed")
      .map((c): Need => ({
        key: `u${c.id}`,
        tone: "bad",
        title: c.title,
        detail: `Upload failed${c.destination_name ? ` to ${c.destination_name}` : ""}${c.upload_error ? `: ${firstSentence(c.upload_error)}` : "."}`,
        href: `#/upload/${c.id}`,
        action: "Try again",
      })),
    ...clips
      .filter((c) => c.status === "failed")
      .map((c): Need => ({ key: `r${c.id}`, tone: "bad", title: c.title, detail: `Render failed: ${c.error}`, href: `#/clip/${c.id}`, action: "Open" })),
    ...vods
      .filter((v) => v.status === "failed")
      .map((v): Need => ({ key: `v${v.id}`, tone: "bad", title: v.title || v.url, detail: `Failed: ${v.error}`, href: `#/videos/${v.id}`, action: "Open" })),
    ...(storage?.free_gb != null && storage.free_gb < storage.min_free_gb
      ? [{
          key: "disk",
          tone: "bad" as const,
          title: "Disk nearly full",
          detail: `${storage.free_gb} GB free. New downloads wait until ${storage.min_free_gb} GB is free.`,
          href: "#/videos",
          action: "Free up space",
        }]
      : []),
    ...vods
      .filter((v) => v.status === "waiting")
      .map((v): Need => ({ key: `w${v.id}`, tone: "warn", title: v.title || v.url, detail: waitingText(v), href: `#/videos/${v.id}`, action: "Open" })),
    ...(toReview
      ? [{ key: "review", tone: "todo" as const, title: `${toReview} clip${toReview === 1 ? "" : "s"} to review`, detail: "Waiting in the inbox", href: "#/inbox", action: "Review" }]
      : []),
    ...(ready
      ? [{
          key: "ready",
          tone: "todo" as const,
          title: `${ready} clip${ready === 1 ? "" : "s"} ready to upload`,
          detail: "Rendered, with the upload not started",
          href: "#/approved",
          action: "Upload",
        }]
      : []),
  ];

  return (
    <Panel title="Needs you" note={needs.length ? String(needs.length) : undefined}>
      {props.loaded && needs.length === 0 && <Empty>Nothing needs you right now.</Empty>}
      {needs.length > 0 && (
        <ul className="divide-y divide-line">
          {needs.slice(0, LIST_MAX).map((n) => (
            <li key={n.key}>
              <a href={n.href} className={ROW}>
                <span className={cn("size-2 shrink-0 rounded-full", DOT[n.tone])} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-semibold">{n.title}</span>
                  <span className={cn("line-clamp-2 break-words text-[13px]", n.tone === "warn" ? "text-amber-300" : "text-graphite")}>
                    {n.detail}
                  </span>
                </span>
                <span className="shrink-0 rounded-lg bg-panel-2 px-3 py-1.5 text-[13px] font-semibold">{n.action}</span>
              </a>
            </li>
          ))}
        </ul>
      )}
      <More count={needs.length - LIST_MAX} href="#/approved" />
    </Panel>
  );
}

/** When each started upload goes, in order. */
function queueTime(c: Clip): [string, string] {
  const at = c.next_upload ? upcoming(c.next_upload) : "";
  if (c.upload_state === "uploading") return ["Uploading now", "text-mark"];
  if (c.upload_state === "queued") return ["Up next", "text-chalk"];
  if (c.upload_state === "retrying") return [at ? `Retries ${at}` : "Retries later", "text-amber-300"];
  return [at || "Scheduled", "text-chalk"];
}

function UploadQueue({ queue, ready, loaded }: { queue: Clip[]; ready: number; loaded: boolean }) {
  return (
    <Panel title="Upload queue" note={queue.length ? String(queue.length) : undefined} link={{ href: "#/approved", label: "Open Upload" }}>
      {loaded && queue.length === 0 && (
        <Empty>
          Nothing is queued to upload.
          {ready > 0 && ` ${ready} clip${ready === 1 ? " is" : "s are"} ready: start ${ready === 1 ? "it" : "them"} from Upload.`}
        </Empty>
      )}
      {queue.length > 0 && (
        <ol className="divide-y divide-line">
          {queue.slice(0, LIST_MAX).map((c) => {
            const [when, tone] = queueTime(c);
            return (
              <li key={c.id}>
                <a href={`#/upload/${c.id}`} className={ROW}>
                  <span className="block aspect-video w-20 shrink-0 overflow-hidden rounded-md bg-panel">
                    <Thumb clip={c} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-semibold">{c.title}</span>
                    <span className="block truncate text-[13px] text-graphite">
                      {KIND_LABEL[c.kind]}
                      {c.has_short && " + Short"} to {c.destination_name || "no channel yet"}
                    </span>
                    {c.upload_state === "retrying" && c.upload_error && (
                      <span className="block truncate text-xs text-amber-300/80">{firstSentence(c.upload_error)}</span>
                    )}
                  </span>
                  <span className={cn("shrink-0 text-right text-[13px] font-semibold", tone)}>{when}</span>
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
    <Panel title="Videos in progress" note={vods.length ? String(vods.length) : undefined} link={{ href: "#/videos", label: "Open Videos" }}>
      {loaded && vods.length === 0 && <Empty>No videos being worked on. Add one on Videos.</Empty>}
      {vods.length > 0 && (
        <ul className="divide-y divide-line">
          {vods.slice(0, LIST_MAX).map((v) => {
            const held = v.status === "queued" && !!pauses && (v.downloaded ? pauses.processing : pauses.downloads);
            const details = [v.show_name, v.platform && platformName(v.platform), v.duration ? `${length(v.duration)} long` : null, `Added ${ago(v.created_at)}`]
              .filter(Boolean);
            return (
              <li key={v.id}>
                <a href={`#/videos/${v.id}`} className="-mx-2 block rounded-lg px-2 py-3 hover:bg-white/[0.03]">
                  <span className="flex items-center gap-3">
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-semibold">{v.title || v.url}</span>
                      <span className="flex flex-wrap gap-x-3 text-[13px] text-graphite">
                        {details.map((d) => (
                          <span key={d as string}>{d}</span>
                        ))}
                      </span>
                    </span>
                    <VodStatusChip status={v.status} paused={v.paused || held} />
                  </span>
                  {ACTIVE.includes(v.status) && !v.paused && !held && (
                    <span className="mt-2 block max-w-md">
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

/** The latest uploads' views, with the week's count beside the title, and the month's most viewed. */
function RecentUploads({ stats }: { stats?: Stats }) {
  const now = Date.now();
  const top = (stats?.uploads ?? [])
    .filter((u) => u.views !== null && u.finished_at && Date.parse(u.finished_at) >= now - 30 * DAY_MS)
    .sort((a, b) => b.views! - a.views!)
    .slice(0, 3);
  const week = (stats?.uploads ?? []).filter((u) => u.finished_at && Date.parse(u.finished_at) >= now - 7 * DAY_MS);
  const weekViews = total(week.map((u) => u.views));
  const note = stats && `${week.length} this week${weekViews !== null ? `, ${compact(weekViews)} views` : ""}`;
  return (
    <>
      <Panel title="Latest uploads" note={note} link={{ href: "#/stats", label: "Stats" }}>
        {stats?.setup_needed && <p className="mt-3 rounded-xl bg-amber-500/10 p-3 text-xs text-amber-200">{stats.setup_needed}</p>}
        {stats && stats.uploads.length === 0 && <Empty>No uploads by the app yet.</Empty>}
        <UploadList uploads={stats?.uploads.slice(0, 6) ?? []} />
        {stats?.checked_at && <p className="pt-2.5 text-xs text-graphite">Views as of {ago(stats.checked_at)}</p>}
      </Panel>
      {top.length > 0 && (
        <Panel title="Most viewed" note="Last 30 days">
          <UploadList uploads={top} />
        </Panel>
      )}
    </>
  );
}

function UploadList({ uploads }: { uploads: StatsUpload[] }) {
  if (!uploads.length) return null;
  return (
    <ul className="divide-y divide-line border-b border-line">
      {uploads.map((u) => (
        <li key={u.job_id}>
          <a href={u.youtube_url} target="_blank" rel="noreferrer" className={ROW}>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-semibold">{u.title}</span>
              <span className="flex items-center gap-1.5 text-[13px] text-graphite">
                <LinkIcon className="size-3 shrink-0" />
                {uploadKind(u)}
                {u.finished_at && <span className="ml-1.5">{ago(u.finished_at)}</span>}
              </span>
            </span>
            <span className="shrink-0 text-right">
              <span className="block font-mono text-sm">{u.views === null ? "–" : compact(u.views)}</span>
              <span className="block text-[11px] text-graphite">views</span>
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
    <Panel title="Channels" link={{ href: "#/shows/youtube", label: "Manage" }}>
      {destinations.length === 0 && <Empty>None yet. Add your clips channels under Shows.</Empty>}
      {destinations.length > 0 && (
        <ul className="divide-y divide-line">
          {destinations.map((d) => {
            const how = [d.youtube && (d.api_connected ? "YouTube API" : "YouTube Studio"), d.rumble && "Rumble"].filter(Boolean).join(" and ");
            return (
              <li key={d.id} className="flex items-start gap-2.5 py-3">
                <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", d.api_error ? "bg-tally" : "bg-cue")} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-semibold">{d.name}</span>
                  <span className="block text-[13px] text-graphite">Through {how}</span>
                  {d.api_error && <span className="block break-words text-xs text-red-300">{d.api_error}</span>}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {usesBrowser && (
        <div className="border-t border-line pt-3">
          <UploadBrowserStatus />
        </div>
      )}
    </Panel>
  );
}
