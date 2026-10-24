import type { ReactNode } from "react";
import { api, errorText, type Clip } from "../api";
import { ClipCard } from "../ClipCard";
import { LinkIcon, UploadIcon } from "../icons";
import { go, usePoll } from "../lib";
import { ErrorNote, Screen, useToast } from "../ui";

/** Approved clips: being rendered, ready to upload, and uploaded. */
export function Approved() {
  const { data: clips, error, refresh } = usePoll(
    () => api.clips(["approved", "rendering", "failed", "rendered", "uploaded"]),
    5000,
  );
  const ready = clips?.filter((c) => c.status === "rendered") ?? [];
  const working = clips?.filter((c) => ["approved", "rendering", "failed"].includes(c.status)) ?? [];
  const uploaded = clips?.filter((c) => c.status === "uploaded") ?? [];

  return (
    <Screen title="Upload" subtitle="Approved clips, ready to post to YouTube.">
      {error && <ErrorNote>{error}</ErrorNote>}
      {clips?.length === 0 && <p className="mt-10 text-center text-sm text-zinc-500">Nothing approved yet.</p>}
      <Section title="Ready to upload" clips={ready} footer={(c) => <UploadFooter clip={c} />} />
      <Section title="Rendering" clips={working} footer={(c) => <RenderFooter clip={c} onRetry={refresh} />} />
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

function UploadFooter({ clip }: { clip: Clip }) {
  return (
    <button
      onClick={() => go(`upload/${clip.id}`)}
      className="flex w-full items-center gap-2 border-t border-white/5 px-3 py-2.5 text-left text-sm active:bg-white/5"
    >
      <span className="min-w-0 flex-1 truncate text-zinc-400">
        {clip.destination_name ? `To ${clip.destination_name}` : "No channel chosen yet"}
      </span>
      <span className="flex items-center gap-1.5 font-semibold text-violet-300">
        <UploadIcon className="size-4" /> Upload
      </span>
    </button>
  );
}

function RenderFooter({ clip, onRetry }: { clip: Clip; onRetry: () => void }) {
  const toast = useToast();
  if (clip.status !== "failed") return null;
  return (
    <div className="space-y-2 border-t border-white/5 p-3">
      <p className="line-clamp-3 break-words text-xs text-red-300">{clip.error}</p>
      <button
        onClick={async () => {
          try {
            await api.approve(clip.id);
            onRetry();
          } catch (e) {
            toast({ text: errorText(e) });
          }
        }}
        className="rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-semibold active:bg-zinc-700"
      >
        Render again
      </button>
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
