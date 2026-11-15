import { useState, type ReactNode } from "react";
import { api, errorText, type Backlog, type BacklogItem, type BacklogOrder, type VideoType } from "../api";
import { ChevronIcon, FolderIcon, LibraryIcon, LinkIcon } from "../icons";
import { cn, go, goBack, goInstead, pastDay, usePoll } from "../lib";
import { Button, Chip, ErrorNote, Panel, ProgressBar, Screen, Segmented, useToast, VIDEO_TYPE_LABEL, VodStatusChip } from "../ui";
import { BLANK_SHOW, channelName, PLATFORM_NAME } from "./Shows";

/**
 * The Library: folders of videos on the server, such as a TV show's seasons, and channels' past videos, each worked
 * through a few a day by itself, in order, for a show (backend/clipper/monitor.py). Each video then goes the way a new
 * stream does: transcribed, the AI's picks to the inbox, rendered and uploaded.
 */

const HISTORY = ["kick", "twitch", "youtube"]; // can list every past video (monitor.HISTORY_PLATFORMS)
const FOLDER_ORDER: Record<BacklogOrder, string> = { first: "In order", latest: "Latest first", random: "At random" };
const CHANNEL_ORDER: Record<BacklogOrder, string> = { first: "Oldest first", latest: "Newest first", random: "At random" };
const DAY_MS = 86400000;

export function Library() {
  const { data: backlogs, error } = usePoll(api.library, 15000);
  return (
    <Screen
      title="Library"
      subtitle="Folders and channels' past videos, worked through a few a day."
      actions={<Button tone="primary" onClick={() => go("library/new")}>Add</Button>}
      wide
    >
      {error && <ErrorNote>{error}</ErrorNote>}
      {backlogs?.length === 0 && (
        <div className="mt-10 flex flex-col items-center text-center">
          <div className="rounded-full bg-zinc-900 p-5 ring-1 ring-white/5">
            <LibraryIcon className="size-8 text-zinc-500" />
          </div>
          <h2 className="mt-4 text-lg font-semibold">Nothing in the Library yet</h2>
          <p className="mt-1 max-w-sm text-sm text-zinc-400">
            Add a folder of videos on the server, such as a TV show's seasons, or a channel's past streams. They're worked
            through a few a day, in order, and each one's clips come to the inbox like a new stream's.
          </p>
          <Button tone="primary" className="mt-5" onClick={() => go("library/new")}>
            Add to the Library
          </Button>
        </div>
      )}
      <div className="space-y-3 xl:grid xl:grid-cols-2 xl:items-start xl:gap-3 xl:space-y-0">
        {backlogs?.map((b) => (
          <button
            key={b.channel.id}
            onClick={() => go(`library/${b.channel.id}`)}
            className="block w-full rounded-2xl bg-zinc-900 p-4 text-left ring-1 ring-white/5 hover:bg-zinc-800/40 active:bg-zinc-800/70"
          >
            <div className="flex items-center gap-2">
              <h3 className="min-w-0 flex-1 truncate font-semibold">{b.name}</h3>
              <span className="text-xs text-zinc-500">{PLATFORM_NAME[b.channel.platform]}</span>
            </div>
            <p className="mt-0.5 truncate text-sm text-zinc-400">
              {b.show_name} · {VIDEO_TYPE_LABEL[b.video_type]}
            </p>
            <Progress backlog={b} className="mt-3" />
          </button>
        ))}
      </div>
    </Screen>
  );
}

