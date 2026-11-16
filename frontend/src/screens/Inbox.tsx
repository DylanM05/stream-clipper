import { useEffect, useRef, useState } from "react";
import { api, errorText, type Clip } from "../api";
import { ReviewCard } from "../ClipCard";
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

export function Inbox() {
  const toast = useToast();
  const { data: clips, error, refresh, setData } = usePoll(() => api.clips(["proposed"]), 10000);
  const [opened, setOpened] = useState<Record<number, boolean>>(() => loadSetting(OPEN_KEY, {}));
  const [selected, setSelected] = useState<number | null>(null); // the clip the keyboard is on
  const lastDecided = useRef<number | null>(null);
  const cards = useRef(new Map<number, HTMLElement>());

  const setOpen = (changes: Record<number, boolean>) => {
    // Only the videos still in the inbox are kept, so it doesn't grow forever.
    const next = Object.fromEntries(
      Object.entries({ ...opened, ...changes }).filter(([id]) => groups.some((g) => String(g.vodId) === id)),
    );
    setOpened(next);
    saveSetting(OPEN_KEY, next);
  };

  const undo = async (id: number) => {
    await api.restore(id);
    void refresh();
  };

  const decide = async (clip: Clip, approve: boolean) => {
    setData((list) => list?.filter((c) => c.id !== clip.id));
    lastDecided.current = clip.id;
    try {
      await (approve ? api.approve(clip.id) : api.reject(clip.id));
      toast({
        text: approve ? "Approved: it will render shortly" : "Rejected",
        action: { label: "Undo", run: () => undo(clip.id) },
      });
    } catch (e) {
      toast({ text: errorText(e) });
      void refresh();
    }
  };

  // Group by video, keeping the server's order (newest video first).
  const groups: { vodId: number; title: string; clips: Clip[] }[] = [];
  for (const clip of clips ?? []) {
    const group = groups.find((g) => g.vodId === clip.vod_id);
    if (group) group.clips.push(clip);
    else groups.push({ vodId: clip.vod_id, title: clip.vod_title, clips: [clip] });
  }
  const isOpen = (vodId: number, i: number) => opened[vodId] ?? i < OPEN_AT_FIRST;
  // The clips the keyboard moves through: those on screen, in the order they're shown.
  const visible = groups.flatMap((g, i) => (isOpen(g.vodId, i) ? g.clips.slice(0, SHOWN) : []));

  const keys = useRef({ visible, selected, decide, undo });
  keys.current = { visible, selected, decide, undo };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (e.metaKey || e.ctrlKey || e.altKey || target.closest("input, textarea, select, [contenteditable]")) return;
      const { visible, selected, decide, undo } = keys.current;
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
      else if (current && (key === "a" || key === "r")) {
        // The keyboard stays where it was, on the clip that takes this one's place.
        setSelected((visible[at + 1] ?? visible[at - 1])?.id ?? null);
        void decide(current, key === "a");
      } else if (current && (key === "e" || key === "enter")) go(`clip/${current.id}`);
      else if (key === "z" && lastDecided.current !== null) {
        const id = lastDecided.current;
        lastDecided.current = null;
        void undo(id).then(() => setSelected(id));
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
      {groups.length > 0 && (
        <div className="mb-1 flex items-baseline gap-4">
          <p className="min-w-0 flex-1 text-[13px] text-graphite">
            {/* A mouse can't swipe a card, so a computer gets the keys instead. */}
            <span className="pointer-fine:hidden">Swipe right to approve, left to reject, or tap to trim and edit.</span>
            <span className="hidden flex-wrap items-center gap-x-4 gap-y-1 pointer-fine:flex">
              {KEYS.map(([k, what]) => (
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
          {groups.length > 1 && (
            <span className="flex shrink-0 gap-4 text-[13px] font-semibold text-mark">
              <button type="button" onClick={() => setOpen(Object.fromEntries(groups.map((g) => [g.vodId, true])))}>
                Expand all
              </button>
              <button type="button" onClick={() => setOpen(Object.fromEntries(groups.map((g) => [g.vodId, false])))}>
                Fold all
              </button>
            </span>
          )}
        </div>
      )}
      <div className="lg:max-w-6xl">
        {groups.map((g, i) => (
          <Fold
            key={g.vodId}
            title={g.title}
            count={g.clips.length}
            open={isOpen(g.vodId, i)}
            onToggle={(open) => setOpen({ [g.vodId]: open })}
          >
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
                      onApprove={() => decide(c, true)}
                      onReject={() => decide(c, false)}
                    />
                  ))}
                </div>
              )}
            </ShowMore>
          </Fold>
        ))}
      </div>
    </Screen>
  );
}
