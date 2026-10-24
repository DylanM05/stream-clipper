from clipper.workspace import Workspace, safe_id, write_json


def test_safe_id():
    assert safe_id("TwitchVod-v2123456789") == "twitchvod-v2123456789"
    assert safe_id("../../etc/passwd") == "etc-passwd"


def test_clear_from_removes_that_step_and_everything_after(tmp_path):
    ws = Workspace.open(tmp_path, "vod")
    for path in (ws.audio_wav, ws.transcript_json, ws.signals_json, ws.segmentation_json, ws.segments_json):
        write_json(path, {})
    (ws.clips_dir / "ch01.mp4").parent.mkdir()
    (ws.clips_dir / "ch01.mp4").write_bytes(b"")

    ws.clear_from("segment")

    assert ws.audio_wav.exists() and ws.transcript_json.exists() and ws.signals_json.exists()
    assert not ws.segmentation_json.exists()
    assert not ws.segments_json.exists()
    assert not ws.clips_dir.exists()
