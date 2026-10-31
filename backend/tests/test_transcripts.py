"""Transcripts saved for use outside the app, with the AI's breakdown of what each stream's time goes on."""

import json

import pytest
from conftest import line, transcript

from clipper.config import Settings
from clipper.db import Show, Vod
from clipper.models import Breakdown, LLMBreakdownPart, Segmentation, SegmentationWithBreakdown, Signals
from clipper.pipeline import segment
from clipper import transcripts
from clipper.workspace import Workspace, read_json, write_json, write_text

TRANSCRIPT = transcript(line(5.0, "Hello chat."), line(65.0, "Big news today."), duration=600)
CATEGORIES = "News: going through headlines\n\nSponsors: paid ad reads\nGameplay"


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def ws(settings):
    ws = Workspace.open(settings.vods_dir, "kick-123")
    write_json(ws.meta_json, {"title": "Stream", "channel": "Creator", "extractor_key": "Kick", "upload_date": "20261030"})
    return ws


class FakeAI:
    name = "Fake"

    def __init__(self, answer):
        self.answer, self.asked = answer, {}

    def structured(self, **kwargs):
        self.asked = kwargs
        return self.answer


def test_breakdown_categories_are_read_one_per_line_and_only_when_transcripts_are_saved():
    show = Show(name="Night Owls", save_transcripts=True, breakdown=CATEGORIES + "\n  : no name\n")
    assert show.breakdown_categories() == {"News": "going through headlines", "Sponsors": "paid ad reads", "Gameplay": ""}
    show.save_transcripts = False
    assert show.breakdown_categories() == {}


def test_the_categories_go_in_the_ais_input_after_the_shows_guidance():
    text = segment.build_input({}, TRANSCRIPT, Signals(), guidance="One clip per game.",
                               breakdown={"News": "going through headlines", "Gameplay": ""})
    assert "One clip per game.\n\n# Breakdown categories\n- News: going through headlines\n- Gameplay\n" in text
    assert "Breakdown" not in segment.build_input({}, TRANSCRIPT, Signals())


@pytest.mark.parametrize("breakdown", [None, {"News": "going through headlines"}])
def test_the_breakdown_is_asked_for_in_the_same_request_only_for_shows_with_categories(settings, ws, monkeypatch, breakdown):
    fake = FakeAI(SegmentationWithBreakdown(chapters=[], highlights=[]))
    monkeypatch.setattr(segment.ai, "model", lambda *a, **k: fake)
    segment.run(ws, settings, TRANSCRIPT, Signals(), breakdown=breakdown)

    asked = fake.asked
    plain = segment.PROMPTS_DIR.joinpath("segment.md").read_text(encoding="utf-8")
    if breakdown:
        assert asked["schema"] is SegmentationWithBreakdown and asked["system_prompt"].startswith(plain)
        assert "## Breakdown" in asked["system_prompt"]
        assert asked["task"].startswith("Split this 00:10:00 stream into chapters and pick up to 3 highlights, "
                                        "and break down what its time goes on. ")
    else:  # just as before breakdowns existed
        assert asked["schema"] is Segmentation and asked["system_prompt"] == plain
        assert asked["task"].startswith("Split this 00:10:00 stream into chapters and pick up to 3 highlights. ")


def test_the_saved_transcript_has_the_breakdown_in_seconds_under_the_shows_categories(settings, ws):
    answer = SegmentationWithBreakdown(chapters=[], highlights=[], breakdown=[
        LLMBreakdownPart(start="00:00:00", end="00:01:05", category="other", note="Says hello"),
        LLMBreakdownPart(start="00:01:05", end="00:04:00", category=" news ", note="Reads the headlines"),
        LLMBreakdownPart(start="00:04:00", end="later", category="News", note="Not a timecode"),
        LLMBreakdownPart(start="00:04:00", end="00:20:00", category="Cooking", note="Not one of the show's"),
    ])
    write_text(ws.segmentation_json, answer.model_dump_json())
    show = Show(name="Night Owls", save_transcripts=True, breakdown=CATEGORIES)
    path = transcripts.save(settings, show, Vod(url="https://kick.com/creator/videos/123"), ws, TRANSCRIPT)

    assert path == settings.transcripts_dir / "night-owls" / "2026-10-30_kick-123.json"
    saved = read_json(path)
    assert saved["categories"] == {"News": "going through headlines", "Sponsors": "paid ad reads", "Gameplay": ""}
    assert saved["breakdown"] == [
        {"start": 0, "end": 65, "category": "Other", "note": "Says hello"},
        {"start": 65, "end": 240, "category": "News", "note": "Reads the headlines"},
        {"start": 240, "end": 600, "category": "Other", "note": "Not one of the show's"},  # cut at the stream's end
    ]
    assert path.with_suffix(".txt").read_text(encoding="utf-8") == "[00:00:05] Hello chat.\n[00:01:05] Big news today.\n"


