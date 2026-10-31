import { useState } from "react";
import { api, type Clip, type Show, type StatsUpload } from "../api";
import { LinkIcon } from "../icons";
import { ago, cn, compact, loadSetting, median, pastDay, saveSetting, total, useDesktop, usePoll } from "../lib";
import { ErrorNote, KIND_LABEL, Screen, StatTile } from "../ui";

type Kind = "all" | Clip["kind"];
const KINDS: Record<Kind, string> = { all: "All", chapter: "Clips", highlight: "Highlights", whole: "Full episodes" };

/** How the list is sorted. A phone switches between the first two; a desktop's table sorts by any of its numbers. */
type Order = "views" | "newest" | "score" | "views_day1" | "views_week1" | "likes" | "comments";
const ORDER_LABEL: Record<Order, string> = {
  views: "Most viewed",
  newest: "Newest",
  score: "Highest score",
  views_day1: "Most on day 1",
  views_week1: "Most in week 1",
  likes: "Most liked",
  comments: "Most comments",
};

/**
 * How uploads are doing on YouTube: whether the AI's score predicts views (which is what Standouts relies on),
 * and every upload's numbers. The worker reads them every few hours for each upload's first month.
 */
export function Stats() {
  const desktop = useDesktop();
  const [showId, setShowId] = useState<number | null>(() => loadSetting("clipper.statsShow", null));
  const [kind, setKind] = useState<Kind>("all");
  const [order, setOrder] = useState<Order>("views");
  const { data: shows } = usePoll(api.shows, 0);
  const { data: stats, error } = usePoll(() => api.stats(showId), 60000, showId);

  const pickShow = (id: number | null) => {
    setShowId(id);
    saveSetting("clipper.statsShow", id);
  };
  const uploads = (stats?.uploads ?? [])
    .filter((u) => kind === "all" || u.kind === kind)
    // The server sends them newest first.
    .sort((a, b) => (order === "newest" ? 0 : (b[order] ?? -1) - (a[order] ?? -1)));
  const subtitle = stats?.checked_at ? `Views as of ${ago(stats.checked_at)}` : "How your uploads are doing on YouTube.";

  if (desktop) {
    return (
      <Screen title="Stats" subtitle={subtitle} wide>
        {error && <ErrorNote>{error}</ErrorNote>}
        {stats?.setup_needed && <p className="mb-4 rounded-xl bg-amber-500/10 p-3 text-sm text-amber-200">{stats.setup_needed}</p>}
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <ShowPicker value={showId} shows={shows} onChange={pickShow} className="w-72" />
          <KindTabs value={kind} onChange={setKind} className="flex gap-1 rounded-xl bg-zinc-800 p-1" />
        </div>
        {stats && <Totals uploads={uploads} />}
        {stats && (
          <div className="my-6 grid items-start gap-6 xl:grid-cols-2">
            <ScoreBands bands={stats.bands} />
            <KindBreakdown uploads={stats.uploads} />
          </div>
        )}
        <WhatTheAiLearns showId={showId} className="mb-6" />
        {stats && uploads.length === 0 && <p className="mt-6 text-center text-sm text-zinc-500">No uploads by the app yet.</p>}
        {uploads.length > 0 && <UploadTable uploads={uploads} order={order} onOrder={setOrder} />}
      </Screen>
    );
  }

  return (
    <Screen title="Stats" subtitle={subtitle}>
      {error && <ErrorNote>{error}</ErrorNote>}
      {stats?.setup_needed && <p className="mb-4 rounded-xl bg-amber-500/10 p-3 text-sm text-amber-200">{stats.setup_needed}</p>}

      <ShowPicker value={showId} shows={shows} onChange={pickShow} className="mb-4 w-full" />

      {stats && <ScoreBands bands={stats.bands} />}
      <WhatTheAiLearns showId={showId} className="mt-6" />

      <div className="mb-3 mt-6 flex items-center gap-2">
        <KindTabs value={kind} onChange={setKind} className="flex flex-1 gap-1 overflow-x-auto rounded-xl bg-zinc-800 p-1" />
        <button
          onClick={() => setOrder(order === "views" ? "newest" : "views")}
          className="shrink-0 rounded-xl bg-zinc-800 px-3 py-2 text-xs font-semibold text-zinc-300 active:bg-zinc-700"
        >
          {ORDER_LABEL[order]}
        </button>
      </div>

      {stats && uploads.length === 0 && <p className="mt-6 text-center text-sm text-zinc-500">No uploads by the app yet.</p>}
      <ul className="space-y-2">
        {uploads.map((u) => (
          <UploadRow key={u.job_id} upload={u} />
        ))}
      </ul>
    </Screen>
  );
}

