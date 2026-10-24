import { api, errorText, type Clip } from "../api";
import { ClipCard } from "../ClipCard";
import { InboxIcon } from "../icons";
import { go, usePoll } from "../lib";
import { Button, ErrorNote, Screen, useToast } from "../ui";

export function Inbox() {
  const toast = useToast();
  const { data: clips, error, refresh, setData } = usePoll(() => api.clips(["proposed"]), 10000);

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
    <Screen title="Inbox" subtitle={clips ? `${clips.length} clip${clips.length === 1 ? "" : "s"} to review` : " "}>
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
        <p className="mb-1 text-xs text-zinc-500">Swipe right to approve, left to reject, or tap to trim and edit.</p>
      )}
      {groups.map((g) => (
        <section key={g.vodId}>
          <h2 className="truncate px-1 pb-2 pt-4 text-xs font-semibold uppercase tracking-wide text-zinc-500">
            {g.title}
          </h2>
          <div className="space-y-3">
            {g.clips.map((c) => (
              <ClipCard key={c.id} clip={c} onApprove={() => decide(c, true)} onReject={() => decide(c, false)} />
            ))}
          </div>
        </section>
      ))}
    </Screen>
  );
}
