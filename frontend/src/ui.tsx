// Small shared building blocks: screen layout, status chips, buttons, toasts.

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import type { Clip, ClipStatus, Destination, Mode, Storage, UploadState, VodStatus } from "./api";
import { BackIcon } from "./icons";
import { cn } from "./lib";

/**
 * A page: its title, then its content. On a phone it's one column above the tab bar; on a desktop, wide pages
 * spread out (lists into two columns, from the xl breakpoint) and the rest stay a readable column.
 */
export function Screen(props: {
  title: string;
  subtitle?: ReactNode;
  onBack?: () => void;
  actions?: ReactNode;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "mx-auto max-w-2xl px-4 pb-32 pt-[calc(env(safe-area-inset-top)+0.75rem)] lg:px-8 lg:pb-16 lg:pt-8",
        props.wide && "lg:max-w-6xl",
      )}
    >
      <header className="mb-4 flex items-center gap-2 lg:mb-6">
        {props.onBack && (
          <button onClick={props.onBack} className="-ml-2 rounded-full p-2 hover:bg-white/5 active:bg-white/10" aria-label="Back">
            <BackIcon className="size-6" />
          </button>
        )}
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-2xl font-bold tracking-tight">{props.title}</h1>
          {props.subtitle && <p className="truncate text-sm text-zinc-400">{props.subtitle}</p>}
        </div>
        {props.actions}
      </header>
      {props.children}
    </div>
  );
}

const CLIP_STATUS: Record<ClipStatus, [string, string]> = {
  proposed: ["To review", "bg-violet-500/15 text-violet-300"],
  extra: ["Extra", "bg-zinc-500/15 text-zinc-300"],
  approved: ["Queued to render", "bg-amber-500/15 text-amber-300"],
  rendering: ["Rendering", "bg-amber-500/15 text-amber-300"],
  rendered: ["Ready to upload", "bg-sky-500/15 text-sky-300"],
  uploaded: ["Uploaded", "bg-emerald-500/15 text-emerald-300"],
  rejected: ["Rejected", "bg-zinc-500/15 text-zinc-400"],
  failed: ["Render failed", "bg-red-500/15 text-red-300"],
};

export const MODE_LABEL: Record<Mode, string> = { segment: "Clips", whole: "Full episode", both: "Full episode + clips" };

const VOD_STATUS: Record<VodStatus, [string, string]> = {
  queued: ["Queued", "bg-zinc-500/15 text-zinc-300"],
  downloading: ["Downloading", "bg-sky-500/15 text-sky-300"],
  preparing: ["Making preview", "bg-sky-500/15 text-sky-300"],
  transcribing: ["Transcribing", "bg-sky-500/15 text-sky-300"],
  analysing: ["Analysing", "bg-sky-500/15 text-sky-300"],
  segmenting: ["Claude is picking clips", "bg-violet-500/15 text-violet-300"],
  waiting: ["Waiting", "bg-amber-500/15 text-amber-300"],
  ready: ["Ready", "bg-emerald-500/15 text-emerald-300"],
  failed: ["Failed", "bg-red-500/15 text-red-300"],
};

const PAUSED = "bg-amber-500/15 text-amber-300";

// A rendered clip whose upload has been started says where that stands instead of "Ready to upload", and so does
// one that's up on one site while another is still to come (or failed).
const UPLOAD_STATUS: Record<Exclude<UploadState, "">, [string, string]> = {
  uploading: ["Uploading", "bg-sky-500/15 text-sky-300"],
  queued: ["Uploads next", "bg-indigo-500/15 text-indigo-300"],
  scheduled: ["Scheduled", "bg-indigo-500/15 text-indigo-300"],
  retrying: ["Upload retries later", "bg-amber-500/15 text-amber-300"],
  failed: ["Upload failed", "bg-red-500/15 text-red-300"],
};