/** What the show's past clips teach the AI, exactly as it's given with the show's next stream (learning.py). */
function WhatTheAiLearns({ showId, className }: { showId: number | null; className: string }) {
  const { data } = usePoll(() => (showId === null ? Promise.resolve(null) : api.trackRecord(showId)), 0, showId);
  if (showId === null || data === undefined) return null;
  return (
    <section className={cn("rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5", className)}>
      <h2 className="text-sm font-bold">What the AI learns from this show's past clips</h2>
      <p className="mt-1 text-xs text-zinc-500">
        What it's given with each new stream of the show: how the uploads did, how you changed or turned down its picks,
        and your notes from the show's settings.
      </p>
      {data?.text ? (
        <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-zinc-300">
          {data.text}
        </pre>
      ) : (
        <p className="mt-3 text-sm text-zinc-500">
          Nothing yet. It needs a few uploads with views, or picks you've changed or turned down.
        </p>
      )}
    </section>
  );
}

function ShowPicker(props: { value: number | null; shows?: Show[]; onChange: (id: number | null) => void; className: string }) {
  return (
    <select
      value={props.value ?? ""}
      onChange={(e) => props.onChange(e.target.value ? Number(e.target.value) : null)}
      className={cn(
        "appearance-none rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 focus:ring-2 focus:ring-violet-500",
        props.className,
      )}
    >
      <option value="">Every show</option>
      {props.shows?.map((s) => (
        <option key={s.id} value={s.id}>
          {s.name}
        </option>
      ))}
    </select>
  );
}

function KindTabs({ value, onChange, className }: { value: Kind; onChange: (k: Kind) => void; className: string }) {
  return (
    <div className={className}>
      {(Object.keys(KINDS) as Kind[]).map((k) => (
        <button
          key={k}
          onClick={() => onChange(k)}
          className={cn(
            "shrink-0 rounded-lg px-2.5 py-1.5 text-xs font-semibold",
            value === k ? "bg-zinc-600 text-white" : "text-zinc-400 hover:text-zinc-200",
          )}
        >
          {KINDS[k]}
        </button>
      ))}
    </div>
  );
}

const counted = (n: number | null) => (n === null ? "–" : compact(Math.round(n)));
const uploadKind = (u: StatsUpload) => (u.which === "short" ? "Short" : KIND_LABEL[u.kind]);

/** The desktop's headline numbers, for the show and kind picked. */
function Totals({ uploads }: { uploads: StatsUpload[] }) {
  const shorts = uploads.filter((u) => u.which === "short").length;
  return (
    <div className="grid grid-cols-3 gap-3 xl:grid-cols-6">
      <StatTile label="Uploads" value={uploads.length} note={shorts ? `${shorts} of them Shorts` : "On YouTube, by the app"} />
      <StatTile label="Views" value={counted(total(uploads.map((u) => u.views)))} note="All of them, so far" />
      <StatTile label="Median views" value={counted(median(uploads.map((u) => u.views)))} note="Per upload, so far" />
      <StatTile label="Median on day 1" value={counted(median(uploads.map((u) => u.views_day1)))} note="Views in the first 24 hours" />
      <StatTile label="Median in week 1" value={counted(median(uploads.map((u) => u.views_week1)))} note="Views in the first 7 days" />
      <StatTile
        label="Likes"
        value={counted(total(uploads.map((u) => u.likes)))}
        note={`${counted(total(uploads.map((u) => u.comments)))} comments`}
      />
    </div>
  );
}

