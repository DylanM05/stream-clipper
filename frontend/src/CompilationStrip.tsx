import type { Compilation } from "./api";
import { cn, length } from "./lib";
import { Button } from "./ui";

/** What the strip's header says about the clips in it, from where the compilation stands. */
function counts({ status, clips, to_review }: Compilation): string {
  const review = to_review.length;
  if (status === "waiting") return clips ? `${clips} kept, ${review} to review` : `Nothing kept yet, ${review} to review`;
  if (status === "none") return "Nothing kept";
  if (status === "making" || status === "one") return `${clips} kept`;
  return `${clips} clip${clips === 1 ? "" : "s"}`; // made: it says how many it joins
}

/**
 * A stream's compilation taking shape: one bar the length of the stream, with the parts kept for it in chalk and the
 * picks still to review outlined. `highlight` shows the range of the clip the keyboard or a hover is on, in mark
 * yellow. A pick that's kept fills in; one that's rejected goes.
 */
export function CompilationStrip({ compilation, highlight }: { compilation: Compilation; highlight?: [number, number] }) {
  const total = compilation.vod_duration || Math.max(1, ...[...compilation.kept, ...compilation.to_review].map(([, b]) => b));
  // One block per start, kept over to review, so a pick that's kept keeps its block and the fill can play.
  const blocks = new Map<number, { range: [number, number]; kept: boolean }>();
  for (const range of compilation.to_review) blocks.set(range[0], { range, kept: false });
  for (const range of compilation.kept) blocks.set(range[0], { range, kept: true });
  const place = ([a, b]: [number, number]) => ({
    left: `${Math.min(100, (a / total) * 100)}%`,
    width: `${Math.max(0, ((b - a) / total) * 100)}%`,
  });
  return (
    <div>
      <div className="flex items-baseline gap-2.5 text-[13px] text-graphite">
        <span className="text-[17px] font-semibold text-chalk font-stretch-semi-condensed">Compilation</span>
        <span className="min-w-0 truncate">{counts(compilation)}</span>
        <span className="ml-auto font-mono text-chalk">{length(compilation.length)}</span>
      </div>
      <div className="relative mt-2 h-1.5 rounded-full bg-panel-2" title="The compilation's parts in the stream">
        {[...blocks.values()].map(({ range, kept }) => (
          <i
            key={range[0]}
            className={cn(
              "absolute inset-y-0 min-w-[3px] rounded-sm transition-colors duration-200",
              kept ? "bg-chalk" : "bg-transparent ring-1 ring-inset ring-graphite",
            )}
            style={place(range)}
          />
        ))}
        {highlight && <i className="absolute inset-y-0 min-w-[3px] rounded-sm bg-mark" style={place(highlight)} />}
      </div>
    </div>
  );
}

/** Under a compilation's strip when the clips kept now would make a different one: Rebuild makes it again from them. */
export function ChangedNotice({ onRebuild, busy }: { onRebuild: () => void; busy?: boolean }) {
  return (
    <div className="mt-3 flex flex-wrap items-center justify-end gap-x-3 gap-y-2 rounded-xl bg-panel-2 px-3.5 py-3">
      <span className="size-2 shrink-0 rounded-full bg-mark" />
      <span className="min-w-0 flex-1 basis-48 text-sm">The kept clips changed since it was made.</span>
      <Button tone="primary" disabled={busy} onClick={onRebuild} className="px-3 py-2">
        Rebuild from kept clips
      </Button>
    </div>
  );
}
