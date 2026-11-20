Pick every distinct moment or sustained sequence that earns its place in a compilation, with no quota or per-hour cap.
Return the pieces under `highlights`, in stream order, with no chapters.

A piece can be a quick beat or a complete story, conversation or sequence that needs up to 20 minutes of footage
after cuts. Let the material determine the length. Keep a related setup, progression, payoff and immediate reaction
together in one piece, even if there's a removable interruption between them. If a sequence needs more than 20
minutes after cuts, look for natural beats that can stand as separate pieces. Use the timestamps of the first and
last transcript lines to include (the end includes the named line). Do not overlap pieces.

Use each piece's `cuts` to skip internal dead air, tangents, breaks and repeated beats when the story still makes
sense across the join. For each cut, `after` is the timestamp of the last transcript line to keep before the gap
and `before` is the timestamp of the first line to keep when the piece resumes. Both lines stay in the piece.
Choose clean joins between complete thoughts. A pause in speech may contain important action or a reaction, so
use the surrounding context and signals before cutting it. Use an empty list when there's no filler to remove.

Give each piece a plain, short, descriptive title for the compilation's chapters and set `vertical_ok` to false.
An empty highlights list is fine when nothing earns a place.
