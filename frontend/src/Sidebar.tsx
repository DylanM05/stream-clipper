import type { ComponentType } from "react";
import { api, type Summary } from "./api";
import { ChartIcon, FilmIcon, GearIcon, GridIcon, InboxIcon, LibraryIcon, TvIcon, UploadIcon } from "./icons";
import { cn, usePoll } from "./lib";
import { PauseBar, usePauses } from "./PauseBar";
import { DiskMeter } from "./ui";

/**
 * The desktop's navigation down the left, in place of the phone's tab bar: the same pages plus the overview,
 * with the switches that pause the work and how full the disk is always in view, and Settings below them.
 */
export function Sidebar({ screen, summary }: { screen: string; summary?: Summary }) {
  const { pauses, setPauses } = usePauses();
  const { data: storage } = usePoll(api.storage, 120000);
  const working = summary?.videos_in_progress ?? 0;

  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 flex-col overflow-y-auto border-r border-white/5 bg-zinc-950 lg:flex">
      <a href="#/overview" className="flex items-center gap-2.5 px-5 pb-5 pt-6">
        <img src="/icon-192.png" alt="" className="size-8 rounded-lg" />
        <span className="font-bold tracking-tight">Stream Clipper</span>
      </a>
      <nav className="space-y-0.5 px-3" aria-label="Pages">
        <SideLink to="overview" label="Overview" icon={GridIcon} active={screen === "overview"} />
        <SideLink to="inbox" label="Inbox" icon={InboxIcon} active={screen === "inbox"} badge={summary?.to_review} />
        <SideLink
          to="videos"
          label="Videos"
          icon={FilmIcon}
          active={screen === "videos"}
          badge={working}
          quiet
          hint={`${working} video${working === 1 ? "" : "s"} being worked on`}
        />
        <SideLink to="library" label="Library" icon={LibraryIcon} active={screen === "library"} />
        <SideLink
          to="approved"
          label="Upload"
          icon={UploadIcon}
          active={screen === "approved" || screen === "upload"}
          badge={summary?.to_upload}
        />
        <SideLink to="stats" label="Stats" icon={ChartIcon} active={screen === "stats"} />
        <SideLink to="shows" label="Shows" icon={TvIcon} active={screen === "shows"} />
      </nav>

      <div className="mt-auto space-y-5 px-4 pt-8">
        {pauses && (
          <section>
            <h2 className="mb-2 px-1 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Work</h2>
            <PauseBar compact parts={["downloads", "processing", "renders", "uploads"]} pauses={pauses} onChange={setPauses} />
          </section>
        )}
        {storage && (
          <section className="px-1">
            <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Disk</h2>
            <DiskMeter storage={storage} />
          </section>
        )}
      </div>
      <nav className="px-3 pb-6 pt-3" aria-label="Settings">
        <SideLink to="settings" label="Settings" icon={GearIcon} active={screen === "settings"} />
      </nav>
    </aside>
  );
}

function SideLink(props: {
  to: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  active: boolean;
  badge?: number;
  quiet?: boolean; // a count to know about, rather than one waiting for you
  hint?: string;
}) {
  const Icon = props.icon;
  return (
    <a
      href={`#/${props.to}`}
      aria-current={props.active ? "page" : undefined}
      title={props.badge ? props.hint : undefined}
      className={cn(
        "flex items-center gap-3 rounded-xl px-3 py-2 text-sm font-medium",
        props.active ? "bg-violet-500/10 text-violet-200" : "text-zinc-400 hover:bg-white/5 hover:text-zinc-200",
      )}
    >
      <Icon className={cn("size-5 shrink-0", props.active ? "text-violet-300" : "text-zinc-500")} />
      <span className="flex-1">{props.label}</span>
      {!!props.badge && (
        <span
          className={cn(
            "min-w-5 rounded-full px-1.5 text-center text-[11px] font-bold leading-5 tabular-nums",
            props.quiet ? "bg-zinc-800 text-zinc-300" : "bg-violet-500 text-white",
          )}
        >
          {props.badge}
        </span>
      )}
    </a>
  );
}
