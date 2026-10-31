import { useState } from "react";
import { api, errorText, type Destination } from "./api";
import { cn, usePoll } from "./lib";
import { Button, useToast } from "./ui";

const NEW = "__new__";

/**
 * Pick one of a channel's YouTube playlists for uploads to go into, or make a new one. Only channels connected
 * to the YouTube API have this; uploads through YouTube Studio can't be put in a playlist automatically.
 */
export function PlaylistSelect(props: { destination?: Destination; value: string; onChange: (id: string) => void }) {
  const { destination } = props;
  if (!destination) return null;
  if (!destination.api_connected) {
    return (
      <p className="mt-2 text-xs text-zinc-500">
        To put uploads in a playlist, connect {destination.name} to the YouTube API under Shows → YouTube channels.
      </p>
    );
  }
  return <Picker key={destination.id} destId={destination.id} value={props.value} onChange={props.onChange} />;
}

function Picker({ destId, value, onChange }: { destId: number; value: string; onChange: (id: string) => void }) {
  const toast = useToast();
  const { data: playlists, error, setData } = usePoll(() => api.playlists(destId), 0, destId);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    try {
      const made = await api.createPlaylist(destId, title.trim(), "public");
      setData([made, ...(playlists ?? [])]);
      onChange(made.id);
      setCreating(false);
      setTitle("");
      toast({ text: `Made the playlist “${made.title}”` });
    } catch (e) {
      toast({ text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  if (error) return <p className="mt-2 text-xs text-red-300">Couldn't load its playlists: {error}</p>;
  const known = !value || playlists?.some((p) => p.id === value);

  return (
    <div className="mt-2 space-y-2">
      <select
        value={creating ? NEW : value}
        onChange={(e) => {
          if (e.target.value === NEW) setCreating(true);
          else {
            setCreating(false);
            onChange(e.target.value);
          }
        }}
        className="w-full appearance-none rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5 focus:ring-2 focus:ring-violet-500"
      >
        <option value="">{playlists ? "No playlist" : "Loading playlists…"}</option>
        {playlists?.map((p) => (
          <option key={p.id} value={p.id}>
            {p.title} ({p.count} video{p.count === 1 ? "" : "s"}{p.privacy && p.privacy !== "public" ? `, ${p.privacy}` : ""})
          </option>
        ))}
        {!known && <option value={value}>A playlist that isn't on this channel any more</option>}
        <option value={NEW}>+ New playlist…</option>
      </select>
      {creating && (
        <div className="flex gap-2">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Playlist name, e.g. Night Owls pre-shows"
            maxLength={150}
            className={cn(
              "min-w-0 flex-1 rounded-xl bg-zinc-900 px-3.5 py-3 text-base outline-none ring-1 ring-white/5",
              "placeholder:text-zinc-500 focus:ring-2 focus:ring-violet-500",
            )}
          />
          <Button tone="primary" disabled={!title.trim() || busy} onClick={create}>
            {busy ? "Making…" : "Make"}
          </Button>
        </div>
      )}
    </div>
  );
}
