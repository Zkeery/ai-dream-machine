"""Read-only exact-version linking; prints a manifest for the release record."""
from pathlib import Path
import base64
import gzip
import hashlib
import importlib.util
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.services.review_version_links import link_review
from app.services.answer_grounding_v13 import check_answer_grounding


def load(path):
    data = json.loads(path.read_text())
    return json.loads(gzip.decompress(base64.b64decode(data["full_result_gzip_base64"]))) if "full_result_gzip_base64" in data else data


def verify():
    evidence = ROOT / "docs/evidence/local-free-acceptance-20261025"
    review_path = evidence / "rag-human-adjudication-20261025/新增人工复核记录.json"
    old_path = evidence / "rag-causal-scope-v6/result.json"
    current_path = evidence / "rag-implicit-relations-v13-20261026/replay-run-01.json"
    expected = {review_path: "6bd9b8da73d25da5340af4dd1139386363b117f01219c748755bdfc9dbdae60b",
                old_path: "9bbd5eadd394e2975998910233901f9f83d72697528407e314b9b09952abda5e",
                current_path: "1239688791bfe988c4b2de5edc88b6ee0999a325fe682b23d3d0c3c9d8886698"}
    for path, value in expected.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == value, "trusted snapshot changed"
    review, old, current = load(review_path), load(old_path), load(current_path)
    assert review["status"] == "completed" and review["active_question_id"] is None
    spec = importlib.util.spec_from_file_location("replay", ROOT / "backend/scripts/replay_answer_grounding_offline.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    base = ROOT / "docs/evidence/第二批收尾-20261011/B1"
    items = replay.validate_items(load(base / "frozen-52-payloads.json"))
    links = []
    for case in review["cases"]:
        item = next(x for x in items if x["item"] == case["item"])
        binding = case["binding"]
        for field, hash_field in (("payload_file", "payload_file_sha256"), ("response_file", "response_sha256")):
            assert hashlib.sha256((ROOT / binding[field]).read_bytes()).hexdigest() == binding[hash_field]
        sources, _ = replay.parse_sources(load(ROOT / binding["payload_file"]), item["group"], item.get("retrieval_binding", {}), item["query"])
        row = next(x for x in current["records"] if x["item"] == case["item"])
        prior = next(x for x in old["records"] if x["item"] == case["item"])
        linked = link_review(row, prior, case, sources)
        assert linked["status"] == "HUMAN_ACCEPTED_THIS_VERSION", linked
        links.append(linked)
    h05 = next(x for x in current["records"] if x["item"] == "H05-with_rag")
    assert h05["assistant_check"]["status"] == "NEED_REVIEW"
    counts = {}
    for item in items:
        row = next(x for x in current["records"] if x["item"] == item["item"])
        payload = load(base / "payloads" / (item["item"] + ".json"))
        sources, _ = replay.parse_sources(payload, item["group"], item.get("retrieval_binding", {}), item["query"])
        fresh = check_answer_grounding(row["answer"], sources, row["citations"])
        assert {k: fresh[k] for k in row["assistant_check"]} == row["assistant_check"], "checker output changed: " + item["item"]
        counts[fresh["status"]] = counts.get(fresh["status"], 0) + 1
    return dict(schema="release-review-links-20261026", status="PASS", linked_count=len(links),
                snapshots={str(p.relative_to(ROOT)): sha for p, sha in expected.items()},
                h05_machine_status="NEED_REVIEW", new_scores=0, old_records_modified=False,
                checker_replay=dict(total=len(items), identical=52, statuses=counts), links=links)


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
