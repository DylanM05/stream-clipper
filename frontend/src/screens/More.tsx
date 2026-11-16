import type { ComponentType, ReactNode } from "react";
import { api, type Summary } from "../api";
import { ChartIcon, GearIcon, GridIcon, LibraryIcon } from "../icons";
import { usePoll } from "../lib";
import { DiskMeter, Screen } from "../ui";
import { WorkStrip } from "../Work";

/**
 * The phone's fifth tab: the pages used less often, and the work and the disk, which a desktop keeps in its sidebar.
 */
export function More({ summary }: { summary?: Summary }) {
  const { data: storage } = usePoll(api.storage, 120000);
  return (
    <Screen title="More">
      <nav className="rounded-2xl bg-panel px-4" aria-label="More pages">
        <Link to="overview" icon={GridIcon} label="Overview" note="What needs you" />
        <Link to="library" icon={LibraryIcon} label="Library" />
        <Link to="stats" icon={ChartIcon} label="Stats" />
        <Link to="settings" icon={GearIcon} label="Settings" />
      </nav>
      <Heading>Work</Heading>
      <WorkStrip summary={summary} stacked className="rounded-2xl bg-panel p-1.5" />
      {storage && (
        <>
          <Heading>Disk</Heading>
          <div className="rounded-2xl bg-panel p-4">
            <DiskMeter storage={storage} />
          </div>
        </>
      )}
    </Screen>
  );
}

function Heading({ children }: { children: ReactNode }) {
  return <h2 className="mb-3 mt-8 text-xl font-semibold font-stretch-condensed">{children}</h2>;
}

function Link(props: { to: string; icon: ComponentType<{ className?: string }>; label: string; note?: string }) {
  const Icon = props.icon;
  return (
    <a href={`#/${props.to}`} className="flex items-center gap-3.5 border-b border-line py-3.5 last:border-b-0">
      <Icon className="size-5 shrink-0 text-graphite" />
      <span className="flex-1 font-semibold">{props.label}</span>
      {props.note && <span className="text-sm text-graphite">{props.note}</span>}
      <svg viewBox="0 0 24 24" className="size-4 text-dim" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
        <path d="M9 6l6 6-6 6" />
      </svg>
    </a>
  );
}