const KIND_GROUPS: { label: string; match: (u: StatsUpload) => boolean }[] = [
  { label: "Clips", match: (u) => u.kind === "chapter" && u.which === "video" },
  { label: "Highlights", match: (u) => u.kind === "highlight" && u.which === "video" },
  { label: "Shorts", match: (u) => u.which === "short" },
  { label: "Full episodes", match: (u) => u.kind === "whole" && u.which === "video" },
];

/** Which kinds of upload do best: median views of each, the same way the score bands are read, and their total. */
function KindBreakdown({ uploads }: { uploads: StatsUpload[] }) {
  const groups = KIND_GROUPS.map((g) => ({ label: g.label, uploads: uploads.filter(g.match) })).filter((g) => g.uploads.length);
  return (
    <section className="rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
      <h2 className="text-sm font-bold">Which kind does best?</h2>
      <p className="mt-1 text-xs text-zinc-500">Median views of each kind of upload, and all its views together.</p>
      {groups.length === 0 ? (
        <p className="mt-3 text-sm text-zinc-500">No uploads yet.</p>
      ) : (
        <table className="mt-3 w-full text-sm tabular-nums">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-wide text-zinc-500">
              <th className="pb-1.5 font-semibold">Kind</th>
              <th className="pb-1.5 text-right font-semibold">Day 1</th>
              <th className="pb-1.5 text-right font-semibold">Week 1</th>
              <th className="pb-1.5 text-right font-semibold">So far</th>
              <th className="pb-1.5 text-right font-semibold">Total</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.label} className="border-t border-white/5">
                <td className="py-2">
                  {g.label}
                  <span className="ml-1.5 text-xs text-zinc-500">
                    {g.uploads.length} video{g.uploads.length === 1 ? "" : "s"}
                  </span>
                </td>
                {[
                  median(g.uploads.map((u) => u.views_day1)),
                  median(g.uploads.map((u) => u.views_week1)),
                  median(g.uploads.map((u) => u.views)),
                  total(g.uploads.map((u) => u.views)),
                ].map((v, i) => (
                  <td key={i} className="py-2 text-right">
                    {v === null ? <span className="text-zinc-600">–</span> : compact(Math.round(v))}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** Every upload as a row, sorted by whichever column's heading was clicked. */
function UploadTable({ uploads, order, onOrder }: { uploads: StatsUpload[]; order: Order; onOrder: (o: Order) => void }) {
  const sortBy = (key: Order, label: string) => (
    <th className="px-3 py-3 text-right font-semibold" aria-sort={order === key ? "descending" : "none"}>
      <button
        onClick={() => onOrder(key)}
        title={ORDER_LABEL[key]}
        className={cn(
          "inline-flex items-center gap-1 whitespace-nowrap uppercase tracking-wide",
          order === key ? "text-zinc-100" : "hover:text-zinc-300",
        )}
      >
        {label}
        <span aria-hidden="true" className={order === key ? "" : "invisible"}>
          ↓
        </span>
      </button>
    </th>
  );
  const number = (v: number | null, strong = false) => (
    <td className={cn("whitespace-nowrap px-3 py-2.5 text-right tabular-nums", strong ? "font-bold" : "text-zinc-300")}>
      {v === null ? <span className="text-zinc-600">–</span> : compact(v)}
    </td>
  );

  return (
    <div className="overflow-x-auto rounded-2xl bg-zinc-900 ring-1 ring-white/5">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-zinc-500">
            <th className="px-4 py-3 font-semibold">Upload</th>
            <th className="px-3 py-3 font-semibold">Kind</th>
            {sortBy("score", "Score")}
            {sortBy("newest", "Posted")}
            {sortBy("views_day1", "Day 1")}
            {sortBy("views_week1", "Week 1")}
            {sortBy("views", "Views")}
            {sortBy("likes", "Likes")}
            {sortBy("comments", "Comments")}
          </tr>
        </thead>
        <tbody>
          {uploads.map((u) => (
            <tr key={u.job_id} className="border-t border-white/5 hover:bg-white/[0.02]">
              <td className="w-full max-w-0 px-4 py-2.5">
                <a href={u.youtube_url} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 hover:text-violet-200" title={u.title}>
                  <span className="truncate font-semibold">{u.title}</span>
                  <LinkIcon className="size-3.5 shrink-0 text-zinc-500" />
                </a>
              </td>
              <td className="whitespace-nowrap px-3 py-2.5 text-zinc-400">{uploadKind(u)}</td>
              <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums text-zinc-300">
                {u.kind === "whole" || u.manual ? <span className="text-zinc-600">–</span> : u.score.toFixed(2)}
              </td>
              <td className="whitespace-nowrap px-3 py-2.5 text-right text-zinc-400" title={u.finished_at ? ago(u.finished_at) : undefined}>
                {u.finished_at ? pastDay(u.finished_at, false) : "–"}
              </td>
              {number(u.views_day1)}
              {number(u.views_week1)}
              {number(u.views, true)}
              {number(u.likes)}
              {number(u.comments)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Median views by the AI's score: if the top band doesn't beat the rest, the score (and Standouts) isn't telling much. */
function ScoreBands({ bands }: { bands: { label: string; count: number; day1: number | null; week1: number | null; views: number | null }[] }) {
  const counted = bands.filter((b) => b.count > 0);
  return (
    <section className="rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
      <h2 className="text-sm font-bold">Does the AI's score predict views?</h2>
      <p className="mt-1 text-xs text-zinc-500">Median views of clips and highlights, by the score the AI gave them.</p>
      {counted.length === 0 ? (
        <p className="mt-3 text-sm text-zinc-500">Not enough uploads with views yet.</p>
      ) : (
        <table className="mt-3 w-full text-sm tabular-nums">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-wide text-zinc-500">
              <th className="pb-1.5 font-semibold">Score</th>
              <th className="pb-1.5 text-right font-semibold">Day 1</th>
              <th className="pb-1.5 text-right font-semibold">Week 1</th>
              <th className="pb-1.5 text-right font-semibold">So far</th>
            </tr>
          </thead>
          <tbody>
            {bands.map((b) => (
              <tr key={b.label} className="border-t border-white/5">
                <td className="py-2">
                  {b.label}
                  <span className="ml-1.5 text-xs text-zinc-500">
                    {b.count} video{b.count === 1 ? "" : "s"}
                  </span>
                </td>
                {[b.day1, b.week1, b.views].map((v, i) => (
                  <td key={i} className="py-2 text-right">
                    {v === null ? <span className="text-zinc-600">–</span> : compact(v)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function UploadRow({ upload: u }: { upload: StatsUpload }) {
  return (
    <li className="rounded-2xl bg-zinc-900 p-3 ring-1 ring-white/5">
      <div className="flex items-start gap-3">
        <a href={u.youtube_url} target="_blank" rel="noreferrer" className="min-w-0 flex-1">
          <p className="line-clamp-2 text-sm font-semibold leading-snug">{u.title}</p>
          <p className="mt-1 flex items-center gap-1 text-xs text-zinc-500">
            <LinkIcon className="size-3.5 shrink-0" />
            {uploadKind(u)}
            {u.kind !== "whole" && !u.manual && ` · score ${u.score.toFixed(2)}`}
            {u.finished_at && ` · ${ago(u.finished_at)}`}
          </p>
        </a>
        <div className="shrink-0 text-right tabular-nums">
          <p className="text-lg font-bold leading-tight">{u.views === null ? "–" : compact(u.views)}</p>
          <p className="text-[11px] text-zinc-500">views</p>
        </div>
      </div>
      {(u.views_day1 !== null || u.likes !== null || u.comments !== null) && (
        <p className="mt-2 text-xs tabular-nums text-zinc-400">
          {[
            u.views_day1 !== null && `${compact(u.views_day1)} on day 1`,
            u.views_week1 !== null && `${compact(u.views_week1)} in week 1`,
            u.likes !== null && `${compact(u.likes)} likes`,
            u.comments !== null && `${compact(u.comments)} comments`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
      )}
    </li>
  );
}
