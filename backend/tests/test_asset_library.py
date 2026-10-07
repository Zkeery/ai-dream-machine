"""生成素材的归属、复用和历史兼容；全部临时数据，不调用模型。"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.core import config
from app.main import app
from app.schemas.session import SessionMeta
from app.schemas.task import TaskMeta
from app.services import asset_library, auth, session_store, task_store


def image_for(source_id: str, name: str = "role.png"):
    path = config.IMAGE_DIR / source_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"owned-generated-image")
    return path


def story(owner: str, sid: str, image):
    return session_store.create_session(SessionMeta(
        session_id=sid, owner_id=owner, idea="测试故事",
        artifacts={"character_design": {"characters": [{"id": "c1", "name": "猫",
                                                       "selected": str(image), "versions": [str(image)]}]}},
    ))


def test_assets_merge_story_and_quick_without_duplicates(data_dirs):
    image = image_for("story")
    story("one", "story", image)
    quick_image = image_for("quick", "1.png")
    task_store.create_task(TaskMeta(task_id="quick", type="literary_video", owner_id="one",
                                   status="completed", input={"text": "短片文案"},
                                   result={"images": [str(quick_image)]}))
    items = asset_library.owned_assets("one")
    assert len(items) == 2
    assert {item["source_type"] for item in items} == {"session", "task"}
    assert all("_path" not in asset_library.public_asset(item) for item in items)


def test_assets_reject_other_accounts_and_forged_paths(data_dirs):
    other = image_for("other")
    story("two", "other", other)
    story("one", "story", other)
    assert asset_library.owned_assets("one") == []
    entry = asset_library.owned_assets("two")[0]
    with pytest.raises(Exception) as error:
        asset_library.require_asset(entry["asset_id"], "one")
    assert error.value.code == "ASSET_NOT_FOUND"


def test_assets_skip_symlinks_outside_source(data_dirs):
    foreign = image_for("foreign")
    link = config.IMAGE_DIR / "story" / "linked.png"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(foreign)
    story("one", "story", link)
    assert asset_library.owned_assets("one") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("project_type", ["story", "comic"])
async def test_asset_reuse_creates_owned_input_and_blocks_other_owner(data_dirs, project_type):
    source = image_for("story")
    if project_type == "comic":
        session_store.create_session(SessionMeta(
            session_id="story", owner_id="one", idea="漫画素材复用", project_type="comic",
            artifacts={"comic_panels": {"shots": [{"shot_id": "s1", "path": str(source),
                                                   "selected": str(source), "versions": [str(source)]}]}},
        ))
    else:
        story("one", "story", source)
    app.dependency_overrides[get_current_user] = lambda: "one"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/assets")
            assert response.status_code == 200
            asset = response.json()[0]
            assert (await client.get(asset["url"])).content == source.read_bytes()
            reused = await client.post("/api/assets/reuse", json={"asset_id": asset["asset_id"]})
            assert reused.status_code == 200
            payload = reused.json()
            assert auth.require_upload_owner(payload["filename"], "one").read_bytes() == source.read_bytes()
            assert payload["asset"]["source_id"] == "story"
            assert source.exists()
            app.dependency_overrides[get_current_user] = lambda: "two"
            assert (await client.get(asset["url"])).status_code == 404
            assert (await client.post("/api/assets/reuse", json={"asset_id": asset["asset_id"]})).status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_assets_retain_history_and_stale_flag(data_dirs):
    old = image_for("story", "old.png")
    current = image_for("story", "current.png")
    meta = story("one", "story", current)
    meta.stale_stages = ["character_design"]
    meta.selected_versions = {"character_design": "v2"}
    meta.artifact_versions = {"character_design": [{"version_id": "v1", "created_at": 1,
                                                   "artifact": {"selected": str(old)}}]}
    session_store.touch(meta)
    items = asset_library.owned_assets("one")
    assert {item["name"] for item in items} == {"old.png", "current.png"}
    assert all(item["stale"] for item in items)
