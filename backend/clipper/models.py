"""Data shapes passed between the pipeline steps."""

from typing import Literal

from pydantic import BaseModel, Field

# --- Transcription ---------------------------------------------------------


class Word(BaseModel):
    start: float
    end: float
    text: str


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    words: list[Word] = []


class Transcript(BaseModel):
    language: str | None
    duration: float
    segments: list[TranscriptSegment]

    def words(self) -> list[Word]:
        return [w for seg in self.segments for w in seg.words]


# --- Signals: hints for the AI about where segments start -------------------


class Span(BaseModel):
    start: float
    end: float


class PlatformChapter(Span):
    title: str


class LoudMoment(BaseModel):
    t: float
    db_above: float  # how far above the surrounding loudness


class SceneCut(BaseModel):
    t: float
    score: float


class Signals(BaseModel):
    platform_chapters: list[PlatformChapter] = []
    long_silences: list[Span] = []
    loud_moments: list[LoudMoment] = []
    scene_cuts: list[SceneCut] = []


# --- What Claude returns ------------------------------------------------------
# Constraints like title length and score range are enforced in refine.py rather
# than here, so a slightly-off answer doesn't cost a whole retry of an 8-hour transcript.

_TIMECODE = "HH:MM:SS, copied from the transcript line where it happens"


class LLMChapter(BaseModel):
    start: str = Field(description=_TIMECODE)
    end: str = Field(description=_TIMECODE)
    category: Literal["content", "pre_stream", "break", "outro"] = Field(
        description="Only 'content' chapters become videos"
    )
    title: str = Field(description="YouTube title for this chapter as its own video, within the task's title length")
    summary: str = Field(description="1-3 factual sentences, usable as the video description")
    # The default keeps answers saved before it existed readable; the AI is always asked for it.
    why: str = Field(default="", description="What would make people watch this as its own video, or hold it back")
    confidence: float = Field(description="0-1: how sure you are about this chapter's boundaries")
    # The default keeps answers saved before it existed readable; Claude is always asked for it.
    score: float = Field(
        default=0, description="0-1: how likely this chapter is to do well as its own video. Reserve 0.9+ for standout ones"
    )


class LLMHighlight(BaseModel):
    start: str = Field(description=_TIMECODE)
    end: str = Field(description=_TIMECODE)
    title: str = Field(description="Hook-style title, within the task's title length")
    description: str = Field(description="1-2 sentences for the video description")
    why: str = Field(description="Why this moment works as a clip")
    score: float = Field(description="0-1: how likely this clip is to do well. Reserve 0.9+ for standout moments")
    vertical_ok: bool = Field(
        description="Works as a vertical Short: doesn't depend on reading small on-screen detail"
    )


class Segmentation(BaseModel):
    chapters: list[LLMChapter]
    highlights: list[LLMHighlight]
    # Defaults keep answers saved before these existed readable; Claude is always asked for them.
    episode_title: str = Field(default="", description="Title for the whole stream as one video, within the task's length")
    episode_summary: str = Field(default="", description="2-4 sentences describing the whole episode, for its description")


class LLMBreakdownPart(BaseModel):
    start: str = Field(description=_TIMECODE)
    end: str = Field(description=_TIMECODE)
    category: str = Field(description="One of the show's breakdown categories, by its name as given, or Other")
    note: str = Field(description="What happens in it, in a few words")


_OTHER_SUMMARY = "1-2 sentences on what the parts under Other were mostly about; empty if none are"


class SegmentationWithBreakdown(Segmentation):
    """The same, plus what the stream's time goes on, for a show that asks for a breakdown (Show.breakdown)."""

    # The defaults keep answers without them readable; the AI is always asked for them when this is the schema.
    breakdown: list[LLMBreakdownPart] = Field(default=[], description="The whole stream, in order, each part by category")
    other_summary: str = Field(default="", description=_OTHER_SUMMARY)


class Breakdown(BaseModel):
    """The breakdown asked for on its own, for a stream whose clips were picked already (segment.break_down)."""

    breakdown: list[LLMBreakdownPart] = Field(description="The whole stream, in order, each part by category")
    other_summary: str = Field(default="", description=_OTHER_SUMMARY)  # the default keeps older answers readable


# --- Final clips, after cut points are snapped to word boundaries -----------


class Clip(BaseModel):
    id: str  # ch01, hl03, my01, ... or "full"
    kind: Literal["chapter", "highlight", "whole"]  # "whole": the entire video, posted as it is
    start: float
    end: float
    title: str
    description: str
    score: float  # how likely it is to do well, per Claude
    why: str = ""  # why the AI picked it
    vertical_ok: bool = False
    llm_start: str
    llm_end: str
    cuts: list[tuple[float, float]] = []  # parts taken out of the middle in the app (cuts.py), in the stream's seconds

    @property
    def duration(self) -> float:
        """How long it comes out: start to end, less the cuts."""
        return self.end - self.start - sum(b - a for a, b in self.cuts)
