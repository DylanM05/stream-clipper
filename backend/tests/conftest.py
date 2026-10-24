from clipper.models import Transcript, TranscriptSegment, Word

WORD_STEP = 0.35  # seconds from one word's start to the next
WORD_LEN = 0.3


def line(start: float, text: str) -> TranscriptSegment:
    """A transcript line whose words follow each other every WORD_STEP seconds."""
    words = [
        Word(start=round(start + i * WORD_STEP, 2), end=round(start + i * WORD_STEP + WORD_LEN, 2), text=w)
        for i, w in enumerate(text.split())
    ]
    return TranscriptSegment(start=start, end=words[-1].end, text=text, words=words)


def transcript(*lines: TranscriptSegment, duration: float | None = None) -> Transcript:
    return Transcript(language="en", duration=duration or lines[-1].end + 5, segments=list(lines))