export function ClipStatusChip({ status, paused, upload }: { status: ClipStatus; paused?: boolean; upload?: UploadState }) {
  const [label, tone] = (status === "rendered" || status === "uploaded") && upload ? UPLOAD_STATUS[upload] : CLIP_STATUS[status];
  if (paused && status === "approved") return <Chip tone={PAUSED}>Render paused</Chip>;
  if (paused && status === "rendering") return <Chip tone={PAUSED}>Pausing…</Chip>;
  return <Chip tone={tone}>{label}</Chip>;
}

export function VodStatusChip({ status, paused }: { status: VodStatus; paused?: boolean }) {
  const [label, tone] = VOD_STATUS[status];
  if (paused && (status === "queued" || status === "waiting")) return <Chip tone={PAUSED}>Paused</Chip>;
  if (paused && status !== "ready" && status !== "failed") return <Chip tone={PAUSED}>Pausing…</Chip>;
  return <Chip tone={tone}>{label}</Chip>;
}

function Chip({ tone, children }: { tone: string; children: ReactNode }) {
  return (
    <span className={cn("inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[11px] font-semibold", tone)}>
      {children}
    </span>
  );
}

export const KIND_LABEL: Record<Clip["kind"], string> = {
  chapter: "Chapter",
  highlight: "Highlight",
  whole: "Full episode",
};

export function ProgressBar({ value }: { value: number | null }) {
  return (
    <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800">
      {value === null ? (
        <div className="h-full w-1/3 animate-[slide_1.4s_ease-in-out_infinite] rounded-full bg-violet-500" />
      ) : (
        <div
          className="h-full rounded-full bg-violet-500 transition-[width] duration-700"
          style={{ width: `${Math.max(3, value * 100)}%` }}
        />
      )}
    </div>
  );
}

export function Button(props: {
  onClick?: () => void;
  tone?: "primary" | "approve" | "danger" | "plain";
  disabled?: boolean;
  className?: string;
  type?: "button" | "submit";
  children: ReactNode;
}) {
  // Hover only applies where there's a mouse (Tailwind puts it behind @media (hover: hover)), and not when disabled.
  const tones = {
    primary: "bg-violet-600 text-white enabled:hover:bg-violet-500 active:bg-violet-700",
    approve: "bg-emerald-600 text-white enabled:hover:bg-emerald-500 active:bg-emerald-700",
    danger: "bg-red-500/10 text-red-300 ring-1 ring-red-500/30 enabled:hover:bg-red-500/15 active:bg-red-500/20",
    plain: "bg-zinc-800 text-zinc-100 enabled:hover:bg-zinc-700/80 active:bg-zinc-700",
  };
  return (
    <button
      type={props.type ?? "button"}
      onClick={props.onClick}
      disabled={props.disabled}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-xl px-4 py-3 text-sm font-semibold disabled:opacity-40",
        tones[props.tone ?? "plain"],
        props.className,
      )}
    >
      {props.children}
    </button>
  );
}

/** Pick which YouTube channel something goes to. */
export function DestinationSelect(props: {
  value: number | null;
  destinations: Destination[] | undefined;
  onChange: (id: number | null) => void;
  className?: string;
}) {
  return (
    <select
      value={props.value ?? ""}
      onChange={(e) => props.onChange(e.target.value ? Number(e.target.value) : null)}
      className={cn(
        "w-full appearance-none rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 focus:ring-2 focus:ring-violet-500",
        props.className,
      )}
    >
      <option value="">Not chosen yet</option>
      {props.destinations?.map((d) => (
        <option key={d.id} value={d.id}>
          {d.name}
        </option>
      ))}
    </select>
  );
}

// --- Pieces of the desktop's overview and stats ---

