import { useState } from "react";
import { api, errorText, type Clip } from "../api";
import { ClipCard } from "../ClipCard";
import { InboxIcon } from "../icons";
import { go, loadSetting, saveSetting, usePoll } from "../lib";
import { Button, ErrorNote, Fold, Screen, ShowMore, useToast } from "../ui";

const OPEN_KEY = "inbox:open"; // which videos' groups were opened or folded, by video id
const OPEN_AT_FIRST = 3; // the newest few videos start open; the rest start folded

export function Inbox() {
  const toast = useToast();
  const { data: clips, error, refresh, setData } = usePoll(() => api.clips(["proposed"]), 10000);
  const [opened, setOpened] = useState<Record<number, boolean>>(() => loadSetting(OPEN_KEY, {}));
  const setOpen = (changes: Record<number, boolean>) => {
    // Only the videos still in the inbox are kept, so it doesn't grow forever.
    const next = Object.fromEntries(
      Object.entries({ ...opened, ...changes }).filter(([id]) => groups.some((g) => String(g.vodId) === id)),
    );
    setOpened(next);
    saveSetting(OPEN_KEY, next);
  };

  const decide = async (clip: Clip, approve: boolean) => {
    setData((list) => list?.filter((c) => c.id !== clip.id));
    try {
      await (approve ? api.approve(clip.id) : api.reject(clip.id));
      toast({
        text: approve ? "Approved: it will render shortly" : "Rejected",
        action: {
          label: "Undo",
          run: async () => {
            await api.restore(clip.id);
            void refresh();
          },
        },
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

  return (
    <Screen title="Inbox" subtitle={clips ? `${clips.length} clip${clips.length === 1 ? "" : "s"} to review` : " "} wide>
      {error && <ErrorNote>{error}</ErrorNote>}
      {clips?.length === 0 && (
        <div className="mt-16 flex flex-col items-center text-center">
          <div className="rounded-full bg-zinc-900 p-5 ring-1 ring-white/5">
            <InboxIcon className="size-8 text-zinc-500" />
          </div>
          <h2 className="mt-4 text-lg font-semibold">All caught up</h2>
          <p className="mt-1 max-w-xs text-sm text-zinc-400">
            New clips show up here once a video has been processed.
          </p>
          <Button tone="primary" className="mt-6" onClick={() => go("videos")}>
            Add a video
          </Button>
        </div>
      )}
      {groups.length > 0 && (
        <div className="mb-1 flex items-baseline gap-4">
          <p className="min-w-0 flex-1 text-xs text-zinc-500">
            {/* A mouse can't swipe a card, so a computer is pointed to the buttons instead. */}
            <span className="pointer-fine:hidden">Swipe right to approve, left to reject, or tap to trim and edit.</span>
            <span className="hidden pointer-fine:inline">Approve or reject with the buttons under each clip, or click one to trim and edit it.</span>
          </p>
          {groups.length > 1 && (
            <span className="flex shrink-0 gap-4 text-xs font-semibold text-violet-300">
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
      {groups.map((g, i) => (
        <Fold
          key={g.vodId}
          title={g.title}
          count={g.clips.length}
          open={opened[g.vodId] ?? i < OPEN_AT_FIRST}
          onToggle={(open) => setOpen({ [g.vodId]: open })}
        >
          <ShowMore items={g.clips}>
            {(shown) => (
              <div className="space-y-3 xl:grid xl:grid-cols-2 xl:items-start xl:gap-3 xl:space-y-0">
                {shown.map((c) => (
                  <ClipCard key={c.id} clip={c} onApprove={() => decide(c, true)} onReject={() => decide(c, false)} />
                ))}
              </div>
            )}
          </ShowMore>
        </Fold>
      ))}
    </Screen>
  );
}
