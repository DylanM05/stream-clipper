import { useEffect, useRef, useState } from "react";
import { api, errorText, type Clip, type Compilation } from "../api";
import { ReviewCard } from "../ClipCard";
import { CompilationStrip } from "../CompilationStrip";
import { InboxIcon } from "../icons";
import { go, loadSetting, saveSetting, usePoll } from "../lib";
import { Button, ErrorNote, Fold, Screen, ShowMore, useToast } from "../ui";

const OPEN_KEY = "inbox:open"; // which videos' groups were opened or folded, by video id
const OPEN_AT_FIRST = 3; // the newest few videos start open; the rest start folded
const SHOWN = 12; // clips shown of a video at first (ShowMore's step)

/** Keys that review clips on a computer. They only act while no text box has the keyboard. */
const KEYS: [string, string][] = [
  ["J K", "Move"],
  ["A", "Approve"],
  ["R", "Reject"],
  ["E", "Edit"],
  ["Z", "Undo"],
];
/** The keys while a stream on screen makes a compilation: A keeps its clips, and P keeps and posts one. */
const KEEP_KEYS: [string, string][] = [
  ["J K", "Move"],
  ["A", "Keep"],
  ["P", "Keep and post"],
  ["R", "Reject"],
  ["E", "Edit"],
  ["Z", "Undo"],
];

type Decision = "approve" | "keep" | "keep and post" | "reject";
type Group = { vodId: number; title: string; clips: Clip[]; compilation?: Compilation };

/** The compilation as it will be once `clip` is decided, until the next poll brings the real one. */
function decided(compilation: Compilation, clip: Clip, decision: Decision): Compilation {
  const range: [number, number] = [clip.start, clip.end];
  const to_review = compilation.to_review.filter(([a]) => a !== clip.start);
  if (decision === "reject") return { ...compilation, to_review };
  const kept = [...compilation.kept, range].sort(([a], [b]) => a - b);
  const clips = compilation.clips + 1;
  const status = to_review.length ? "waiting" : clips > 1 ? "making" : "one";
  return { ...compilation, kept, to_review, clips, length: compilation.length + clip.end - clip.start, status };
}