/** One headline number: what it counts, the number, and a line about it. A link if it leads somewhere. */
export function StatTile(props: { label: string; value: ReactNode; note?: ReactNode; href?: string }) {
  const body = (
    <>
      <p className="truncate text-xs font-medium text-zinc-400">{props.label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight">{props.value}</p>
      {props.note && <p className="mt-0.5 truncate text-xs text-zinc-500">{props.note}</p>}
    </>
  );
  const tile = "block min-w-0 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5";
  return props.href ? (
    <a href={props.href} className={cn(tile, "hover:bg-zinc-800/70 hover:ring-white/10")}>
      {body}
    </a>
  ) : (
    <div className={tile}>{body}</div>
  );
}

/** A titled box on the overview, with an optional link to the page it summarises. */
export function Panel(props: { title: string; link?: { href: string; label: string }; children: ReactNode }) {
  return (
    <section className="rounded-2xl bg-zinc-900 ring-1 ring-white/5">
      <header className="flex items-baseline gap-2 px-4 pb-2 pt-3.5">
        <h2 className="flex-1 text-sm font-bold">{props.title}</h2>
        {props.link && (
          <a href={props.link.href} className="text-xs font-semibold text-violet-300 hover:text-violet-200">
            {props.link.label}
          </a>
        )}
      </header>
      {props.children}
    </section>
  );
}

/** How full the data disk is. Amber once it's getting near the point where downloads wait, red past it. */
export function DiskMeter({ storage }: { storage: Storage }) {
  const { free_gb: free, total_gb: size, videos_gb: videos, min_free_gb: min } = storage;
  if (free === null || !size) return <p className="text-xs text-zinc-500">Streams use {videos} GB.</p>;
  const low = free < min;
  const near = !low && free < min * 2;
  const [fill, track] = low
    ? ["bg-red-400", "bg-red-400/15"]
    : near
      ? ["bg-amber-400", "bg-amber-400/15"]
      : ["bg-violet-500", "bg-violet-500/15"];
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="font-semibold text-zinc-300">{free} GB free</span>
        <span className="text-zinc-500">of {size} GB</span>
      </div>
      <div
        className={cn("mt-1.5 h-1.5 overflow-hidden rounded-full", track)}
        role="meter"
        aria-label="Data disk used"
        aria-valuemin={0}
        aria-valuemax={size}
        aria-valuenow={size - free}
      >
        <div className={cn("h-full rounded-full", fill)} style={{ width: `${Math.min(100, ((size - free) / size) * 100)}%` }} />
      </div>
      <p className={cn("mt-1.5 text-xs", low ? "text-red-300" : near ? "text-amber-300" : "text-zinc-500")}>
        {low ? `Below ${min} GB free: new downloads wait for space.` : `Streams use ${videos} GB.`}
        {near && ` Downloads wait below ${min} GB.`}
      </p>
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return <p className="rounded-xl bg-red-500/10 p-3 text-sm text-red-300 ring-1 ring-red-500/20">{children}</p>;
}

// --- Toasts with an optional action, e.g. "Approved · Undo" ---

type Toast = { id: number; text: string; action?: { label: string; run: () => void } };
type ShowToast = (toast: Omit<Toast, "id">) => void;

const ToastContext = createContext<ShowToast>(() => {});
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = (id: number) => setToasts((ts) => ts.filter((t) => t.id !== id));
  const show = useCallback<ShowToast>((toast) => {
    const id = Date.now() + Math.random();
    setToasts((ts) => [...ts.slice(-2), { ...toast, id }]);
    setTimeout(() => dismiss(id), 5000);
  }, []);

  return (
    <ToastContext.Provider value={show}>
      {children}
      {/* Above the tab bar on a phone; in the bottom-right corner on a desktop, out of the way of the page. */}
      <div className="pointer-events-none fixed inset-x-0 bottom-[calc(5.5rem+env(safe-area-inset-bottom))] z-50 flex flex-col items-center gap-2 px-4 lg:items-end lg:px-6">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="pointer-events-auto flex max-w-md items-center gap-4 rounded-xl bg-zinc-800 px-4 py-3 text-sm shadow-xl ring-1 ring-white/10"
          >
            <span className="flex-1">{t.text}</span>
            {t.action && (
              <button
                className="font-semibold text-violet-300 hover:text-violet-200"
                onClick={() => {
                  t.action!.run();
                  dismiss(t.id);
                }}
              >
                {t.action.label}
              </button>
            )}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
