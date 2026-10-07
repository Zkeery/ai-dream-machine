"""Seedance/Veo contracts and preflight: no external model calls."""
from copy import deepcopy

import httpx
from PIL import Image
import pytest

from app.core import config
from app.core.errors import AppError
from app.models.video_client import VideoClient
from app.models.video_contracts import SEEDANCE, VEO
from app.schemas.models import ModelSelection
from app.schemas.session import SessionCreate
from app.services import cost_control, model_catalog, session_store


@pytest.mark.parametrize("model,seconds", [(SEEDANCE, 5), (VEO, 8)])
@pytest.mark.parametrize("mode", ["first_frame", "start_end", "reference"])
def test_native_payload_poll_download_and_budget_use_selected_model(monkeypatch, tmp_path, model, seconds, mode):
    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (160, 90), (index, 30, 60)).save(path)
        paths.append(str(path))
    requests, reservations = [], []

    def request(method, url, **kwargs):
        requests.append((method, url, kwargs.get("json")))
        if method == "POST":
            return httpx.Response(200, json={"id": "native-video", "status": "pending"})
        return httpx.Response(200, json={"status": "completed", "output": [{"content_url": "https://media.test/video.mp4"}]})

    monkeypatch.setattr(config.settings, "aihubmix_api_key", "offline-test")
    monkeypatch.setattr(httpx, "request", request)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, content=b"saved-video"))
    monkeypatch.setattr(cost_control, "reserve", lambda call, model, kind, units: reservations.append((model, units)))
    monkeypatch.setattr(cost_control, "settle", lambda *a, **k: None)
    client = VideoClient()
    client.poll_interval = 0
    out = client.image_to_video(paths[0], "one uncut take", tmp_path / "out.mp4", mode,
                                reference_paths=[paths[0], paths[1]], end_image_path=paths[2] if mode == "start_end" else None,
                                video_ratio="16:9", resolution="1080P", model=model)
    assert out.read_bytes() == b"saved-video"
    assert [r[0] for r in requests] == ["POST", "GET"]
    assert requests[0][1].endswith("/ai/v1/videos")
    assert requests[1][1].endswith("/ai/v1/videos/native-video")
    payload = requests[0][2]
    assert payload["model"] == model and payload["duration"] == seconds
    assert reservations == [(model, {"seconds": float(seconds), "resolution": "1080P"})]
    assert payload["resolution"] == "1080p" and "extra" not in payload
    assert ("generate_audio" in payload) == (model == SEEDANCE)
    if mode == "reference":
        assert len(payload["input_references"]) == 2  # deduplicated, no silent truncation
        assert all(r["url"].startswith("data:image/png;base64,") for r in payload["input_references"])
        assert "frame_images" not in payload and payload["aspect_ratio"] == "16:9"
    else:
        assert "input_references" not in payload
        assert [f["frame_type"] for f in payload["frame_images"]] == (["first_frame", "last_frame"] if mode == "start_end" else ["first_frame"])
        assert payload["aspect_ratio"] == ("adaptive" if model == SEEDANCE else "16:9")


@pytest.mark.parametrize("ratio,refs,error", [("1:1", [], "VIDEO_FORMAT_UNSUPPORTED"),
                                                ("16:9", ["b", "c", "d"], "VIDEO_REFERENCE_LIMIT")])
def test_veo_invalid_request_fails_before_reading_media_or_http(monkeypatch, tmp_path, ratio, refs, error):
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "offline-test")
    monkeypatch.setattr(httpx, "request", lambda *a, **k: pytest.fail("invalid inputs must never submit"))
    with pytest.raises(AppError) as exc:
        VideoClient().image_to_video("a", "one shot", tmp_path / "out.mp4", "reference", model=VEO,
                                      video_ratio=ratio, reference_paths=refs)
    assert exc.value.code == error


def test_catalog_exposes_durations_and_read_only_cost_estimates():
    options = {o["id"]: o for o in model_catalog.public_catalog()["groups"]["video_reference"]["options"]}
    assert options[SEEDANCE]["label"] == "Seedance 2.5"
    assert options[SEEDANCE]["video"]["estimated_clip_cny"] == {"720P": 9.4, "1080P": 21.12}
    assert options[VEO]["video"]["duration_seconds"] == 8
    assert options[VEO]["video"]["estimated_clip_cny"] == {"720P": 25.6, "1080P": 25.6}
    for key in ("video_first_frame", "video_start_end", "video_reference"):
        assert model_catalog.validate_selection({key: VEO}) == {key: VEO}


async def noop(*args):
    pass


@pytest.mark.asyncio
async def test_preflight_rejects_entire_batch_before_agent_or_first_clip(orch, monkeypatch):
    meta = orch.create(SessionCreate(idea="模型接入回归", episodes=1))
    for stage in ("script_generation", "character_design", "storyboard", "reference_generation"):
        meta = await orch.execute_stage(meta.session_id, stage, noop)
    # The first shot has 3 references, the second 4. Neither may be submitted.
    extra = deepcopy(meta.artifacts["character_design"]["characters"][0])
    extra.update(id="c2", selected=extra["selected"] + ".other")
    meta.artifacts["character_design"]["characters"].append(extra)
    meta.artifacts["storyboard"]["shots"][1]["character_ids"].append("c2")
    meta.model_selection = ModelSelection(video_reference=VEO)
    meta.video_generation_mode = "reference"
    session_store.touch(meta)
    monkeypatch.setattr(orch, "_run_stage_agents", lambda *a: pytest.fail("preflight must precede paid Agent decisions"))
    with pytest.raises(AppError) as exc:
        await orch.execute_stage(meta.session_id, "video_generation", noop)
    assert exc.value.code == "VIDEO_REFERENCE_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("model,seconds", [(SEEDANCE, 5), (VEO, 8)])
async def test_story_generation_records_real_selection_and_requested_duration(orch, monkeypatch, model, seconds):
    meta = orch.create(SessionCreate(idea="模型接入回归", model_selection={"video_first_frame": model}))
    for stage in ("script_generation", "character_design", "storyboard", "reference_generation"):
        meta = await orch.execute_stage(meta.session_id, stage, noop)
    prompts = []
    generate = orch.video.image_to_video
    def capture(image, prompt, *args, **kwargs):
        prompts.append((prompt, kwargs["model"]))
        return generate(image, prompt, *args, **kwargs)
    monkeypatch.setattr(orch.video, "image_to_video", capture)
    meta = await orch.execute_stage(meta.session_id, "video_generation", noop)
    assert all(f"片段时长：{seconds}秒" in p and m == model for p, m in prompts)
    for segment in meta.artifacts["video_generation"]["segments"]:
        assert segment["requested_duration_seconds"] == seconds
        assert segment["model_usage"]["models"]["video_first_frame"] == model
