import type { ReactNode } from "react";
import { api, errorText, type Clip, type UploadState } from "../api";
import { ClipCard } from "../ClipCard";
import { InboxIcon, LinkIcon, PauseIcon, PlayIcon, TrashIcon } from "../icons";
import { cn, firstSentence, upcoming, usePoll } from "../lib";
import { WorkStrip } from "../Work";
import { ErrorNote, Fold, Screen, ShowMore, useToast } from "../ui";

// Rendered clips whose upload has been started, in the order they go up; a failed one is back to ready.
export const GOING_UP: UploadState[] = ["uploading", "queued", "scheduled", "retrying"];

/** Clips whose upload has started, in the order they go up: the one uploading, the one next, then the scheduled ones and
 * the retries together by when they go (a retry keeps its place in the queue, so newer clips can come after it). */
const STARTED_RANK: Partial<Record<UploadState, number>> = { uploading: 0, queued: 1 };
export const inUploadOrder = (a: Clip, b: Clip) =>
  (STARTED_RANK[a.upload_state] ?? 2) - (STARTED_RANK[b.upload_state] ?? 2) ||
  (a.next_upload ?? "").localeCompare(b.next_upload ?? "");

/** Approved clips: being rendered, ready to upload, scheduled, and uploaded. */
export function Approved() {
  const { data: clips, error, refresh } = usePoll(
    () => api.clips(["approved", "rendering", "failed", "rendered", "uploaded"]),
    5000,
  );
  const rendered = clips?.filter((c) => c.status === "rendered") ?? [];
  // Failed uploads and renders first, so they don't get lost among the rest.
  const attention = clips?.filter((c) => c.status === "failed" || (c.status === "rendered" && c.upload_state === "failed")) ?? [];
  const ready = rendered.filter((c) => !GOING_UP.includes(c.upload_state) && c.upload_state !== "failed");
  const scheduled = rendered.filter((c) => GOING_UP.includes(c.upload_state)).sort(inUploadOrder);
  const working = clips?.filter((c) => ["approved", "rendering"].includes(c.status)) ?? [];
  const uploaded = clips?.filter((c) => c.status === "uploaded") ?? [];

  return (
    <Screen title="Upload" subtitle="Approved clips, ready to post to YouTube." wide>
      <div className="lg:max-w-md">
        <WorkStrip stacked parts={["renders", "uploads"]} className="rounded-2xl bg-zinc-900 p-1.5 lg:hidden" />
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
      {clips?.length === 0 && <p className="mt-10 text-center text-sm text-zinc-500">Nothing approved yet.</p>}
      <Section id="upload:attention" title="Needs attention" tone="bg-red-500/15 text-red-300" clips={attention}
        footer={(c) => (c.status === "failed" ? <RenderFooter clip={c} onChanged={refresh} /> : <UploadFooter clip={c} onChanged={refresh} />)} />
      <Section id="upload:ready" title="Ready to upload" clips={ready} footer={(c) => <UploadFooter clip={c} onChanged={refresh} />} />
      <Section id="upload:scheduled" title="Scheduled" clips={scheduled} footer={(c) => <UploadFooter clip={c} onChanged={refresh} />} />
      <Section id="upload:rendering" title="Rendering" clips={working} footer={(c) => <RenderFooter clip={c} onChanged={refresh} />} />
      {/* Grows with every upload, so it starts folded and shows a dozen at a time. */}
      <Section id="upload:uploaded" title="Uploaded" clips={uploaded} defaultOpen={false} footer={(c) => <UploadedFooter clip={c} />} />
    </Screen>
  );
}

function Section(props: {
  id: string;
  title: string;
  clips: Clip[];
  tone?: string;
  defaultOpen?: boolean;
  footer: (c: Clip) => ReactNode;
}) {
  if (props.clips.length === 0) return null;
  return (
    <Fold id={props.id} title={props.title} count={props.clips.length} tone={props.tone} defaultOpen={props.defaultOpen}>
      <ShowMore items={props.clips}>
        {(shown) => (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {shown.map((c) => (
              <ClipCard key={c.id} clip={c} to={`upload/${c.id}`} large showStatus footer={props.footer(c)} />
            ))}
          </div>
        )}
      </ShowMore>
    </Fold>
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
        "flex flex-1 items-center justify-center gap-1.5 py-2.5 text-sm font-medium hover:bg-white/[0.03] active:bg-white/5",
        props.tone === "danger" ? "text-red-300" : props.tone === "accent" ? "font-semibold text-violet-300" : "text-zinc-400",
      )}
    >
      {props.children}
    </button>
  );
}

/** What's happening with a clip's upload, after the channel it goes to. */
function uploadLine(clip: Clip): string {
  const at = clip.next_upload ? upcoming(clip.next_upload) : "";
  const why = clip.upload_error ? ` (${firstSentence(clip.upload_error)})` : "";
  return {
    "": "",
    uploading: "uploading now",
    queued: "goes up as soon as the upload running is done",
    scheduled: `goes up ${at}`,
    retrying: `retries ${at}, in its turn${clip.upload_error ? ` · failed: ${firstSentence(clip.upload_error)}` : ""}`,
    failed: `upload failed${why}: open it to try again`,
  }[clip.upload_state];
}

function UploadFooter({ clip, onChanged }: { clip: Clip; onChanged: () => void }) {
  const act = useClipAction(onChanged);
  const line = uploadLine(clip);
  return (
    <div className="border-t border-white/5">
      <p className="truncate px-3 pt-2 text-xs text-zinc-500">
        {clip.destination_name ? `To ${clip.destination_name}` : "No channel chosen yet"}
        {line && <span className={clip.upload_state === "failed" ? "text-red-300" : "text-sky-300"}> · {line}</span>}
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
      {clip.rumble_url && (
        <a href={clip.rumble_url} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 font-semibold text-emerald-300">
          <LinkIcon className="size-4" /> On Rumble
        </a>
      )}
      {clip.youtube_url && (
        <a href={clip.youtube_url} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 font-semibold text-emerald-300">
          <LinkIcon className="size-4" /> On YouTube
        </a>
      )}
    </div>
  );
}
