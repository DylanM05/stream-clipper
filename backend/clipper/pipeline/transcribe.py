"""Transcribe the audio with faster-whisper on the GPU, keeping a timestamp for every word."""

import logging
from collections.abc import Callable

from ..config import Settings
from ..models import Transcript, TranscriptSegment, Word
from ..timecode import to_timecode
from ..workspace import Workspace, write_text
from . import audio

log = logging.getLogger(__name__)


def run(ws: Workspace, settings: Settings, on_progress: Callable[[float], None] | None = None) -> Transcript:
    if ws.transcript_json.exists():
        return Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8"))

    # Imported here because it pulls in CUDA libraries, which only the server image has.
    from faster_whisper import WhisperModel

    log.info("Loading Whisper model %r on %s", settings.whisper_model, settings.whisper_device)
    model = WhisperModel(
        settings.whisper_model, device=settings.whisper_device, compute_type=settings.whisper_compute_type
    )
    pieces, info = model.transcribe(
        # Samples rather than a path: faster-whisper's own decoder breaks with some PyAV versions.
        audio.load(ws),
        language=settings.language,
        word_timestamps=True,
        vad_filter=True,
        # Stops one misheard phrase from repeating through hours of audio.
        condition_on_previous_text=False,
    )

    segments: list[TranscriptSegment] = []
    next_report = 0.05
    for piece in pieces:
        segments.append(TranscriptSegment(
            start=round(piece.start, 2),
            end=round(piece.end, 2),
            text=piece.text.strip(),
            words=[Word(start=round(w.start, 2), end=round(w.end, 2), text=w.word.strip()) for w in piece.words or []],
        ))
        if on_progress and info.duration:
            on_progress(min(1.0, piece.end / info.duration))
        if info.duration and piece.end / info.duration >= next_report:
            log.info("Transcribed %s / %s (%d%%)", to_timecode(piece.end), to_timecode(info.duration), piece.end / info.duration * 100)
            next_report += 0.05

    transcript = Transcript(language=info.language, duration=info.duration, segments=segments)
    write_text(ws.transcript_json, transcript.model_dump_json())
    log.info("Transcript has %d lines and %d words", len(segments), sum(len(s.words) for s in segments))
    return transcript
