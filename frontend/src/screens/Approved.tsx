import type { ReactNode } from "react";
import { api, errorText, type Clip } from "../api";
import { ClipCard } from "../ClipCard";
import { InboxIcon, LinkIcon, PauseIcon, PlayIcon, TrashIcon, UploadIcon } from "../icons";
import { cn, go, usePoll } from "../lib";
import { PauseBar } from "../PauseBar";
import { ErrorNote, Screen, useToast } from "../ui";

/** Approved clips: being rendered, ready to upload, and uploaded. */
export function Approved() {
  const { data: clips, error, refresh } = usePoll(
    () => api.clips(["approved", "rendering", "failed", "rendered", "uploaded"]),
    5000,
  );
  const { data: pauses, setData: setPauses } = usePoll(api.pauses, 10000);
  const ready = clips?.filter((c) => c.status === "rendered") ?? [];
  const working = clips?.filter((c) => ["approved", "rendering", "failed"].includes(c.status)) ?? [];
  const uploaded = clips?.filter((c) => c.status === "uploaded") ?? [];

  return (
    <Screen title="Upload" subtitle="Approved clips, ready to post to YouTube.">
      <PauseBar parts={["renders", "uploads"]} pauses={pauses} onChange={setPauses} />
      {error && <ErrorNote>{error}</ErrorNote>}
      {clips?.length === 0 && <p className="mt-10 text-center text-sm text-zinc-500">Nothing approved yet.</p>}
      <Section title="Ready to upload" clips={ready} footer={(c) => <UploadFooter clip={c} onChanged={refresh} />} />
      <Section title="Rendering" clips={working} footer={(c) => <RenderFooter clip={c} onChanged={refresh} />} />
      <Section title="Uploaded" clips={uploaded} footer={(c) => <UploadedFooter clip={c} />} />
    </Screen>
  );
}

function Section(props: { title: string; clips: Clip[]; footer: (c: Clip) => ReactNode }) {
  if (props.clips.length === 0) return null;
  return (
    <section>
      <h2 className="px-1 pb-2 pt-4 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        {props.title} · {props.clips.length}
      </h2>
      <div className="space-y-3">
        {props.clips.map((c) => (
          <ClipCard key={c.id} clip={c} showStatus footer={props.footer(c)} />
        ))}
      </div>
    </section>
  );
}

/** Runs a clip action with a toast, and an undo where there is one. */
function useClipAction(onChanged: () => void) {
  const toast = useToast();
  return async (work: () => Promise<unknown>, done?: string, undo?: () => Promise<unknown>) => {
    try {
      await work();
      if (done) toast({ text: done, action: undo && { label: "Undo", run: () => void undo().then(onChanged) } });
      onChanged();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };
}

function FooterButton(props: { onClick: () => void; tone?: "danger" | "accent"; children: ReactNode }) {
  return (
    <button
      onClick={props.onClick}
      className={cn(
        "flex flex-1 items-center justify-center gap-1.5 py-2.5 text-sm font-medium active:bg-white/5",
        props.tone === "danger" ? "text-red-300" : props.tone === "accent" ? "font-semibold text-violet-300" : "text-zinc-400",
      )}
    >
      {props.children}
    </button>
  );
}

function UploadFooter({ clip, onChanged }: { clip: Clip; onChanged: () => void }) {
  const act = useClipAction(onChanged);
  return (
    <div className="border-t border-white/5">
      <p className="truncate px-3 pt-2 text-xs text-zinc-500">
        {clip.destination_name ? `To ${clip.destination_name}` : "No channel chosen yet"}
      </p>
      <div className="flex divide-x divide-white/5">
        <FooterButton onClick={() => act(() => api.restore(clip.id), "Back in the inbox. Its render was deleted.")}>
          <InboxIcon className="size-4" /> To inbox
        </FooterButton>
        <FooterButton
          tone="danger"
          onClick={() => act(() => api.reject(clip.id), "Deleted", () => api.approve(clip.id))}
        >
          <TrashIcon className="size-4" /> Delete
        </FooterButton>
        <FooterButton tone="accent" onClick={() => go(`upload/${clip.id}`)}>
          <UploadIcon className="size-4" /> Upload
        </FooterButton>
      </div>
    </div>
  );
}

function RenderFooter({ clip, onChanged }: { clip: Clip; onChanged: () => void }) {
  const act = useClipAction(onChanged);
  const failed = clip.status === "failed";
  return (
    <div className="border-t border-white/5">
      {failed && <p className="line-clamp-3 break-words px-3 pt-2 text-xs text-red-300">{clip.error}</p>}
      <div className="flex divide-x divide-white/5">
        <FooterButton onClick={() => act(() => api.restore(clip.id), "Back in the inbox")}>
          <InboxIcon className="size-4" /> To inbox
        </FooterButton>
        {failed ? (
          <FooterButton tone="accent" onClick={() => act(() => api.approve(clip.id))}>
            Render again
          </FooterButton>
        ) : (
          <FooterButton
            tone="accent"
            onClick={() => act(() => (clip.paused ? api.resumeClip(clip.id) : api.pauseClip(clip.id)))}
          >
            {clip.paused ? <PlayIcon className="size-4" /> : <PauseIcon className="size-4" />}
            {clip.paused ? "Resume" : "Pause"}
          </FooterButton>
        )}
      </div>
    </div>
  );
}

function UploadedFooter({ clip }: { clip: Clip }) {
  return (
    <div className="flex items-center gap-2 border-t border-white/5 px-3 py-2.5 text-sm">
      <span className="min-w-0 flex-1 truncate text-zinc-500">{clip.destination_name || "Uploaded"}</span>
      {clip.youtube_url ? (
        <a href={clip.youtube_url} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 font-semibold text-emerald-300">
          <LinkIcon className="size-4" /> On YouTube
        </a>
      ) : (
        <button onClick={() => go(`upload/${clip.id}`)} className="font-semibold text-zinc-300">
          Details
        </button>
      )}
    </div>
  );
}
