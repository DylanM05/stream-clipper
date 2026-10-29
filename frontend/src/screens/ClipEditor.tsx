import { useEffect, useState } from "react";
import { api, errorText, type NewClipInfo, type Vod } from "../api";
import { DesktopEditor } from "../editor/DesktopEditor";
import { PhoneEditor } from "../editor/PhoneEditor";
import { useEditor, type Source } from "../editor/useEditor";
import { useDesktop, usePoll } from "../lib";
import { ErrorNote } from "../ui";

/** Editing a clip: its ends, the parts cut out of it, its title and where it goes. */
export function ClipEditor({ id }: { id: number }) {
  const { data: clip, error, setData } = usePoll(() => api.clip(id), 5000, id);
  if (error && !clip) return <div className="p-6"><ErrorNote>{error}</ErrorNote></div>;
  if (!clip) return null;
  return <Editor source={{ mode: "edit", clip, onSaved: setData }} />;
}

/** Making a clip from any part of a stream, optionally starting from `from` to `to` in it. */
export function NewClip({ vodId, from, to }: { vodId: number; from?: number; to?: number }) {
  const [loaded, setLoaded] = useState<{ vod: Vod; info: NewClipInfo }>();
  const [error, setError] = useState("");
  useEffect(() => {
    Promise.all([api.vod(vodId), api.newClipInfo(vodId)]).then(
      ([detail, info]) => setLoaded({ vod: detail.vod, info }),
      (e) => setError(errorText(e)),
    );
  }, [vodId]);
  if (error) return <div className="p-6"><ErrorNote>{error}</ErrorNote></div>;
  if (!loaded) return null;
  if (loaded.vod.status !== "ready" || loaded.vod.cleaned) {
    return (
      <div className="p-6">
        <ErrorNote>
          {loaded.vod.cleaned
            ? "This stream's video files were deleted to free space, so no more clips can be made from it."
            : "Clips can be made from this video once it's ready."}
        </ErrorNote>
      </div>
    );
  }
  return <Editor source={{ mode: "new", vod: loaded.vod, info: loaded.info, from, to }} />;
}

function Editor({ source }: { source: Source }) {
  const desktop = useDesktop();
  const ed = useEditor(source, desktop);
  return desktop ? <DesktopEditor ed={ed} /> : <PhoneEditor ed={ed} />;
}
