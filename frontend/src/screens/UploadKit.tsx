import { useState, type ReactNode } from "react";
import { api, errorText, media, type Clip, type Destination, type UploadJob, type Visibility } from "../api";
import { CopyIcon, DownloadIcon, LinkIcon, PhoneIcon, ShareIcon, UploadIcon } from "../icons";
import { ago, cn, copyText, fileName, formatBytes, goBack, loadSetting, saveSetting, usePoll } from "../lib";
import { Button, DestinationSelect, ErrorNote, ProgressBar, Screen, useToast } from "../ui";

const MAX_SHARE_BYTES = 1e9; // bigger than this is a job for a computer

/**
 * Posting one clip: the channel it goes to, then either an automatic upload by the worker (through the
 * YouTube API, or YouTube Studio for channels not connected to it) or everything needed to upload it by hand.
 */
export function UploadKit({ id }: { id: number }) {
  const { data: clip, error, setData } = usePoll(() => api.clip(id), 5000, id);
  const { data: destinations } = usePoll(api.destinations, 0);
  if (!clip) return error ? <div className="p-6"><ErrorNote>{error}</ErrorNote></div> : null;
  return <Kit clip={clip} destinations={destinations} onChange={setData} />;
}

function Kit({ clip, destinations, onChange }: { clip: Clip; destinations?: Destination[]; onChange: (c: Clip) => void }) {
  const toast = useToast();
  const [which, setWhich] = useState<"video" | "short">("video");
  const [link, setLink] = useState(clip.youtube_url);
  const destination = destinations?.find((d) => d.id === clip.destination_id);
  const size = which === "video" ? clip.video_size : clip.short_size;
  const rendered = clip.status === "rendered" || clip.status === "uploaded";

  const act = async (work: () => Promise<Clip>, done: string) => {
    try {
      onChange(await work());
      toast({ text: done });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  const copy = async (text: string, what: string) => toast({ text: (await copyText(text)) ? `${what} copied` : "Couldn't copy" });

  return (
    <Screen title="Upload" subtitle={clip.title} onBack={() => goBack("approved")}>
      {!rendered && <ErrorNote>This clip isn't rendered yet. Come back once it shows as ready.</ErrorNote>}

      <Step n={1} title="Channel">
        <DestinationSelect
          value={clip.destination_id}
          destinations={destinations}
          onChange={(destination_id) => act(() => api.updateClip(clip.id, { destination_id }), "Channel changed")}
        />
        {destination ? (
          <a
            href={destination.studio_upload_url}
            target="_blank"
            rel="noreferrer"
            className="mt-2 flex items-center gap-1.5 text-sm font-medium text-violet-300"
          >
            <LinkIcon className="size-4" /> Open YouTube Studio's upload page
          </a>
        ) : (
          <p className="mt-2 text-xs text-zinc-500">Pick the channel this goes on. Add channels under Shows → YouTube channels.</p>
        )}
        <p className="mt-1 text-xs text-zinc-500">Make sure YouTube is signed in to this channel before you upload.</p>
      </Step>

      <Step n={2} title="Upload automatically">
        <AutoUpload clip={clip} destination={destination} />
      </Step>

      <div className="mb-6 flex items-center gap-3 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        <span className="h-px flex-1 bg-white/10" /> Or upload by hand <span className="h-px flex-1 bg-white/10" />
      </div>

      <Step n={3} title="Video file">
        {clip.has_short && (
          <div className="mb-3 grid grid-cols-2 gap-1 rounded-xl bg-zinc-800 p-1">
            {(["video", "short"] as const).map((w) => (
              <button
                key={w}
                onClick={() => setWhich(w)}
                className={cn("rounded-lg py-2 text-sm font-semibold", which === w ? "bg-zinc-600 text-white" : "text-zinc-400")}
              >
                {w === "video" ? "Video (16:9)" : "Short (9:16)"}
              </button>
            ))}
          </div>
        )}
        {rendered && size > 0 && (
          <video
            key={which}
            src={media.file(clip, which)}
            controls
            playsInline
            preload="metadata"
            className={cn("w-full rounded-xl bg-black", which === "short" ? "mx-auto aspect-[9/16] max-h-96 w-auto" : "aspect-video")}
          />
        )}
        {rendered && size === 0 && (
          <p className="text-sm text-zinc-500">
            {clip.status === "uploaded" ? "The file was deleted once it was uploaded, to save space." : "The file isn't there any more."}
          </p>
        )}
        <div className="mt-3 grid gap-2">
          {size > 0 && <ShareToPhone clip={clip} which={which} size={size} />}
          <a
            href={media.file(clip, which, true)}
            className={cn(
              "inline-flex items-center justify-center gap-1.5 rounded-xl bg-zinc-800 px-4 py-3 text-sm font-semibold active:bg-zinc-700",
              (!rendered || size === 0) && "pointer-events-none opacity-40",
            )}
          >
            <DownloadIcon className="size-4" /> Download{size ? ` (${formatBytes(size)})` : ""}
          </a>
        </div>
        <p className="mt-2 text-xs text-zinc-500">
          The file is named after the title, so YouTube Studio fills the title in for you.
        </p>
      </Step>

      <Step n={4} title="Title and description">
        <CopyBox label="Title" text={clip.title} onCopy={() => copy(clip.title, "Title")} />
        <CopyBox label="Description" text={clip.description} onCopy={() => copy(clip.description, "Description")} long />
      </Step>

      <Step n={5} title="Done?">
        {clip.status === "uploaded" ? (
          <div className="space-y-2">
            <p className="text-sm text-emerald-300">Marked as uploaded{clip.destination_name ? ` to ${clip.destination_name}` : ""}.</p>
            {clip.youtube_url && (
              <a href={clip.youtube_url} target="_blank" rel="noreferrer" className="block truncate text-sm text-violet-300">
                {clip.youtube_url}
              </a>
            )}
            <Button
              onClick={() =>
                act(
                  () => api.unmarkUploaded(clip.id),
                  clip.has_video ? "Moved back to Ready to upload" : "Its file was deleted after uploading, so it renders again first",
                )
              }
            >
              Not uploaded yet
            </Button>
          </div>
        ) : (
          <div className="space-y-2">
            <input
              type="url"
              inputMode="url"
              value={link}
              onChange={(e) => setLink(e.target.value)}
              placeholder="YouTube link (optional)"
              className="w-full rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 placeholder:text-zinc-500 focus:ring-2 focus:ring-violet-500"
            />
            <Button tone="approve" disabled={!rendered} onClick={() => act(() => api.markUploaded(clip.id, link), "Marked as uploaded")} className="w-full">
              Mark as uploaded
            </Button>
          </div>
        )}
      </Step>
    </Screen>
  );
}

const VISIBILITY_LABEL: Record<Visibility, string> = { public: "Public", unlisted: "Unlisted", private: "Private" };
const JOB_STATUS: Record<UploadJob["status"], string> = {
  queued: "Waiting its turn",
  uploading: "Uploading",
  done: "Uploaded",
  failed: "Failed",
};
const METHOD_LABEL: Record<UploadJob["method"], string> = { api: " · YouTube API", studio: " · Studio", "": "" };

/** The worker uploads through the YouTube API if the channel is connected, else through YouTube Studio. */
function AutoUpload({ clip, destination }: { clip: Clip; destination?: Destination }) {
  const toast = useToast();
  const { data: jobs, refresh } = usePoll(() => api.uploads(clip.id), 4000, clip.id);
  const [visibility, setVisibility] = useState<Visibility>(() => loadSetting("clipper.uploadVisibility", "public"));
  const busy = (which: UploadJob["which"]) => jobs?.some((j) => j.which === which && ["queued", "uploading"].includes(j.status));
  const ready = clip.status === "rendered" || clip.status === "uploaded";

  const run = async (work: () => Promise<unknown>, done?: string) => {
    try {
      await work();
      if (done) toast({ text: done });
      void refresh();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };
  const start = (which: UploadJob["which"]) => {
    saveSetting("clipper.uploadVisibility", visibility);
    return run(() => api.startUpload(clip.id, which, visibility), "Queued. The worker uploads it next.");
  };

  return (
    <div className="space-y-3 rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
      {destination?.api_connected ? (
        <div className="flex items-start gap-2 text-xs">
          <span className="mt-1 size-2 shrink-0 rounded-full bg-emerald-400" />
          <span className="text-zinc-400">Uploads through the YouTube API as “{destination.api_account}”</span>
        </div>
      ) : (
        <>
          <UploadBrowserStatus />
          {destination && (
            <p className="text-xs text-zinc-500">
              Connect this channel to the YouTube API under Shows → YouTube channels to upload without the browser.
            </p>
          )}
          {destination && !destination.channel_id && (
            <p className="text-xs text-amber-300">
              This channel's ID isn't known, so it goes to whichever channel the upload browser has selected. Fix the
              link under Shows → YouTube channels.
            </p>
          )}
        </>
      )}
      {destination?.api_error && <p className="text-xs text-red-300">{destination.api_error}</p>}
      <div className="grid grid-cols-3 gap-1 rounded-xl bg-zinc-800 p-1">
        {(Object.keys(VISIBILITY_LABEL) as Visibility[]).map((v) => (
          <button
            key={v}
            onClick={() => setVisibility(v)}
            className={cn("rounded-lg py-2 text-sm font-semibold", visibility === v ? "bg-zinc-600 text-white" : "text-zinc-400")}
          >
            {VISIBILITY_LABEL[v]}
          </button>
        ))}
      </div>
      <div className="grid gap-2">
        <Button tone="primary" disabled={!ready || busy("video") || !clip.destination_id} onClick={() => start("video")}>
          <UploadIcon className="size-4" /> Upload {clip.kind === "whole" ? "the full episode" : "the video"} ({VISIBILITY_LABEL[visibility]})
        </Button>
        {clip.has_short && (
          <Button disabled={!ready || busy("short") || !clip.destination_id} onClick={() => start("short")}>
            <PhoneIcon className="size-4" /> Upload the Short ({VISIBILITY_LABEL[visibility]})
          </Button>
        )}
        {!clip.destination_id && <p className="text-xs text-zinc-500">Pick a channel above first.</p>}
      </div>

      {jobs && jobs.length > 0 && (
        <ul className="divide-y divide-white/5 rounded-xl bg-zinc-950/50">
          {jobs.map((job) => (
            <li key={job.id} className="space-y-2 p-3 text-sm">
              <div className="flex items-center gap-2">
                <span className="font-semibold">{job.which === "short" ? "Short" : "Video"}</span>
                <span className="text-zinc-500">
                  · {VISIBILITY_LABEL[job.visibility]}
                  {METHOD_LABEL[job.method]}
                </span>
                <span
                  className={cn(
                    "ml-auto text-xs font-semibold",
                    job.status === "done" ? "text-emerald-300" : job.status === "failed" ? "text-red-300" : "text-amber-300",
                  )}
                >
                  {JOB_STATUS[job.status]}
                  {job.status === "uploading" && job.progress > 0 && ` ${Math.round(job.progress * 100)}%`}
                </span>
              </div>
              {job.status === "uploading" && <ProgressBar value={job.progress || null} />}
              {job.status === "done" && job.youtube_url && (
                <a href={job.youtube_url} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 text-violet-300">
                  <LinkIcon className="size-4" /> {job.youtube_url}
                </a>
              )}
              {job.status === "done" && job.error && <p className="break-words text-xs text-amber-300">{job.error}</p>}
              {job.status === "failed" && (
                <>
                  <p className="break-words text-xs text-red-300">{job.error}</p>
                  <div className="flex gap-2">
                    <button onClick={() => run(() => api.retryUpload(job.id), "Queued again")} className="rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-semibold active:bg-zinc-700">
                      Retry
                    </button>
                    {job.has_screenshot && (
                      <a href={media.uploadScreenshot(job)} target="_blank" rel="noreferrer" className="rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-semibold active:bg-zinc-700">
                        What Studio showed
                      </a>
                    )}
                    <button onClick={() => run(() => api.cancelUpload(job.id))} className="rounded-lg px-3 py-1.5 text-xs font-semibold text-zinc-400">
                      Dismiss
                    </button>
                  </div>
                </>
              )}
              {job.status === "queued" && (
                <button onClick={() => run(() => api.cancelUpload(job.id), "Cancelled")} className="text-xs font-semibold text-zinc-400">
                  Cancel
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Whether the upload browser is up and signed in to YouTube (or to X, which it reads posts from), with
 * buttons to open it (on a desktop, or its view on the server) and to check again after signing in.
 */
export function UploadBrowserStatus({ site = "youtube" }: { site?: "youtube" | "x" }) {
  const toast = useToast();
  const { data: studio, setData } = usePoll(api.studio, 15000);
  const [checking, setChecking] = useState(false);
  if (!studio) return null;

  const siteName = site === "x" ? "X" : "YouTube";
  const signedIn = site === "x" ? studio.x_signed_in : studio.signed_in;
  const ok = studio.browser && signedIn;
  const message = !studio.checked_at
    ? "The upload browser hasn't been checked yet."
    : !studio.browser
      ? "The upload browser isn't open."
      : !signedIn
        ? `The upload browser isn't signed in to ${siteName} yet.`
        : `Upload browser signed in to ${siteName} · checked ${ago(studio.checked_at)}`;

  const open = async () => {
    if (studio.browser_view_url) {
      window.open(studio.browser_view_url, "_blank", "noreferrer");
      return;
    }
    try {
      await api.openStudioBrowser();
      toast({ text: `Opened a separate browser window. Sign in to ${siteName} there, then tap Check again.` });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };
  const check = async () => {
    setChecking(true);
    try {
      setData(await api.checkStudio());
    } catch (e) {
      toast({ text: errorText(e) });
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-start gap-2 text-xs">
        <span className={cn("mt-1 size-2 shrink-0 rounded-full", ok ? "bg-emerald-400" : "bg-amber-400")} />
        <span className={ok ? "text-zinc-400" : "text-amber-300"}>{message}</span>
      </div>
      <div className="flex gap-2">
        {!ok && (
          <button onClick={open} className="rounded-lg bg-violet-600/80 px-3 py-1.5 text-xs font-semibold active:bg-violet-700">
            {studio.browser ? "Show the upload browser" : "Open the upload browser"}
          </button>
        )}
        <button onClick={check} disabled={checking} className="rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-semibold active:bg-zinc-700">
          {checking ? "Checking…" : "Check again"}
        </button>
      </div>
    </div>
  );
}

/**
 * Phones can hand a video file to the YouTube app through the share sheet. The file has to be fetched
 * first, and sharing needs a fresh tap afterwards, so it's two buttons: get it ready, then share.
 */
function ShareToPhone({ clip, which, size }: { clip: Clip; which: "video" | "short"; size: number }) {
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const canShareFiles =
    typeof navigator.canShare === "function" && navigator.canShare({ files: [new File([""], "test.mp4", { type: "video/mp4" })] });
  if (!canShareFiles) return null;
  if (size > MAX_SHARE_BYTES) {
    return <p className="text-xs text-zinc-500">At {formatBytes(size)} this is too big to share from a phone; use a computer.</p>;
  }

  const name = `${fileName(clip.title)}${which === "short" ? " (Short)" : ""}.mp4`;
  const prepare = async () => {
    try {
      setProgress(0);
      const res = await fetch(media.file(clip, which));
      const reader = res.body!.getReader();
      const chunks: BlobPart[] = [];
      let received = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        received += value.length;
        setProgress(size ? received / size : null);
      }
      setFile(new File(chunks, name, { type: "video/mp4" }));
    } catch (e) {
      toast({ text: errorText(e) });
    } finally {
      setProgress(null);
    }
  };
  const share = async () => {
    await copyText(clip.title);
    try {
      await navigator.share({ files: [file!], title: clip.title });
    } catch (e) {
      if ((e as Error).name !== "AbortError") toast({ text: errorText(e) });
    }
  };

  if (file && file.name === name) {
    return (
      <Button tone="primary" onClick={share}>
        <ShareIcon className="size-4" /> Share to YouTube app (title is copied)
      </Button>
    );
  }
  return (
    <Button tone="primary" onClick={prepare} disabled={progress !== null}>
      <ShareIcon className="size-4" />
      {progress !== null ? `Getting it ready… ${Math.round(progress * 100)}%` : "Get it ready to share from this phone"}
    </Button>
  );
}

function CopyBox({ label, text, onCopy, long }: { label: string; text: string; onCopy: () => void; long?: boolean }) {
  return (
    <div className="mb-3 rounded-xl bg-zinc-900 ring-1 ring-white/5">
      <div className="flex items-center justify-between px-3 pt-2.5">
        <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">{label}</span>
        <button onClick={onCopy} className="flex items-center gap-1 rounded-lg px-2 py-1 text-xs font-semibold text-violet-300 active:bg-white/5">
          <CopyIcon className="size-3.5" /> Copy
        </button>
      </div>
      <p className={cn("whitespace-pre-wrap break-words px-3 pb-3 pt-1 text-sm", long ? "max-h-48 overflow-y-auto text-zinc-300" : "font-semibold")}>
        {text || <span className="text-zinc-500">(empty)</span>}
      </p>
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <section className="mb-6">
      <h2 className="mb-2 flex items-center gap-2 text-sm font-bold">
        <span className="grid size-6 place-items-center rounded-full bg-violet-600 text-xs">{n}</span>
        {title}
      </h2>
      {children}
    </section>
  );
}
