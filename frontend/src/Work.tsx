import { useEffect } from "react";
import { api, errorText, type Pauses, type Summary, type WorkPart } from "./api";
import { PauseIcon, PlayIcon } from "./icons";
import { cn, usePoll } from "./lib";
import { useToast } from "./ui";

const STAGE: Record<keyof Pauses, string> = {
  downloads: "Download",
  processing: "Process",
  renders: "Render",
  uploads: "Upload",
};

/** What pausing and resuming are called in a toast. */
const PLURAL: Record<keyof Pauses, string> = {
  downloads: "Downloads",
  processing: "Processing",
  renders: "Rendering",
  uploads: "Uploads",
};

/** A video's step in processing, said before its title. */
const STEP: Record<string, string> = {
  preparing: "Making the preview of",
  transcribing: "Transcribing",
  analysing: "Analysing",
  segmenting: "The AI is picking clips from",
};

/** Announces new switch positions, so every work strip on the page (the desktop sidebar has one too) agrees. */
const CHANGED = "clipper:pauses";

/** The pause switches, refreshed from the server and kept in step with changes made elsewhere on the page. */
export function usePauses() {
  const { data: pauses, setData: setPauses } = usePoll(api.pauses, 10000);
  useEffect(() => {
    const onChange = (e: Event) => setPauses((e as CustomEvent<Pauses>).detail);
    window.addEventListener(CHANGED, onChange);
    return () => window.removeEventListener(CHANGED, onChange);
  }, [setPauses]);
  return { pauses, setPauses };
}

/** What a part of the work is doing, in a few words. */
function doing(part: WorkPart, paused: boolean): string {
  const waiting = part.count - (part.now ? 1 : 0);
  if (paused) return part.count ? `Paused with ${part.count} waiting` : "Paused";
  if (!part.now) return waiting ? `${waiting} waiting` : "Nothing to do";
  const what = STEP[part.step] ? `${STEP[part.step]} ${part.now}` : part.now;
  return waiting ? `${what}, and ${waiting} waiting` : what;
}

/**
 * The work, a step at a time in the order a video goes through it: download, process, render, upload. Each says
 * what it's doing and has a switch that pauses or resumes it; what's running stops within a few seconds.
 * `stacked` lists them one above the other, for the desktop sidebar and phones; otherwise they go across.
 */
export function WorkStrip(props: { summary?: Summary; parts?: (keyof Pauses)[]; stacked?: boolean; className?: string }) {
  const toast = useToast();
  const { pauses, setPauses } = usePauses();
  const { data: polled } = usePoll(() => (props.summary ? Promise.resolve(undefined) : api.summary()), 5000, !!props.summary);
  const work = (props.summary ?? polled)?.work;
  if (!pauses || !work) return null;
  const parts = work.filter((w) => !props.parts || props.parts.includes(w.stage));

  const toggle = async (stage: keyof Pauses) => {
    try {
      const now = await api.setPauses({ [stage]: !pauses[stage] });
      setPauses(now);
      window.dispatchEvent(new CustomEvent(CHANGED, { detail: now }));
      toast({ text: pauses[stage] ? `${PLURAL[stage]} resumed` : `${PLURAL[stage]} paused. It carries on from there when you resume.` });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  return (
    <ol
      className={cn(props.stacked ? "grid gap-0.5" : "grid gap-x-3 gap-y-5 sm:grid-cols-2 xl:grid-cols-4", props.className)}
      aria-label="Work"
    >
      {parts.map((part, i) => {
        const paused = pauses[part.stage];
        const busy = !!part.now && !paused;
        const label = `${paused ? "Resume" : "Pause"} ${PLURAL[part.stage].toLowerCase()}`;
        const lamp = (
          <span
            className={cn(
              "relative z-10 size-2.5 shrink-0 rounded-full",
              paused ? "ring-2 ring-zinc-500 ring-inset" : busy ? "bg-mark" : "bg-dim",
            )}
          />
        );
        const name = (
          <span className="flex items-center gap-2 font-semibold">
            {STAGE[part.stage]}
            {paused && <span className="rounded bg-zinc-500 px-1.5 text-[11px] font-semibold leading-4 text-bay">Paused</span>}
          </span>
        );
        const progress = busy && part.progress !== null && (
          <div className="h-[3px] overflow-hidden rounded-full bg-zinc-800">
            <div className="h-full bg-mark transition-[width] duration-700" style={{ width: `${Math.max(3, part.progress * 100)}%` }} />
          </div>
        );
        const button = (
          <button
            onClick={() => toggle(part.stage)}
            aria-label={label}
            title={label}
            className={cn(
              "inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg font-semibold",
              paused ? "bg-zinc-700 text-chalk" : "bg-zinc-800 text-graphite hover:text-chalk",
              props.stacked ? "size-9 lg:size-7" : "px-2.5 py-1.5 text-[13px]",
            )}
          >
            {paused ? <PlayIcon className="size-3.5" /> : <PauseIcon className="size-3.5" />}
            {!props.stacked && (paused ? "Resume" : "Pause")}
          </button>
        );

        if (props.stacked) {
          return (
            <li key={part.stage} className="relative grid grid-cols-[auto_1fr_auto] items-center gap-x-2.5 rounded-lg px-2.5 py-1.5">
              {i < parts.length - 1 && <span className="absolute bottom-[-50%] left-[14.5px] top-1/2 w-px bg-line" />}
              {lamp}
              <div className="min-w-0">
                <div className="flex items-baseline justify-between gap-2 text-sm">
                  {name}
                  <span className="font-mono text-xs text-graphite">{part.count}</span>
                </div>
                <p className="truncate text-xs text-graphite">{doing(part, paused)}</p>
              </div>
              {button}
              {progress && <div className="col-start-2 col-end-4 mt-1">{progress}</div>}
            </li>
          );
        }
        return (
          <li key={part.stage} className="relative min-w-0 pr-3">
            {i < parts.length - 1 && <span className="absolute left-3 right-0 top-[11px] hidden h-px bg-line xl:block" />}
            {/* The line to the next part runs behind; the name is cut out of it. */}
            <div className="relative flex w-fit items-center gap-2 bg-bay pr-3">{lamp}{name}</div>
            <div className="mt-2 font-mono text-2xl leading-none">{part.count}</div>
            <p className="mt-1.5 line-clamp-2 min-h-[2.5em] text-[13px] leading-snug text-graphite">{doing(part, paused)}</p>
            <div className="mt-2.5">{button}</div>
            {progress && <div className="mt-2.5">{progress}</div>}
          </li>
        );
      })}
    </ol>
  );
}
