import { useState } from "react";
import { api, type Clip, type StatsUpload } from "../api";
import { LinkIcon } from "../icons";
import { ago, cn, compact, loadSetting, saveSetting, usePoll } from "../lib";
import { ErrorNote, KIND_LABEL, Screen } from "../ui";

type Kind = "all" | Clip["kind"];
const KINDS: Record<Kind, string> = { all: "All", chapter: "Chapters", highlight: "Highlights", whole: "Full episodes" };

/**
 * How uploads are doing on YouTube: whether Claude's score predicts views (which is what Standouts relies on),
 * and every upload's numbers. The worker reads them every few hours for each upload's first month.
 */
export function Stats() {
  const [showId, setShowId] = useState<number | null>(() => loadSetting("clipper.statsShow", null));
  const [kind, setKind] = useState<Kind>("all");
  const [order, setOrder] = useState<"views" | "newest">("views");
  const { data: shows } = usePoll(api.shows, 0);
  const { data: stats, error } = usePoll(() => api.stats(showId), 60000, showId);

  const pickShow = (id: number | null) => {
    setShowId(id);
    saveSetting("clipper.statsShow", id);
  };
  const uploads = (stats?.uploads ?? [])
    .filter((u) => kind === "all" || u.kind === kind)
    .sort((a, b) => (order === "views" ? (b.views ?? -1) - (a.views ?? -1) : 0));

  return (
    <Screen title="Stats" subtitle={stats?.checked_at ? `Views as of ${ago(stats.checked_at)}` : "How your uploads are doing on YouTube."}>
      {error && <ErrorNote>{error}</ErrorNote>}
      {stats?.setup_needed && <p className="mb-4 rounded-xl bg-amber-500/10 p-3 text-sm text-amber-200">{stats.setup_needed}</p>}

      <select
        value={showId ?? ""}
        onChange={(e) => pickShow(e.target.value ? Number(e.target.value) : null)}
        className="mb-4 w-full appearance-none rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 focus:ring-2 focus:ring-violet-500"
      >
        <option value="">Every show</option>
        {shows?.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}
          </option>
        ))}
      </select>

      {stats && <ScoreBands bands={stats.bands} />}

      <div className="mb-3 mt-6 flex items-center gap-2">
        <div className="flex flex-1 gap-1 overflow-x-auto rounded-xl bg-zinc-800 p-1">
          {(Object.keys(KINDS) as Kind[]).map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              className={cn("shrink-0 rounded-lg px-2.5 py-1.5 text-xs font-semibold", kind === k ? "bg-zinc-600 text-white" : "text-zinc-400")}
            >
              {KINDS[k]}
            </button>
          ))}
        </div>
        <button
          onClick={() => setOrder(order === "views" ? "newest" : "views")}
          className="shrink-0 rounded-xl bg-zinc-800 px-3 py-2 text-xs font-semibold text-zinc-300 active:bg-zinc-700"
        >
          {order === "views" ? "Most viewed" : "Newest"}
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

/** Median views by Claude's score: if the top band doesn't beat the rest, the score (and Standouts) isn't telling much. */
function ScoreBands({ bands }: { bands: { label: string; count: number; day1: number | null; week1: number | null; views: number | null }[] }) {
  const counted = bands.filter((b) => b.count > 0);
  return (
    <section className="rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
      <h2 className="text-sm font-bold">Does Claude's score predict views?</h2>
      <p className="mt-1 text-xs text-zinc-500">Median views of chapters and highlights, by the score Claude gave them.</p>
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
            {u.which === "short" ? "Short" : KIND_LABEL[u.kind]}
            {u.kind !== "whole" && ` · score ${u.score.toFixed(2)}`}
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
