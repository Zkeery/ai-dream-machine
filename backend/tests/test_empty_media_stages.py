"""A placeholder is neither a generated asset nor a completed stage."""
from copy import deepcopy

import pytest

from app.core.errors import AppError
from app.schemas.session import SessionCreate
from app.services import session_store


async def noop(*args):
    pass


@pytest.mark.asyncio
async def test_character_regeneration_before_storyboard_does_not_skip_media(orch):
    meta = orch.create(SessionCreate(idea="天台上的金黄"))
    for stage in ["script_generation", "character_design", "character_design"]:
        meta = await orch.execute_stage(meta.session_id, stage, noop)
    for stage in ["reference_generation", "video_generation"]:
        assert stage not in meta.stages_completed
        assert stage in meta.stale_stages
    # Starting storyboard also seeds legacy versions; placeholders must not enter it.
    meta = await orch.execute_stage(meta.session_id, "storyboard", noop)
    assert not meta.artifact_versions.get("reference_generation")
    assert not meta.artifact_versions.get("video_generation")
    meta = orch.continue_session(meta.session_id)
    assert meta.current_stage == "reference_generation"
    meta = await orch.execute_stage(meta.session_id, "reference_generation", noop)
    assert meta.artifacts["reference_generation"]["shots"]
    assert orch.continue_session(meta.session_id).current_stage == "video_generation"
    meta = await orch.execute_stage(meta.session_id, "video_generation", noop)
    assert meta.artifacts["video_generation"]["segments"]
    assert orch.continue_session(meta.session_id).current_stage == "post_production"


@pytest.mark.asyncio
async def test_legacy_empty_completion_repairs_to_reference_without_changing_upstream(orch):
    meta = orch.create(SessionCreate(idea="天台上的金黄"))
    for stage in ["script_generation", "character_design", "storyboard"]:
        meta = await orch.execute_stage(meta.session_id, stage, noop)
    upstream = deepcopy(meta.artifacts)
    versions = deepcopy(meta.selected_versions)
    for stage in ["reference_generation", "video_generation"]:
        meta.artifacts[stage] = {"stale_items": []}
        meta.stages_completed.append(stage)
        if stage in meta.stale_stages:
            meta.stale_stages.remove(stage)
        meta.artifact_versions[stage] = [{"version_id": stage, "reason": "legacy", "artifact": {"stale_items": []}}]
        meta.selected_versions[stage] = stage
    meta.current_stage, meta.status = "post_production", "idle"
    session_store.touch(meta)
    repaired = orch.get(meta.session_id)
    assert repaired.current_stage == "reference_generation"
    assert repaired.status == "idle"
    assert repaired.selected_versions == versions
    for stage in ["script_generation", "character_design", "storyboard"]:
        assert repaired.artifacts[stage] == upstream[stage]
    for stage in ["reference_generation", "video_generation"]:
        assert stage not in repaired.stages_completed
        assert stage not in repaired.artifact_versions
        assert stage not in repaired.artifacts
    assert orch.get(meta.session_id).model_dump() == repaired.model_dump()


def test_composition_rejects_metadata_only_video_even_when_marked_complete(orch):
    meta = orch.create(SessionCreate(idea="x"))
    meta.artifacts["video_generation"] = {"stale_items": []}
    meta.stages_completed.append("video_generation")
    with pytest.raises(AppError) as exc:
        orch._require_inputs(meta, "post_production")
    assert exc.value.code == "MISSING_STAGE_INPUT"
