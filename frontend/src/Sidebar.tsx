import type { ComponentType } from "react";
import { api, type Summary } from "./api";
import { ChartIcon, FilmIcon, GearIcon, GridIcon, InboxIcon, LibraryIcon, TvIcon, UploadIcon } from "./icons";
import { cn, usePoll } from "./lib";
import { DiskMeter } from "./ui";
import { WorkStrip } from "./Work";

/**
 * The desktop's navigation down the left, in place of the phone's tab bar: the same pages plus the overview,
 * with the work strip (what's running, with its pause switches) and how full the disk is always in view, and
 * Settings below them.
 */
export function Sidebar({ screen, summary }: { screen: string; summary?: Summary }) {
  const { data: storage } = usePoll(api.storage, 120000);
  const working = summary?.videos_in_progress ?? 0;

  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 flex-col overflow-y-auto border-r border-line bg-[#1b1c1f] lg:flex">
      <a href="#/overview" className="flex items-center gap-2.5 px-6 pb-6 pt-6">
        {/* A tally light: the app's mark. */}
        <span className="size-3 rounded-[3px] bg-tally ring-[3px] ring-tally/15" />
        <span className="text-lg font-bold font-stretch-condensed">Stream Clipper</span>
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
        <SideLink
          to="approved"
          label="Upload"
          icon={UploadIcon}
          active={screen === "approved" || screen === "upload"}
          badge={summary?.to_upload}
        />
        <SideLink to="shows" label="Shows" icon={TvIcon} active={screen === "shows"} />
        <SideLink to="library" label="Library" icon={LibraryIcon} active={screen === "library"} />
        <SideLink to="stats" label="Stats" icon={ChartIcon} active={screen === "stats"} />
      </nav>

      <div className="mt-auto space-y-5 px-2.5 pt-8">
        <section>
          <h2 className="mb-1.5 px-2.5 text-xs font-medium text-graphite">Work</h2>
          <WorkStrip summary={summary} stacked />
        </section>
        {storage && (
          <section className="px-2.5">
            <h2 className="mb-2 text-xs font-medium text-graphite">Disk</h2>
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
        "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium",
        props.active ? "bg-panel text-chalk shadow-[inset_3px_0_0_var(--color-mark)]" : "text-graphite hover:bg-white/5 hover:text-chalk",
      )}
    >
      <Icon className={cn("size-5 shrink-0", props.active ? "text-chalk" : "text-dim")} />
      <span className="flex-1">{props.label}</span>
      {!!props.badge && (
        <span
          className={cn(
            "min-w-6 rounded-md px-1.5 text-center font-mono text-[11px] font-semibold leading-5 tabular-nums",
            props.quiet ? "bg-panel-2 text-chalk" : "bg-mark text-ink",
          )}
        >
          {props.badge}
        </span>
      )}
    </a>
  );
}
