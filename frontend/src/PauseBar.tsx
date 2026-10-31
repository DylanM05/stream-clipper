import { useEffect } from "react";
import { api, errorText, type Pauses } from "./api";
import { PauseIcon, PlayIcon } from "./icons";
import { cn, usePoll } from "./lib";
import { useToast } from "./ui";

const LABEL: Record<keyof Pauses, string> = {
  downloads: "Downloads",
  processing: "Processing",
  renders: "Rendering",
  uploads: "Uploads",
};

/** Announces new switch positions, so every set of switches on the page (the desktop sidebar has some too) agrees. */
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

/**
 * Switches that pause or resume parts of the work as a whole. What's running stops within a few seconds.
 * Compact puts them in one column, for the desktop sidebar.
 */
export function PauseBar(props: { parts: (keyof Pauses)[]; pauses?: Pauses; onChange: (p: Pauses) => void; compact?: boolean }) {
  const toast = useToast();
  if (!props.pauses) return null;
  const pauses = props.pauses;

  const toggle = async (part: keyof Pauses) => {
    try {
      const now = await api.setPauses({ [part]: !pauses[part] });
      props.onChange(now);
      window.dispatchEvent(new CustomEvent(CHANGED, { detail: now }));
      toast({ text: pauses[part] ? `${LABEL[part]} resumed` : `${LABEL[part]} paused. It carries on from there when you resume.` });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  return (
    <div className={cn("grid", props.compact ? "grid-cols-1 gap-1.5" : "grid-cols-2 gap-2")}>
      {props.parts.map((part) => (
        <button
          key={part}
          onClick={() => toggle(part)}
          aria-pressed={pauses[part]}
          className={cn(
            "flex items-center gap-2 rounded-xl px-3 text-left font-semibold ring-1",
            props.compact ? "py-2 text-[13px]" : "py-2.5 text-sm",
            pauses[part]
              ? "bg-amber-500/10 text-amber-200 ring-amber-500/30 hover:bg-amber-500/15 active:bg-amber-500/20"
              : "bg-zinc-900 text-zinc-300 ring-white/5 hover:bg-zinc-800/70 active:bg-zinc-800",
          )}
        >
          {/* The icon is what a tap does; the word on the right is how it is now. */}
          {pauses[part] ? <PlayIcon className="size-4 shrink-0" /> : <PauseIcon className="size-4 shrink-0 text-zinc-500" />}
          <span className="min-w-0 flex-1 truncate">{LABEL[part]}</span>
          <span className={cn("text-[11px] font-medium", pauses[part] ? "text-amber-300" : "text-zinc-500")}>
            {pauses[part] ? "Paused" : "On"}
          </span>
        </button>
      ))}
    </div>
  );
}