def test_a_stream_picked_through_before_the_show_had_categories_has_an_empty_breakdown(settings, ws):
    write_text(ws.segmentation_json, Segmentation(chapters=[], highlights=[]).model_dump_json())
    show = Show(name="Night Owls", save_transcripts=True, breakdown=CATEGORIES)
    saved = read_json(transcripts.save(settings, show, Vod(url="u"), ws, TRANSCRIPT))
    assert saved["breakdown"] == [] and "News" in saved["categories"]


def test_each_saved_line_has_its_words_timings_so_pauses_inside_it_can_be_measured(settings, ws):
    path = transcripts.save(settings, Show(name="Night Owls", save_transcripts=True), Vod(url="u"), ws, TRANSCRIPT)
    assert read_json(path)["segments"][0]["words"] == [[5.0, 5.3], [5.35, 5.65]]  # "Hello chat."
    assert "\n" not in path.read_text(encoding="utf-8")  # compact: a long stream's word timings run to megabytes


def part(start, end, category, note=""):
    return LLMBreakdownPart(start=start, end=end, category=category, note=note)


def test_a_breakdown_asked_for_on_its_own_replaces_the_one_with_the_clips_and_categories_match_however_written(settings, ws):
    write_text(ws.segmentation_json, SegmentationWithBreakdown(
        chapters=[], highlights=[], breakdown=[part("00:00:00", "00:10:00", "News")]).model_dump_json())
    write_text(ws.breakdown_json, Breakdown(other_summary="Small talk with chat.", breakdown=[
        part("00:00:00", "00:05:00", "dead air"), part("00:05:00", "00:10:00", "Dead_Air")]).model_dump_json())
    show = Show(name="Night Owls", save_transcripts=True, breakdown="Dead-air: waiting around")
    saved = read_json(transcripts.save(settings, show, Vod(url="u"), ws, TRANSCRIPT))
    assert [p["category"] for p in saved["breakdown"]] == ["Dead-air", "Dead-air"]
    assert saved["other_summary"] == "Small talk with chat."


def test_breaking_a_stream_down_on_its_own_asks_only_for_the_breakdown(settings, ws, monkeypatch):
    fake = FakeAI(Breakdown(breakdown=[part("00:00:00", "00:10:00", "News", "Headlines")]))
    monkeypatch.setattr(segment.ai, "model", lambda *a, **k: fake)
    parts = segment.break_down(ws, settings, TRANSCRIPT, {"News": "going through headlines"}, context="The host is Sam.")

    assert [p.note for p in parts] == ["Headlines"]
    asked = fake.asked
    assert asked["schema"] is Breakdown and asked["system_prompt"].startswith("You work out what a long livestream's time")
    assert "## Breakdown" in asked["system_prompt"] and asked["task"].startswith("Break down what this 00:10:00 stream")
    assert "# About the show\nThe host is Sam." in asked["stdin_text"] and "- News: going through headlines" in asked["stdin_text"]
    assert transcripts.parts(ws) == parts  # kept in llm/breakdown.json


