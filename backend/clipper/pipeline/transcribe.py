"""Transcribe the audio with faster-whisper on the GPU, keeping a timestamp for every word.

The model runs on the GPU, but faster-whisper prepares its input (the samples, speech detection and the
spectrogram) on the CPU for all the audio it's given at once: several GB for an 8-hour stream. So the
audio goes in 20-minute windows, which keeps that to a few hundred MB however long the stream is.
What's done is saved after each window, so a restart or pause carries on from there.
"""

import logging
from collections.abc import Callable

from ..config import Settings
from ..models import Transcript, TranscriptSegment, Word
from ..timecode import to_timecode
from ..workspace import Workspace, read_json, write_json, write_text
from . import audio

log = logging.getLogger(__name__)

WINDOW_S = 20 * 60
# Each window gets this much more audio, so the line running across its end is heard whole. The next
# window starts where the last line kept ended.
OVERLAP_S = 30


def run(ws: Workspace, settings: Settings, on_progress: Callable[[float], None] | None = None) -> Transcript:
    if ws.transcript_json.exists():
        return Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8"))

    # Imported here because it pulls in CUDA libraries, which only the server image has.
    from faster_whisper import WhisperModel

    log.info("Loading Whisper model %r on %s", settings.whisper_model, settings.whisper_device)
    model = WhisperModel(
        settings.whisper_model, device=settings.whisper_device, compute_type=settings.whisper_compute_type
    )
    total = audio.duration(ws)
    language = settings.language
    segments: list[TranscriptSegment] = []
    pos = 0.0
    if ws.transcript_partial_json.exists():
        saved = read_json(ws.transcript_partial_json)
        pos, language = saved["pos"], saved["language"] or language
        segments = [TranscriptSegment.model_validate(s) for s in saved["segments"]]
        log.info("Carrying on transcribing from %s", to_timecode(pos))
    next_report = (int(pos / total * 20) + 1) / 20 if total else 1
    while pos < total:
        last = pos + WINDOW_S >= total
        pieces, info = model.transcribe(
            # Samples rather than a path: faster-whisper's own decoder breaks with some PyAV versions.
            audio.load(ws, start_s=pos, duration_s=WINDOW_S + OVERLAP_S),
            language=language,
            word_timestamps=True,
            vad_filter=True,
            # Stops one misheard phrase from repeating through hours of audio.
            condition_on_previous_text=False,
        )
        language = language or info.language  # detected once, from the first window
        kept_until = pos
        for piece in pieces:
            if not last and piece.start >= WINDOW_S:
                break  # the next window starts with this line
            segments.append(TranscriptSegment(
                start=round(pos + piece.start, 2),
                end=round(pos + piece.end, 2),
                text=piece.text.strip(),
                words=[Word(start=round(pos + w.start, 2), end=round(pos + w.end, 2), text=w.word.strip())
                       for w in piece.words or []],
            ))
            kept_until = pos + piece.end
            if on_progress:
                on_progress(min(1.0, kept_until / total))
            if kept_until / total >= next_report:
                log.info("Transcribed %s / %s (%d%%)", to_timecode(kept_until), to_timecode(total), kept_until / total * 100)
                next_report += 0.05
        if last:
            break
        pos = kept_until if kept_until > pos else pos + WINDOW_S  # a window with no speech is skipped whole
        write_json(ws.transcript_partial_json,
                   {"pos": pos, "language": language, "segments": [s.model_dump() for s in segments]})
    if on_progress:
        on_progress(1.0)

    transcript = Transcript(language=language, duration=total, segments=segments)
    write_text(ws.transcript_json, transcript.model_dump_json())
    ws.transcript_partial_json.unlink(missing_ok=True)
    log.info("Transcript has %d lines and %d words", len(segments), sum(len(s.words) for s in segments))
    return transcript
