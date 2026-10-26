"""Exact migration of existing human judgments to a newer checker record.

Only the machine annotation may differ. No scoring or semantic rule is added.
The caller must authenticate the original review and baseline snapshots.
"""
from copy import deepcopy
import hashlib
import json

TARGETS = {"R07-with_rag", "H01-with_rag", "R10-with_rag"}


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def link_review(current, original, case, sources):
    outcome = dict(status="NEED_REVIEW", rescored=False, machine_check_unchanged=True,
                   original_scores_changed=False, reason="version_or_context_mismatch")
    if current.get("item") not in TARGETS or current.get("item") != case.get("item"):
        return outcome
    excluded = {"assistant_check", "human_comparison"}
    if sha({k: v for k, v in current.items() if k not in excluded}) != sha({k: v for k, v in original.items() if k not in excluded}):
        return outcome
    binding = case["binding"]
    if (any(sha(current.get(k)) != sha(v) for k, v in binding.items())
            or current["query"] != case["query"] or current["answer"] != case["answer"]
            or current["citations"] != case["citations"]
            or hashlib.sha256(current["answer"].encode()).hexdigest() != binding["answer_sha256"]
            or sha(sources) != binding["provided_sources_sha256"]):
        return outcome
    descriptors = [{k: v for k, v in source.items() if k != "text"}
                   | {"text_sha256": hashlib.sha256(source["text"].encode()).hexdigest()} for source in sources]
    if sha(descriptors) != sha(current["sources"]):
        return outcome
    judgments = case.get("new_judgments", [])
    expected = {"source_support": "可合理推断" if current["item"] == "R10-with_rag" else "原文明示",
                "rewrite_acceptance": "接受", "overall_acceptability": "可接受"}
    if len(judgments) != 3 or {j["field"]: j["answer"] for j in judgments} != expected:
        return outcome
    outcome.update(status="HUMAN_ACCEPTED_THIS_VERSION", reason="same_original_inputs_new_checker_annotation_only",
                   item=current["item"], query=current["query"], answer=current["answer"],
                   binding=deepcopy(binding), source_versions=deepcopy(descriptors), judgments=deepcopy(judgments),
                   current_checker_version=current["assistant_check"]["version"],
                   current_record_sha256=sha(current),
                   scope="仅限该问题、回答、完整资料和完成版本；不迁移至新的生成内容，也不改变机器结论。"
                       + ("出租汽车判断仅限本次交通画面创作请求，不是普遍世界存在性规则。" if current["item"] == "R10-with_rag" else ""))
    return outcome
