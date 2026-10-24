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
    confidence: float = Field(description="0-1: how sure you are about this chapter's boundaries")


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


# --- Final clips, after cut points are snapped to word boundaries -----------


class Clip(BaseModel):
    id: str  # ch01, hl03, ... or "full"
    kind: Literal["chapter", "highlight", "whole"]  # "whole": the entire video, posted as it is
    start: float
    end: float
    title: str
    description: str
    score: float  # chapter confidence or highlight score
    why: str = ""  # highlights: why Claude picked the moment
    vertical_ok: bool = False
    llm_start: str
    llm_end: str

    @property
    def duration(self) -> float:
        return self.end - self.start
