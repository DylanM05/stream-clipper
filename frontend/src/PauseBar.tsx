import { api, errorText, type Pauses } from "./api";
import { PauseIcon, PlayIcon } from "./icons";
import { cn } from "./lib";
import { useToast } from "./ui";

const LABEL: Record<keyof Pauses, string> = {
  downloads: "Downloads",
  processing: "Processing",
  renders: "Rendering",
  uploads: "Uploads",
};

/** Switches that pause or resume parts of the work as a whole. What's running stops within a few seconds. */
export function PauseBar(props: { parts: (keyof Pauses)[]; pauses?: Pauses; onChange: (p: Pauses) => void }) {
  const toast = useToast();
  if (!props.pauses) return null;
  const pauses = props.pauses;

  const toggle = async (part: keyof Pauses) => {
    try {
      props.onChange(await api.setPauses({ [part]: !pauses[part] }));
      toast({ text: pauses[part] ? `${LABEL[part]} resumed` : `${LABEL[part]} paused. It carries on from there when you resume.` });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  return (
    <div className="grid grid-cols-2 gap-2">
      {props.parts.map((part) => (
        <button
          key={part}
          onClick={() => toggle(part)}
          aria-pressed={pauses[part]}
          className={cn(
            "flex items-center gap-2 rounded-xl px-3 py-2.5 text-left text-sm font-semibold ring-1",
            pauses[part]
              ? "bg-amber-500/10 text-amber-200 ring-amber-500/30 active:bg-amber-500/20"
              : "bg-zinc-900 text-zinc-300 ring-white/5 active:bg-zinc-800",
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
