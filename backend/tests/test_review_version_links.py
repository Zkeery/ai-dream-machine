from copy import deepcopy
import hashlib

import pytest

from app.services.review_version_links import link_review, sha


def fixture():
    sources = [{"citation_id": "K1", "document_id": "doc", "version_id": "v1", "version_number": 1, "text": "交通画面只允许步行。"}]
    binding = dict(answer_sha256=hashlib.sha256("不能写出租汽车镜头。".encode()).hexdigest(),
                   provided_sources_sha256=sha(sources), completion_version={"id": "saved", "created": 123})
    current = dict(item="R10-with_rag", query="写出租汽车动作戏", answer="不能写出租汽车镜头。", citations=["K1"],
                   sources=[{k: v for k, v in sources[0].items() if k != "text"}
                            | {"text_sha256": hashlib.sha256(sources[0]["text"].encode()).hexdigest()}],
                   assistant_check={"version": "v13", "status": "NEED_REVIEW"}, **binding)
    old = deepcopy(current)
    old["assistant_check"]["version"] = "v6"
    case = {k: deepcopy(current[k]) for k in ("item", "query", "answer", "citations")}
    case.update(binding=binding, new_judgments=[dict(field=k, answer=v) for k, v in
        (("source_support", "可合理推断"), ("rewrite_acceptance", "接受"), ("overall_acceptability", "可接受"))])
    return current, old, case, sources


def test_only_machine_annotation_can_change():
    args = fixture()
    before = deepcopy(args)
    assert link_review(*args)["status"] == "HUMAN_ACCEPTED_THIS_VERSION"
    assert args == before


@pytest.mark.parametrize("field,value", [("query", "世界存在出租汽车吗"), ("answer", "世界没有汽车"),
    ("citations", []), ("completion_version", {"id": "new", "created": 123.0}), ("sources", [])])
def test_different_context_or_version_stays_review(field, value):
    current, old, case, sources = fixture()
    current[field] = value
    assert link_review(current, old, case, sources)["status"] == "NEED_REVIEW"


def test_actual_source_changes_and_missing_judgment_stay_review():
    current, old, case, sources = fixture()
    sources[0]["text"] = "修改后的资料"
    assert link_review(current, old, case, sources)["status"] == "NEED_REVIEW"
    current, old, case, sources = fixture()
    case["new_judgments"].pop()
    assert link_review(current, old, case, sources)["status"] == "NEED_REVIEW"