/** The Library on the overview: how far each backlog is. Only there when it has something. */
export function LibraryPanel() {
  const { data: backlogs } = usePoll(api.library, 60000);
  if (!backlogs?.length) return null;
  return (
    <Panel title="Library" link={{ href: "#/library", label: "All" }}>
      <ul className="divide-y divide-white/5">
        {backlogs.map((b) => (
          <li key={b.channel.id}>
            <a href={`#/library/${b.channel.id}`} className="block px-4 py-3 hover:bg-white/[0.03]">
              <p className="truncate text-sm font-medium">{b.name}</p>
              <Progress backlog={b} className="mt-2" />
            </a>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

/** How far through it is, and what it does next. */
function Progress({ backlog: b, className }: { backlog: Backlog; className?: string }) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <ProgressBar value={b.total ? b.done / b.total : 0} />
      <p className="text-xs text-zinc-400">
        {b.done} of {b.total} done
        {b.working > 0 && ` · ${b.working} being worked on`}
        {b.failed > 0 && <span className="text-red-300"> · {b.failed} failed</span>}
        {b.skipped > 0 && ` · ${b.skipped} skipped`}
      </p>
      <p className={cn("text-xs", b.channel.backlog_paused && b.left ? "text-amber-300" : "text-zinc-500")}>{nextLine(b)}</p>
      {b.channel.last_error && <p className="line-clamp-2 break-words text-xs text-red-300">{b.channel.last_error}</p>}
    </div>
  );
}

function nextLine(b: Backlog): string {
  const ch = b.channel;
  if (!ch.history_listed && HISTORY.includes(ch.platform)) return "Listing the channel's past videos…";
  if (!b.total && !b.skipped && !ch.last_checked_at) return "Looking through it…";
  if (!b.left) {
    const coming = b.copying ? `, with ${b.copying} ${ch.platform === "folder" ? "being copied in" : "not finished yet"}` : "";
    return `All through${coming}. New ones are processed as they come.`;
  }
  if (ch.backlog_paused) return `Paused, with ${b.left} to go`;
  return `Next: ${b.next} · ${ch.backlog_per_day} a day · through around ${finishDay(b.left, ch.backlog_per_day, b.today)}`;
}

/** Roughly when the backlog is through at its pace: what's left of today's go first, then the rest a day at a time. */
function finishDay(left: number, perDay: number, today: number): string {
  const room = Math.max(0, perDay - today);
  const days = left <= room ? 0 : Math.ceil((left - room) / perDay);
  if (days <= 0) return "today";
  if (days === 1) return "tomorrow";
  const day = new Date(Date.now() + days * DAY_MS);
  return day.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(day.getFullYear() !== new Date().getFullYear() && { year: "numeric" }),
  });
}

// --- One backlog: its videos, and what to do with them ---

export function BacklogPage({ id }: { id: number }) {
  const toast = useToast();
  const { data: b, error, refresh } = usePoll(() => api.backlog(id), 10000, id);
  const [settings, setSettings] = useState(false);

  const act = async (work: () => Promise<unknown>, done?: string, undo?: () => Promise<unknown>) => {
    try {
      await work();
      if (done) toast({ text: done, action: undo && { label: "Undo", run: () => void act(undo) } });
      await refresh();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  if (!b) {
    return (
      <Screen title="Library" onBack={() => goBack("library")}>
        {error ? <ErrorNote>{error}</ErrorNote> : <p className="text-sm text-zinc-500">Loading…</p>}
      </Screen>
    );
  }
  const ch = b.channel;
  const folder = ch.platform === "folder";
  const left = b.items.filter((i) => i.state === "left");
  const first = left.find((i) => i.place === 1);
  const upNext = new Set(ch.backlog_paused ? [] : left.filter((i) => i.place! <= ch.backlog_per_day).map((i) => i.id));
  const groups = groupItems(b.items);

  const skip = (items: BacklogItem[]) => {
    const ids = items.map((i) => i.id);
    void act(() => api.skipInBacklog(ch.id, ids, true), ids.length === 1 ? "Skipped" : `Skipped ${ids.length}`,
      () => api.skipInBacklog(ch.id, ids, false));
  };
  const putBack = (items: BacklogItem[]) => {
    const ids = items.map((i) => i.id);
    void act(() => api.skipInBacklog(ch.id, ids, false), ids.length === 1 ? "Put back" : `Put back ${ids.length}`,
      () => api.skipInBacklog(ch.id, ids, true));
  };
  const actions: ItemActions = {
    process: (item) => void act(() => api.processChannelVideo(item.id), "Queued. Follow it under Videos."),
    skip: (item) => skip([item]),
    putBack: (item) => putBack([item]),
    startHere: (item) => {
      const before = b.items.slice(0, b.items.indexOf(item)).filter((i) => i.state === "left");
      const after = ch.backlog_order === "random" ? "so only it and the ones after it are picked from" : "so it starts here";
      if (before.length && confirm(`Skip the ${before.length} before it, ${after}?`)) skip(before);
    },
  };

  return (
    <Screen title={b.name} subtitle={`${b.show_name} · ${VIDEO_TYPE_LABEL[b.video_type]}`} onBack={() => goBack("library")}>
      <section className="rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
        <Progress backlog={b} />
        <div className="mt-4 grid grid-cols-[1fr_1.4fr_1fr] gap-2">
          <Button
            onClick={() => act(() => api.updateChannel(ch.id, { backlog_paused: !ch.backlog_paused }), ch.backlog_paused ? "Carrying on" : "Paused")}
          >
            {ch.backlog_paused ? "Resume" : "Pause"}
          </Button>
          <Button disabled={!first} onClick={() => actions.process(first!)}>
            Process next
          </Button>
          <Button onClick={() => setSettings((s) => !s)}>Settings</Button>
        </div>
        {settings && <BacklogSettings key={ch.id} backlog={b} act={act} />}
      </section>

      <div className="mt-6 space-y-3">
        {groups.map((g, i) => (
          <ItemGroup
            key={`${i}:${g.name}`}
            name={g.name || (folder ? "In the folder itself" : "No date")}
            items={g.items}
            upNext={upNext}
            folder={folder}
            startOpen={groups.length === 1 || g.items.some((i) => upNext.has(i.id) || i.state === "working" || i.state === "failed")}
            actions={actions}
            onSkip={skip}
            onPutBack={putBack}
          />
        ))}
      </div>
    </Screen>
  );
}

function BacklogSettings({ backlog: b, act }: { backlog: Backlog; act: (work: () => Promise<unknown>, done?: string) => Promise<void> }) {
  const ch = b.channel;
  const folder = ch.platform === "folder";
  const [pace, setPace] = useState(ch.backlog_per_day); // shown at once, while it saves
  const changePace = (n: number) => {
    setPace(n);
    void act(() => api.updateChannel(ch.id, { backlog_per_day: n }));
  };
  const remove = () => {
    if (folder) {
      if (confirm("Stop watching this folder? What's been processed stays under Videos.")) {
        void act(() => api.deleteChannel(ch.id), "Removed").then(() => go("library"));
      }
    } else if (confirm(`Take it out of the Library? ${b.show_name} still watches it for new videos, and its older ones wait for you.`)) {
      void act(() => api.updateChannel(ch.id, { backlog: false }), "Taken out of the Library").then(() => go("library"));
    }
  };
  return (
    <div className="mt-4 space-y-4 border-t border-white/5 pt-4">
      <Group label="A day" hint="older ones; new ones never wait">
        <Stepper value={pace} onChange={changePace} />
      </Group>
      <Group label="Order">
        <div className="flex">
          <Segmented
            options={folder ? FOLDER_ORDER : CHANNEL_ORDER}
            value={ch.backlog_order}
            onChange={(backlog_order) => void act(() => api.updateChannel(ch.id, { backlog_order }))}
          />
        </div>
        <p className="mt-1.5 text-xs text-zinc-500">{orderHint(ch.backlog_order, folder)}</p>
      </Group>
      <p className="text-xs text-zinc-500">
        Titles, the AI's instructions and where the clips go are the show's:{" "}
        <a href={`#/shows/${ch.show_id}`} className="font-medium text-violet-300">
          {b.show_name}
        </a>
        .
      </p>
      <Button tone="danger" className="w-full" onClick={remove}>
        {folder ? "Remove the folder" : "Take it out of the Library"}
      </Button>
    </div>
  );
}

type ItemActions = Record<"process" | "skip" | "putBack" | "startHere", (item: BacklogItem) => void>;

/** A backlog's items one after the other, in groups: a folder's subfolders (seasons) or a channel's months. */
function groupItems(items: BacklogItem[]): { name: string; items: BacklogItem[] }[] {
  const groups: { name: string; items: BacklogItem[] }[] = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && last.name === item.group) last.items.push(item);
    else groups.push({ name: item.group, items: [item] });
  }
  return groups;
}

function ItemGroup(props: {
  name: string;
  items: BacklogItem[];
  upNext: Set<number>;
  folder: boolean;
  startOpen: boolean;
  actions: ItemActions;
  onSkip: (items: BacklogItem[]) => void;
  onPutBack: (items: BacklogItem[]) => void;
}) {
  const [open, setOpen] = useState(props.startOpen);
  const left = props.items.filter((i) => i.state === "left");
  const skipped = props.items.filter((i) => i.state === "skipped");
  const done = props.items.filter((i) => i.state === "done").length;
  return (
    <section className="rounded-2xl bg-zinc-900 ring-1 ring-white/5">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-3 px-4 py-3 text-left" aria-expanded={open}>
        <ChevronIcon className={cn("size-4 shrink-0 text-zinc-500 transition-transform", !open && "-rotate-90")} />
        <span className="min-w-0 flex-1 truncate text-sm font-semibold">{props.name}</span>
        <span className="shrink-0 text-xs text-zinc-500">
          {done} of {props.items.length - skipped.length} done
        </span>
      </button>
      {open && (
        <>
          <ul className="divide-y divide-white/5 border-t border-white/5">
            {props.items.map((item) => (
              <ItemRow key={item.id} item={item} next={props.upNext.has(item.id)} folder={props.folder} actions={props.actions} />
            ))}
          </ul>
          {(left.length > 1 || skipped.length > 1) && (
            <div className="flex divide-x divide-white/5 border-t border-white/5 text-sm">
              {left.length > 1 && <GroupAction onClick={() => props.onSkip(left)}>Skip these {left.length}</GroupAction>}
              {skipped.length > 1 && <GroupAction onClick={() => props.onPutBack(skipped)}>Put back {skipped.length} skipped</GroupAction>}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function GroupAction({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button onClick={onClick} className="flex-1 py-2.5 font-medium text-zinc-300 hover:bg-white/[0.03] active:bg-white/5">
      {children}
    </button>
  );
}

const STATE: Record<BacklogItem["state"], [string, string]> = {
  done: ["Done", "bg-emerald-500/15 text-emerald-300"],
  working: ["Processing", "bg-sky-500/15 text-sky-300"],
  failed: ["Failed", "bg-red-500/15 text-red-300"],
  left: ["To do", "bg-zinc-500/15 text-zinc-400"],
  skipped: ["Skipped", "bg-zinc-500/10 text-zinc-500"],
  copying: ["Being copied in", "bg-amber-500/15 text-amber-300"],
};

/** One video: tap it for what can be done with it. */
function ItemRow({ item, next, folder, actions }: { item: BacklogItem; next: boolean; folder: boolean; actions: ItemActions }) {
  const [open, setOpen] = useState(false);
  const meta = [
    !folder && item.published_at && pastDay(item.published_at),
    item.state === "done" && clipsLine(item.counts),
    (item.state === "skipped" || item.state === "copying") && item.reason,
  ].filter(Boolean);
  const [label, tone] = STATE[item.state];
  return (
    <li>
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-white/[0.02]">
        <div className="min-w-0 flex-1">
          <p className={cn("line-clamp-2 break-words text-sm leading-snug", item.state === "skipped" && "text-zinc-500")}>{item.title}</p>
          {meta.length > 0 && <p className="mt-0.5 truncate text-xs text-zinc-500">{meta.join(" · ")}</p>}
        </div>
        {item.state === "working" && item.vod_status ? (
          <VodStatusChip status={item.vod_status} />
        ) : item.state === "left" && next ? (
          <Chip tone="bg-violet-500/15 text-violet-300">Next</Chip>
        ) : (
          <Chip tone={tone}>{item.state === "copying" && !folder ? "Not finished" : label}</Chip>
        )}
      </button>
      {open && (
        <div className="flex flex-wrap gap-2 px-4 pb-3">
          {item.state === "left" && (
            <>
              <Small tone="primary" onClick={() => actions.process(item)}>Process now</Small>
              <Small onClick={() => actions.skip(item)}>Skip</Small>
              <Small onClick={() => actions.startHere(item)}>Start from here</Small>
            </>
          )}
          {item.state === "skipped" && <Small onClick={() => actions.putBack(item)}>Put back</Small>}
          {item.vod_id !== null && <Small onClick={() => go(`videos/${item.vod_id}`)}>Open</Small>}
          {!folder && (
            <a href={item.url} target="_blank" rel="noreferrer" className={cn(SMALL, "inline-flex items-center gap-1")}>
              Watch <LinkIcon className="size-3" />
            </a>
          )}
        </div>
      )}
    </li>
  );
}

/** "3 to review, 2 uploaded": what became of a processed video's clips. */
function clipsLine(counts: BacklogItem["counts"]): string {
  const parts = [counts.proposed && `${counts.proposed} to review`, counts.uploaded && `${counts.uploaded} uploaded`].filter(Boolean);
  const all = Object.values(counts).reduce((sum, n) => sum + (n ?? 0), 0);
  return parts.length ? parts.join(", ") : all ? `${all} clip${all === 1 ? "" : "s"}` : "No clips";
}

const SMALL = "rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-semibold text-zinc-200 hover:bg-zinc-700/80 active:bg-zinc-700";

function Small({ onClick, tone, children }: { onClick: () => void; tone?: "primary"; children: ReactNode }) {
  return (
    <button onClick={onClick} className={cn(SMALL, tone === "primary" && "bg-mark text-ink hover:bg-violet-400 active:bg-violet-600")}>
      {children}
    </button>
  );
}

// --- Adding a folder or a channel's past videos ---

export function AddToLibrary() {
  const toast = useToast();
  const { data: shows } = usePoll(api.shows, 0);
  const [source, setSource] = useState<"folder" | "channel">("folder");
  const [folder, setFolder] = useState<string | null>(null);
  const [link, setLink] = useState("");
  const [showId, setShowId] = useState<number | "new" | null>(null);
  const [newName, setNewName] = useState("");
  const [kind, setKind] = useState<VideoType>("episodes");
  const [perDay, setPerDay] = useState(2);
  const [order, setOrder] = useState<BacklogOrder>("first");
  const [busy, setBusy] = useState(false);

  const show = typeof showId === "number" ? shows?.find((s) => s.id === showId) : undefined;
  // Channels already watched, whose past videos could be worked through.
  const watched = (shows ?? []).flatMap((s) => s.channels.filter((c) => c.platform !== "folder" && !c.backlog).map((c) => ({ show: s, channel: c })));
  const ready = (source === "folder" ? !!folder : !!link.trim()) && (showId === "new" ? !!newName.trim() : !!show);

  const switchSource = (to: "folder" | "channel") => {
    setSource(to);
    setKind(to === "folder" ? "episodes" : "streams");
  };

  const add = async () => {
    setBusy(true);
    try {
      let id = showId as number;
      if (showId === "new") {
        id = (await api.addShow({ ...BLANK_SHOW, name: newName.trim(), title_template: "{title}", video_type: kind })).id;
        setShowId(id); // so trying again doesn't make it twice
      }
      const channel = await api.addChannel(id, {
        url: source === "folder" ? folder! : link.trim(),
        backlog: true,
        backlog_per_day: perDay,
        backlog_order: order,
      });
      toast({ text: "In the Library. The first ones start within a minute." });
      goInstead(`library/${channel.id}`);
    } catch (e) {
      toast({ text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Screen title="Add to the Library" onBack={() => goBack("library")}>
      <div className="space-y-6">
        <Group label="What to work through">
          <div className="flex">
            <Segmented options={{ folder: "A folder on the server", channel: "A channel's past videos" }} value={source} onChange={switchSource} />
          </div>
        </Group>

        {source === "folder" ? (
          <FolderPicker value={folder} onChange={setFolder} />
        ) : (
          <Group label="Channel">
            <input
              type="url"
              inputMode="url"
              value={link}
              onChange={(e) => setLink(e.target.value)}
              placeholder="Its link, e.g. youtube.com/@name"
              className={input}
            />
            <p className="mt-1.5 text-xs text-zinc-500">
              YouTube, Twitch and Kick list every past video; Rumble and X only their newest few, and Twitch keeps past streams
              for 7 to 60 days. The channel is watched for new videos too, which are processed as they come.
            </p>
            {watched.length > 0 && (
              <div className="mt-3">
                <p className="text-xs text-zinc-500">Or one a show already watches:</p>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {watched.map(({ show: s, channel: c }) => (
                    <button
                      key={c.id}
                      onClick={() => {
                        setLink(c.url);
                        setShowId(s.id);
                      }}
                      className={cn(
                        "rounded-full px-2.5 py-1 text-xs",
                        link === c.url ? "bg-violet-500/20 text-violet-200" : "bg-zinc-800 text-zinc-300 hover:bg-zinc-700",
                      )}
                    >
                      {PLATFORM_NAME[c.platform]} · {channelName(c)} <span className="text-zinc-500">({s.name})</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </Group>
        )}

        <Group label="For the show" hint="its titles, AI notes and upload channel">
          <select
            value={showId ?? ""}
            onChange={(e) => setShowId(e.target.value === "new" ? "new" : e.target.value ? Number(e.target.value) : null)}
            className={cn(input, "appearance-none")}
          >
            <option value="">Choose a show…</option>
            {shows?.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
            <option value="new">A new show…</option>
          </select>
          {showId === "new" && (
            <div className="mt-3 space-y-3">
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Its name, e.g. Cartoon Shorts" className={input} />
              <div className="flex">
                <Segmented options={VIDEO_TYPE_LABEL} value={kind} onChange={setKind} />
              </div>
              <p className="text-xs text-zinc-500">Its titles, upload channel and the rest can be set on its page once it's added.</p>
            </div>
          )}
          {show && (
            <p className="mt-1.5 text-xs text-zinc-500">
              Its videos are {VIDEO_TYPE_LABEL[show.video_type].toLowerCase()}, for the AI; that's changed on{" "}
              <a href={`#/shows/${show.id}`} className="font-medium text-violet-300">
                its page
              </a>
              .
            </p>
          )}
        </Group>

        <Group label="A day" hint="older ones; new ones never wait">
          <Stepper value={perDay} onChange={setPerDay} />
        </Group>

        <Group label="Order">
          <div className="flex">
            <Segmented options={source === "folder" ? FOLDER_ORDER : CHANNEL_ORDER} value={order} onChange={setOrder} />
          </div>
          <p className="mt-1.5 text-xs text-zinc-500">{orderHint(order, source === "folder")}</p>
        </Group>

        <Button tone="primary" onClick={add} disabled={!ready || busy} className="w-full">
          {busy ? "Adding…" : "Start working through it"}
        </Button>
      </div>
    </Screen>
  );
}

/** Choose a folder in the server's media folder: browse into it, then see what watching it would find. */
function FolderPicker({ value, onChange }: { value: string | null; onChange: (path: string | null) => void }) {
  const [path, setPath] = useState<string>();
  const { data: here, error } = usePoll(() => api.folders(path), 0, path);
  const { data: found, error: findError } = usePoll(() => (value ? api.folderVideos(value) : Promise.resolve(null)), 0, value);

  if (value) {
    return (
      <Group label="Folder">
        <div className="rounded-2xl bg-zinc-900 p-4 ring-1 ring-white/5">
          <p className="flex items-center gap-2 break-all text-sm font-semibold">
            <FolderIcon className="size-4 shrink-0 text-zinc-400" />
            {value}
          </p>
          {findError && <p className="mt-2 text-xs text-red-300">{findError}</p>}
          {found && (
            <div className="mt-2 text-xs text-zinc-400">
              <p>{found.videos === 0 ? "No videos in it or its folders." : `${found.videos} video${found.videos === 1 ? "" : "s"}, in this order:`}</p>
              <ol className="mt-1 space-y-0.5 text-zinc-500">
                {found.first.map((name) => (
                  <li key={name} className="truncate">
                    {name}
                  </li>
                ))}
                {found.videos > found.first.length && <li>and {found.videos - found.first.length} more</li>}
              </ol>
            </div>
          )}
          <button onClick={() => onChange(null)} className="mt-3 text-sm font-medium text-violet-300">
            Choose another
          </button>
        </div>
      </Group>
    );
  }
  return (
    <Group label="Folder" hint="on the server">
      {error && <ErrorNote>{error}</ErrorNote>}
      {here && !here.parent && !here.folders.length && !here.videos && (
        <p className="mb-3 rounded-xl bg-amber-500/10 p-3 text-sm text-amber-200 ring-1 ring-amber-500/20">
          The media folder is empty. Set CLIPPER_HOST_MEDIA_DIR in the server's .env to the folder your videos are in (a
          full path), then run docker compose up -d.
        </p>
      )}
      {here && (
        <div className="rounded-2xl bg-zinc-900 ring-1 ring-white/5">
          <div className="flex items-center gap-2 px-4 py-3">
            <FolderIcon className="size-4 shrink-0 text-zinc-400" />
            <p className="min-w-0 flex-1 break-all text-sm font-semibold">{here.path}</p>
            {here.parent && (
              <button onClick={() => setPath(here.parent!)} className="shrink-0 text-sm font-medium text-violet-300">
                Up
              </button>
            )}
          </div>
          {here.folders.length > 0 && (
            <ul className="max-h-80 divide-y divide-white/5 overflow-y-auto border-t border-white/5">
              {here.folders.map((name) => (
                <li key={name}>
                  <button
                    onClick={() => setPath(`${here.path}/${name}`)}
                    className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm hover:bg-white/[0.03] active:bg-white/5"
                  >
                    <FolderIcon className="size-4 shrink-0 text-zinc-500" />
                    <span className="min-w-0 flex-1 truncate">{name}</span>
                    <ChevronIcon className="size-4 shrink-0 -rotate-90 text-zinc-600" />
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="flex items-center gap-3 border-t border-white/5 px-4 py-3">
            <p className="flex-1 text-xs text-zinc-500">
              {here.videos ? `${here.videos} video${here.videos === 1 ? "" : "s"} here` : here.folders.length ? "" : "No videos in it"}
              {here.folders.length > 0 && (here.videos ? ", and its folders' too" : "Its folders' videos are included")}
            </p>
            <Button tone="primary" className="shrink-0 py-2" onClick={() => onChange(here.path)}>
              Choose this folder
            </Button>
          </div>
        </div>
      )}
    </Group>
  );
}

function orderHint(order: BacklogOrder, folder: boolean): string {
  if (order === "random") return `Any ${folder ? "episode" : "video"} still to go, picked at random.`;
  if (folder) return order === "first" ? "Season by season, from the first episode." : "From the last episode back.";
  return order === "first" ? "From the oldest video." : "From the newest video back.";
}

function Stepper({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  return (
    <div className="flex items-center gap-2">
      <Button onClick={() => onChange(Math.max(1, value - 1))} disabled={value <= 1} className="w-12">
        −
      </Button>
      <span className="flex-1 text-center text-sm font-semibold">{value} a day</span>
      <Button onClick={() => onChange(Math.min(100, value + 1))} disabled={value >= 100} className="w-12">
        +
      </Button>
    </div>
  );
}

const input =
  "w-full rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 placeholder:text-zinc-500 focus:ring-2 focus:ring-violet-500";

/** A caption over a group of controls. */
function Group({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div>
      <p className="mb-1.5 flex items-baseline justify-between gap-3 text-xs font-medium text-zinc-500">
        {label}
        {hint && <span className="text-right font-normal normal-case">{hint}</span>}
      </p>
      {children}
    </div>
  );
}