class TestBreakdownCommand:
    @pytest.fixture
    def setup(self, settings, monkeypatch):
        from sqlmodel import Session

        from clipper import cli
        from clipper.db import get_engine

        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        asked, used = [], []
        self.used = used

        def break_down(ws, settings_, transcript, categories, context="", model=None, effort=None):
            asked.append(ws.root.name)
            used.append((model, effort))
            write_text(ws.breakdown_json, Breakdown(breakdown=[part("00:00:00", "00:10:00", "News")]).model_dump_json())
            return transcripts.parts(ws)

        monkeypatch.setattr(cli.segment, "break_down", break_down)
        with Session(get_engine(settings)) as s:
            tracked = Show(name="Night Owls", save_transcripts=True, breakdown=CATEGORIES)
            other = Show(name="Other show")
            s.add_all([tracked, other])
            s.commit()
            ids = {}
            for name, show, has_one in (("kick-1", tracked, False), ("kick-2", tracked, True), ("kick-3", other, False)):
                ws = Workspace.open(settings.vods_dir, name)
                write_json(ws.meta_json, {"title": name, "upload_date": "20261030"})
                write_text(ws.transcript_json, TRANSCRIPT.model_dump_json())
                if has_one:
                    write_text(ws.segmentation_json, SegmentationWithBreakdown(
                        chapters=[], highlights=[], breakdown=[part("00:00:00", "00:10:00", "Gameplay")]).model_dump_json())
                vod = Vod(url=f"https://kick.com/x/videos/{name}", workspace=name, title=name, show_id=show.id)
                s.add(vod)
                s.commit()
                ids[name] = vod.id
        return cli, asked, ids

    def run(self, cli, *args):
        from typer.testing import CliRunner

        out = CliRunner().invoke(cli.app, ["breakdown", *args])
        assert out.exit_code == 0, out.output
        return out.output

    def test_by_default_only_streams_without_a_breakdown_are_done_and_saved_with_their_transcript(self, setup, settings):
        cli, asked, _ = setup
        self.run(cli)
        assert asked == ["kick-1"]
        saved = read_json(settings.transcripts_dir / "night-owls" / "2026-10-30_kick-1.json")
        assert saved["breakdown"] == [{"start": 0, "end": 600, "category": "News", "note": ""}]
        self.run(cli)
        assert asked == ["kick-1"]  # nothing left without one

    def test_again_redoes_the_ones_with_one_and_ids_pick_streams_saying_why_one_cant_be_done(self, setup):
        cli, asked, ids = setup
        self.run(cli, "--again")
        assert asked == ["kick-2", "kick-1"]  # newest first
        output = self.run(cli, str(ids["kick-2"]), str(ids["kick-3"]))
        assert asked[2:] == ["kick-2"] and "Its show has no breakdown categories" in output

    def test_imported_breaks_down_only_transcripts_from_elsewhere_that_have_none(self, setup, settings, monkeypatch):
        cli, asked, _ = setup
        folder = settings.transcripts_dir / "night-owls"
        write_text(folder / "2026-10-30_kick-1.json", json.dumps({"id": "kick-1", "duration": 600, "segments": []}))
        write_text(folder / "2025-01-02_archive-9.json", json.dumps({
            "id": "archive-9", "title": "An old stream", "date": "2025-01-02", "duration": 600, "platform": "Rumble",
            "segments": [{"start": 5, "end": 9, "text": "Hello chat."}],
        }))
        got = []

        def ask(settings_, meta, transcript, categories, context="", model=None, effort=None):
            got.append((meta["title"], meta["upload_date"], transcript.segments[0].text, context))
            return Breakdown(breakdown=[part("00:00:00", "00:10:00", "news")], other_summary="Nothing else.")

        monkeypatch.setattr(cli.segment, "ask_breakdown", ask)
        self.run(cli, "--imported")
        assert got == [("An old stream", "20250102", "Hello chat.", "")] and asked == []
        saved = read_json(folder / "2025-01-02_archive-9.json")
        assert saved["breakdown"] == [{"start": 0, "end": 600, "category": "News", "note": ""}]
        assert saved["other_summary"] == "Nothing else." and saved["categories"]["News"] == "going through headlines"
        assert "breakdown" not in read_json(folder / "2026-10-30_kick-1.json")  # the app's own: done the usual way
        self.run(cli, "--imported")
        assert len(got) == 1  # it has one now

    def test_limit_does_the_newest_few_with_the_model_and_effort_asked_for(self, setup):
        cli, asked, _ = setup
        self.run(cli, "--again", "--limit", "1", "--model", "sonnet", "--effort", "low")
        assert asked == ["kick-2"] and self.used == [("sonnet", "low")]

    def test_wait_rides_out_a_usage_limit_and_carries_on(self, setup, monkeypatch):
        from datetime import timedelta

        from clipper.db import utcnow
        from clipper.llm import UsageLimitError

        cli, asked, _ = setup
        real, calls, slept = cli.segment.break_down, [], []

        def limited_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise UsageLimitError("limit", resets_at=utcnow() + timedelta(hours=1))
            return real(*args, **kwargs)

        monkeypatch.setattr(cli.segment, "break_down", limited_once)
        monkeypatch.setattr(cli.time, "sleep", slept.append)
        output = self.run(cli, "--wait")
        assert asked == ["kick-1"] and len(calls) == 2  # the same stream, again after the wait
        assert 60 * 60 < slept[0] <= 70 * 60 and "Carrying on at" in output  # until it resets, and a head start

    def test_reserve_waits_while_the_plan_is_past_its_share_and_carries_on_after_the_reset(self, setup, monkeypatch):
        from datetime import timedelta

        from clipper import ai
        from clipper.db import utcnow

        cli, asked, _ = setup
        resets = (utcnow() + timedelta(hours=2)).isoformat()
        readings = [[{"kind": "five_hour", "percent": 85, "resets_at": resets}, {"kind": "seven_day", "percent": 40}],
                    [{"kind": "five_hour", "percent": 3, "resets_at": resets}, {"kind": "seven_day", "percent": 41}]]

        class Plan:
            def plan_usage(self):
                return readings.pop(0)

        slept = []
        monkeypatch.setattr(ai, "model", lambda *a, **k: Plan())
        monkeypatch.setattr(cli.time, "sleep", slept.append)
        output = self.run(cli, "--reserve", "20")
        assert asked == ["kick-1"] and len(slept) == 1 and 2 * 3600 < slept[0] <= 2 * 3600 + 600
        assert "Leaving the last 20% for the app (five_hour 85% used)" in output

    def test_reserve_stops_when_the_plans_usage_cant_be_read(self, setup, monkeypatch):
        from typer.testing import CliRunner

        from clipper import ai

        cli, asked, _ = setup

        class NoReport:
            def plan_usage(self):
                return None

        monkeypatch.setattr(ai, "model", lambda *a, **k: NoReport())
        out = CliRunner().invoke(cli.app, ["breakdown", "--reserve", "20"])
        assert out.exit_code == 1 and asked == [] and "can't be kept" in out.output