export function Inbox() {
  const toast = useToast();
  // The clips to review, and the compilations of the streams that make one, so a stream's group stays (as its strip)
  // once its last clip is decided.
  const { data, error, refresh, setData } = usePoll(() => Promise.all([api.clips(["proposed"]), api.compilations()]), 10000);
  const clips = data?.[0];
  const [opened, setOpened] = useState<Record<number, boolean>>(() => loadSetting(OPEN_KEY, {}));
  const [selected, setSelected] = useState<number | null>(null); // the clip the keyboard is on
  const [hovered, setHovered] = useState<number | null>(null); // the clip the mouse is on
  const lastDecided = useRef<{ id: number; undo: () => Promise<void> } | null>(null);
  const cards = useRef(new Map<number, HTMLElement>());

  const setOpen = (changes: Record<number, boolean>) => {
    // Only the videos still in the inbox are kept, so it doesn't grow forever.
    const next = Object.fromEntries(
      Object.entries({ ...opened, ...changes }).filter(([id]) => groups.some((g) => String(g.vodId) === id)),
    );
    setOpened(next);
    saveSetting(OPEN_KEY, next);
  };

  const restore = async (id: number) => {
    await api.restore(id);
    void refresh();
  };

  const decide = async (clip: Clip, decision: Decision) => {
    const compilation = groups.find((g) => g.vodId === clip.vod_id)?.compilation;
    const after = compilation && clip.in_compilation ? decided(compilation, clip, decision) : undefined;
    setData((d) => d && [d[0].filter((c) => c.id !== clip.id), d[1].map((c) => (after && c.vod_id === after.vod_id ? after : c))]);
    const undo = () => restore(clip.id);
    lastDecided.current = { id: clip.id, undo };
    try {
      if (decision === "keep") await api.keep(clip.id);
      else if (decision === "reject") await api.reject(clip.id);
      else await api.approve(clip.id); // in a compiling stream that keeps it and posts it by itself too
      const text =
        decision === "keep" && after?.status === "making"
          ? `That was the last one. The compilation of ${after.clips} clips is being made.`
          : {
              approve: "Approved: it will render shortly",
              keep: "Kept for the compilation",
              "keep and post": "Kept. It will also render to post by itself.",
              reject: "Rejected",
            }[decision];
      toast({ text, action: { label: "Undo", run: undo } });
      if (compilation) void refresh(); // the strip shows the clip's kept parts, not just its ends
    } catch (e) {
      toast({ text: errorText(e) });
      void refresh();
    }
  };

  /** For a stream with a single clip kept: post that clip by itself (approve it). Undo keeps it for the compilation. */
  const postByItself = async (vodId: number) => {
    try {
      const { clips } = await api.vod(vodId);
      const clip = clips.find((c) => c.status === "kept");
      if (!clip) return void refresh();
      await api.approve(clip.id);
      const undo = async () => {
        await api.keep(clip.id);
        void refresh();
      };
      lastDecided.current = { id: clip.id, undo };
      toast({ text: "It will render to post by itself", action: { label: "Undo", run: undo } });
    } catch (e) {
      toast({ text: errorText(e) });
    }
    void refresh();
  };

  // Group by video, keeping the server's order (newest video first). A compiling stream's group has its compilation,
  // and one with nothing left to review is only its strip, after the rest.
  const groups: Group[] = [];
  for (const clip of clips ?? []) {
    const group = groups.find((g) => g.vodId === clip.vod_id);
    if (group) group.clips.push(clip);
    else groups.push({ vodId: clip.vod_id, title: clip.vod_title, clips: [clip] });
  }
  for (const compilation of data?.[1] ?? []) {
    const group = groups.find((g) => g.vodId === compilation.vod_id);
    if (group) group.compilation = compilation;
    else groups.push({ vodId: compilation.vod_id, title: compilation.vod_title, clips: [], compilation });
  }
  const reviewing = groups.filter((g) => g.clips.length > 0);
  const strips = groups.filter((g) => g.clips.length === 0);
  const isOpen = (vodId: number, i: number) => opened[vodId] ?? i < OPEN_AT_FIRST;
  // The clips the keyboard moves through: those on screen, in the order they're shown.
  const visible = reviewing.flatMap((g, i) => (isOpen(g.vodId, i) ? g.clips.slice(0, SHOWN) : []));
  const keeping = visible.some((c) => c.in_compilation);
  const lit = visible.find((c) => c.id === (hovered ?? selected)); // its range shows on its stream's strip

  const keys = useRef({ visible, selected, decide });
  keys.current = { visible, selected, decide };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (e.metaKey || e.ctrlKey || e.altKey || target.closest("input, textarea, select, [contenteditable]")) return;
      const { visible, selected, decide } = keys.current;
      const at = visible.findIndex((c) => c.id === selected);
      const current = at >= 0 ? visible[at] : undefined;
      const move = (to: number) => {
        const clip = visible[Math.max(0, Math.min(visible.length - 1, to))];
        if (!clip) return;
        setSelected(clip.id);
        cards.current.get(clip.id)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      };
      const key = e.key.toLowerCase();
      if (key === "j" || key === "arrowdown") move(at + 1);
      else if (key === "k" || key === "arrowup") move(at < 0 ? 0 : at - 1);
      else if (current && (key === "a" || key === "r" || (key === "p" && current.in_compilation))) {
        // The keyboard stays where it was, on the clip that takes this one's place.
        setSelected((visible[at + 1] ?? visible[at - 1])?.id ?? null);
        const keep = current.in_compilation ? "keep" : "approve";
        void decide(current, key === "a" ? keep : key === "p" ? "keep and post" : "reject");
      } else if (current && (key === "e" || key === "enter")) go(`clip/${current.id}`);
      else if (key === "z" && lastDecided.current !== null) {
        const { id, undo } = lastDecided.current;
        lastDecided.current = null;
        void undo().then(() => setSelected(id));
      } else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <Screen title="Inbox" subtitle={clips ? `${clips.length} clip${clips.length === 1 ? "" : "s"} to review` : " "} wide>
      {error && <ErrorNote>{error}</ErrorNote>}
      {clips?.length === 0 && (
        <div className="mt-16 flex flex-col items-center text-center">
          <div className="rounded-full bg-panel p-5">
            <InboxIcon className="size-8 text-graphite" />
          </div>
          <h2 className="mt-4 text-2xl font-semibold font-stretch-condensed">All caught up</h2>
          <p className="mt-1 max-w-xs text-sm text-graphite">New clips show up here once a video has been processed.</p>
          <Button tone="primary" className="mt-6" onClick={() => go("videos")}>
            Add a video
          </Button>
        </div>
      )}
      {reviewing.length > 0 && (
        <div className="mb-1 flex items-baseline gap-4">
          <p className="min-w-0 flex-1 text-[13px] text-graphite">
            {/* A mouse can't swipe a card, so a computer gets the keys instead. */}
            <span className="pointer-fine:hidden">
              Swipe right to {keeping ? "keep" : "approve"}, left to reject, or tap to trim and edit.
            </span>
            <span className="hidden flex-wrap items-center gap-x-4 gap-y-1 pointer-fine:flex">
              {(keeping ? KEEP_KEYS : KEYS).map(([k, what]) => (
                <span key={what} className="inline-flex items-center gap-1.5">
                  {k.split(" ").map((one) => (
                    <kbd key={one} className="grid h-6 min-w-6 place-items-center rounded-md bg-panel-2 px-1.5 font-mono text-xs text-chalk shadow-[inset_0_-2px_0_rgb(0_0_0/0.35)]">
                      {one}
                    </kbd>
                  ))}
                  {what}
                </span>
              ))}
            </span>
          </p>
          {reviewing.length > 1 && (
            <span className="flex shrink-0 gap-4 text-[13px] font-semibold text-mark">
              <button type="button" onClick={() => setOpen(Object.fromEntries(reviewing.map((g) => [g.vodId, true])))}>
                Expand all
              </button>
              <button type="button" onClick={() => setOpen(Object.fromEntries(reviewing.map((g) => [g.vodId, false])))}>
                Fold all
              </button>
            </span>
          )}
        </div>
      )}
      <div className="lg:max-w-6xl">
        {reviewing.map((g, i) => (
          <Fold
            key={g.vodId}
            title={g.title}
            count={g.clips.length}
            open={isOpen(g.vodId, i)}
            onToggle={(open) => setOpen({ [g.vodId]: open })}
          >
            {g.compilation && !g.compilation.clip_id && (
              <div className="pb-3 pt-1 lg:max-w-xl">
                <CompilationStrip
                  compilation={g.compilation}
                  highlight={lit?.vod_id === g.vodId && lit.in_compilation ? [lit.start, lit.end] : undefined}
                />
              </div>
            )}
            <ShowMore items={g.clips} step={SHOWN}>
              {(shown) => (
                <div className="divide-y divide-line lg:space-y-1 lg:divide-y-0">
                  {shown.map((c) => (
                    <ReviewCard
                      key={c.id}
                      ref={(el) => {
                        if (el) cards.current.set(c.id, el);
                        else cards.current.delete(c.id);
                      }}
                      clip={c}
                      selected={c.id === selected}
                      onApprove={() => decide(c, c.in_compilation ? "keep" : "approve")}
                      onKeepAndPost={c.in_compilation ? () => decide(c, "keep and post") : undefined}
                      onReject={() => decide(c, "reject")}
                      onHover={(on) => setHovered((id) => (on ? c.id : id === c.id ? null : id))}
                      strip={c.kind === "compilation" && g.compilation ? <CompilationStrip compilation={g.compilation} /> : undefined}
                    />
                  ))}
                </div>
              )}
            </ShowMore>
          </Fold>
        ))}
        {strips.map((g) => g.compilation && <StripOnly key={g.vodId} title={g.title} compilation={g.compilation} onPost={() => postByItself(g.vodId)} />)}
      </div>
    </Screen>
  );
}

/**
 * A compiling stream with nothing left to review: its strip, and where it stands. Being made (until it is, when it
 * arrives as a card if the show reviews compilations), or a single clip kept, which can be posted by itself.
 */
function StripOnly({ title, compilation, onPost }: { title: string; compilation: Compilation; onPost: () => void }) {
  const one = compilation.status === "one";
  const stands = one ? "Only one clip kept" : compilation.status === "making" ? "Being made" : "";
  return (
    <section className="pt-5">
      <div className="flex items-baseline gap-2 border-b border-line pb-2">
        <h3 className="min-w-0 flex-1 truncate text-xl font-semibold font-stretch-condensed">{title}</h3>
        {stands && <span className="shrink-0 text-[13px] text-graphite">{stands}</span>}
      </div>
      <div className="pt-3 lg:max-w-xl">
        <CompilationStrip compilation={compilation} />
        {one && (
          <>
            <p className="mt-2 text-[13px] text-graphite">Keep another, or post this one by itself.</p>
            <Button className="mt-3" onClick={onPost}>
              Post it by itself
            </Button>
          </>
        )}
      </div>
    </section>
  );
}
