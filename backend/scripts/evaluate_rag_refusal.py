#!/usr/bin/env python3
"""Evaluate RAG answer-adequacy refusal on synthetic fixtures (temporary store).

Parameters may be inspected on the development set; the holdout set is measured
once. Real LLM calls are counted and capped. Evidence is written with a new
filename; old evidence files are never overwritten.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from scripts.evaluate_rag import (  # noqa: E402
    dependency_versions,
    model_binding,
    object_hash,
    sha256_file,
    validate_fixture,
)


DATE_TAG = "20261006"
MAX_PAID_CALLS = 60  # overridden by --call-cap in main


def load_fixture(path: Path) -> dict:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    validate_fixture(fixture)
    return fixture


def ordinary_hits(context: dict) -> list[dict]:
    return [source for source in context.get("sources", []) if not source.get("is_constraint")]


def rejected_hits(context: dict) -> list[dict]:
    return list(context.get("rejected_sources") or [])


def evaluate_case(case: dict, context: dict, error: dict | None, reverse_documents: dict) -> dict:
    checks = []

    def check(rule: str, passed: bool, detail: str) -> None:
        checks.append({"rule": rule, "passed": bool(passed), "detail": detail})

    if error:
        check("DATA01", False, f"unexpected error {error}")
        return {"checks": checks, "passed": False, "error": error}

    hits = ordinary_hits(context)
    rejected = rejected_hits(context)
    labels = [reverse_documents.get(source.get("document_id"), "UNKNOWN") for source in hits]
    expected = set(case.get("expected_documents") or [])
    recall = (len(expected.intersection(labels)) / len(expected)) if expected else None
    adequacy = context.get("adequacy") or {}
    status = context.get("status")

    if case.get("expect_empty_hits"):
        # Success if injectable ordinary hits are empty: either refused, or never retrieved.
        empty = not hits
        check("REF01", empty, "ordinary injectable hits must be empty for no-answer cases")
        ok_status = status in {"insufficient", "no_match"} or (status == "matched" and empty and not rejected)
        check("REF02", ok_status, f"status={status}")
        if status == "insufficient" or rejected:
            check("REF03", adequacy.get("sufficient") is False,
                  f"adequacy={adequacy.get('sufficient')} degraded={adequacy.get('degraded')}")
        check("REF04", empty, "no-answer must not inject ordinary sources")
    elif expected:
        check("RET01", recall == 1.0, f"recall={recall} labels={labels}")
        adequacy_engaged = bool(rejected) or status == "insufficient" or bool(adequacy)
        if not hits and not rejected:
            # Retrieval miss: not counted as adequacy false refusal.
            check("REF_SKIP", adequacy in (None, {}) or adequacy.get("skipped_llm") in (True, None),
                  "no ordinary hits; adequacy false-refusal N/A")
        else:
            check("REF05", status == "matched", f"answerable case status={status}")
            check("REF06", adequacy.get("sufficient") is True, f"false refusal reason={adequacy.get('reason')}")
            check("REF07", not rejected, "answerable case must not reject gold sources")
    else:
        check("DATA01", True, "non-scored structural case")

    return {
        "case_id": case["case_id"],
        "query": case["query"],
        "status": status,
        "hit_labels": labels,
        "rejected_labels": [reverse_documents.get(source.get("document_id"), "UNKNOWN") for source in rejected],
        "recall_at_5": recall,
        "adequacy": adequacy,
        "warnings": context.get("warnings") or [],
        "retrieval_mode": context.get("retrieval_mode"),
        "checks": checks,
        "passed": all(item["passed"] for item in checks),
        "latency_ms": adequacy.get("latency_ms"),
    }


def index_fixture(fixture: dict, store, retrieval):
    library_ids, document_ids, catalog = {}, {}, {}
    definitions = {library["id"]: library for library in fixture["libraries"]}
    for library in fixture["libraries"]:
        created = store.create_library(library["owner"], library["name"], "拒答评测合成资料")
        library_ids[library["id"]] = created["library_id"]
    for doc in fixture["documents"]:
        library = definitions[doc["library"]]
        saved = store.save_document(
            library["owner"], library_ids[doc["library"]], doc["id"] + ".txt",
            doc["text"].encode(), title=doc["title"], category=library["kind"],
        )
        document_ids[doc["id"]] = saved["document_id"]
        catalog[saved["document_id"]] = saved
    for library in fixture["libraries"]:
        store.save_document(
            library["owner"], library_ids[library["id"]], "constraint.txt",
            library["constraints"].encode(), title=library["name"] + "固定约束",
            category=library["kind"], is_constraint=True,
        )
    for update in fixture.get("updates") or []:
        doc = next(item for item in fixture["documents"] if item["id"] == update["id"])
        owner = definitions[doc["library"]]["owner"]
        saved = store.replace_document(
            owner, document_ids[update["id"]], update["id"] + "-v2.txt", update["text"].encode(), title=doc["title"],
        )
        catalog[saved["document_id"]] = saved
        document_ids[update["id"]] = saved["document_id"]
    for deletion in fixture.get("deletions") or []:
        doc = next(item for item in fixture["documents"] if item["id"] == deletion)
        owner = definitions[doc["library"]]["owner"]
        store.delete_document(owner, document_ids[deletion])
    reverse_documents = {document_ids[label]: label for label in document_ids}
    return library_ids, document_ids, catalog, reverse_documents


def run_suite(name: str, fixture: dict, *, hybrid: bool, case_ids: list[str] | None, call_counter: dict) -> dict:
    from app.core import config
    from app.core.errors import AppError
    from app.services import knowledge_store as store, knowledge_retrieval as retrieval

    selected = [case for case in fixture["cases"] if case_ids is None or case["case_id"] in case_ids]
    # Skip empty-query / permission structural cases that never need adequacy for this experiment.
    selected = [
        case for case in selected
        if not case.get("expected_error") and not case.get("expect_permission_error")
    ]
    evidence = {
        "run_id": str(uuid4()),
        "suite": name,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "hybrid_enabled": hybrid,
        "adequacy_enabled": True,
        "fixture_eval_version": fixture["eval_version"],
        "cases": [],
        "paid_llm_calls": 0,
    }
    original_data = config.DATA_DIR
    with tempfile.TemporaryDirectory(prefix="dream-machine-rag-refusal-") as directory:
        config.DATA_DIR = Path(directory)
        config.settings.knowledge_adequacy_enabled = True
        config.settings.knowledge_hybrid_retrieval_enabled = hybrid
        config.settings.knowledge_adequacy_max_retries = 1
        retrieval.close()
        try:
            library_ids, document_ids, catalog, reverse_documents = index_fixture(fixture, store, retrieval)
            # Monkeypatch generate_json to count calls
            from app.models import llm_client as llm_mod
            original_generate_json = llm_mod.LLMClient.generate_json
            original_generate = llm_mod.LLMClient.generate

            def counted_generate(self, system, user, *, model=None, owner_id=None):
                call_counter["n"] += 1
                evidence["paid_llm_calls"] += 1
                if call_counter["n"] > MAX_PAID_CALLS:
                    raise AppError("EVAL_CALL_CAP", f"exceeded eval call cap {MAX_PAID_CALLS}", 429)
                return original_generate(self, system, user, model=model, owner_id=owner_id)

            llm_mod.LLMClient.generate = counted_generate
            try:
                for case in selected:
                    owner = case["owner"]
                    libs = [library_ids[item] for item in case["libraries"]]
                    started = time.perf_counter()
                    error = None
                    context = None
                    try:
                        # Only run adequacy-relevant retrieval; structural permission cases filtered out.
                        context = retrieval.build_context(owner, libs, case["query"], fixture["configuration"]["top_k"])
                    except AppError as exc:
                        error = {"code": exc.code, "message": str(exc.message if hasattr(exc, "message") else exc),
                                 "http_status": getattr(exc, "status_code", None)}
                    elapsed = (time.perf_counter() - started) * 1000
                    result = evaluate_case(case, context or {}, error, reverse_documents)
                    result["wall_latency_ms"] = elapsed
                    evidence["cases"].append(result)
                    print(f"[{name}] {case['case_id']} passed={result['passed']} status={result.get('status')} "
                          f"hits={result.get('hit_labels')} rejected={result.get('rejected_labels')} "
                          f"adequacy={((result.get('adequacy') or {}).get('sufficient'))}", flush=True)
            finally:
                llm_mod.LLMClient.generate = original_generate
                llm_mod.LLMClient.generate_json = original_generate_json
        finally:
            retrieval.close()
            config.DATA_DIR = original_data
    # Summaries
    answerable = [row for row in evidence["cases"] if row.get("recall_at_5") is not None]
    noanswer = [row for row in evidence["cases"] if any(case["case_id"] == row["case_id"] and case.get("expect_empty_hits")
                                                       for case in selected)]
    # Adequacy false refusal: answerable cases where injectable ordinary hits were refused.
    adequacy_answerable = []
    false_refusals = []
    retrieval_miss_answerable = []
    for row in answerable:
        adeq = row.get("adequacy") or {}
        engaged = row.get("status") == "insufficient" or bool(row.get("rejected_labels")) or bool(adeq)
        had_hits = bool(row.get("hit_labels")) or bool(row.get("rejected_labels"))
        if not had_hits and row.get("status") != "insufficient":
            retrieval_miss_answerable.append(row)
            continue
        if engaged or had_hits:
            adequacy_answerable.append(row)
            if row.get("status") == "insufficient" or adeq.get("sufficient") is False:
                false_refusals.append(row)
    correct_refusals = [row for row in noanswer if row["passed"]]
    latencies = [row.get("latency_ms") for row in evidence["cases"]
                 if isinstance(row.get("latency_ms"), (int, float)) and (row.get("latency_ms") or 0) > 0
                 and not (row.get("adequacy") or {}).get("skipped_llm")]
    evidence["summary"] = {
        "case_count": len(evidence["cases"]),
        "passed_count": sum(1 for row in evidence["cases"] if row["passed"]),
        "failed_case_ids": [row["case_id"] for row in evidence["cases"] if not row["passed"]],
        "answerable_count": len(answerable),
        "adequacy_answerable_count": len(adequacy_answerable),
        "retrieval_miss_answerable_ids": [row["case_id"] for row in retrieval_miss_answerable],
        "false_refusal_count": len(false_refusals),
        "false_refusal_rate": (len(false_refusals) / len(adequacy_answerable)) if adequacy_answerable else 0.0,
        "false_refusal_ids": [row["case_id"] for row in false_refusals],
        "noanswer_count": len(noanswer),
        "correct_refusal_count": len(correct_refusals),
        "correct_refusal_rate": (len(correct_refusals) / len(noanswer)) if noanswer else None,
        "flag_H07": next((row for row in evidence["cases"] if row["case_id"] == "H07"), None),
        "flag_H08": next((row for row in evidence["cases"] if row["case_id"] == "H08"), None),
        "flag_R12": next((row for row in evidence["cases"] if row["case_id"] == "R12"), None),
        "adequacy_latency_ms": {
            "p50": statistics.median(latencies) if latencies else None,
            "p95": sorted(latencies)[max(0, int(0.95 * len(latencies) + 0.999) - 1)] if latencies else None,
            "mean": statistics.mean(latencies) if latencies else None,
            "count": len(latencies),
        },
        "paid_llm_calls": evidence["paid_llm_calls"],
    }
    evidence["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return evidence


def write_evidence(evidence: dict, filename: str) -> Path:
    out = PROJECT / "docs" / "evidence" / "RAG" / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise FileExistsError(out)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default=str(PROJECT / "data" / "knowledge" / "models"))
    parser.add_argument("--skip-holdout", action="store_true")
    parser.add_argument("--tag", default="refusal-round2", help="evidence filename tag prefix")
    parser.add_argument("--call-cap", type=int, default=60)
    parser.add_argument("--hybrid-probe", action="store_true", help="also evaluate hybrid+refusal after baseline")
    args = parser.parse_args()
    global MAX_PAID_CALLS
    MAX_PAID_CALLS = args.call_cap
    tag = args.tag

    cache = Path(args.cache)
    os.environ["KNOWLEDGE_MODEL_CACHE"] = str(cache)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

    from app.core import config
    tag = getattr(args, "tag", "refusal-round2")
    if not config.settings.aihubmix_api_key:
        print("NO_API_KEY: code/tests ready; real refusal eval marked pending")
        pending = {
            "run_id": str(uuid4()),
            "status": "PENDING_NO_API_KEY",
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "note": "AIHUBMIX_API_KEY missing; refusal code landed, real eval not executed",
        }
        write_evidence(pending, f"rag-eval-{tag}-PENDING-no-key-{DATE_TAG}.json")
        return 2

    fixture_dev = load_fixture(BACKEND / "tests" / "fixtures" / "rag_eval.json")
    fixture_holdout = load_fixture(BACKEND / "tests" / "fixtures" / "rag_eval_holdout.json")
    call_counter = {"n": 0}
    binding = model_binding(cache)

    # Development set: answerable + R12 once (no empties/permission). Tune only here if needed.
    dev_ids = [case["case_id"] for case in fixture_dev["cases"]
               if not case.get("expected_error") and not case.get("expect_permission_error")]
    print("=== DEV baseline+refusal ===", flush=True)
    dev = run_suite("dev-baseline-refusal", fixture_dev, hybrid=False, case_ids=dev_ids, call_counter=call_counter)
    dev["model_binding"] = binding
    dev["dependencies"] = dependency_versions()
    write_evidence(dev, f"rag-eval-{tag}-baseline-dev-{DATE_TAG}.json")
    print("DEV summary", json.dumps(dev["summary"], ensure_ascii=False), flush=True)

    holdout = None
    if not args.skip_holdout:
        print("=== HOLDOUT baseline+refusal (once) ===", flush=True)
        hold_ids = [case["case_id"] for case in fixture_holdout["cases"]]
        holdout = run_suite("holdout-baseline-refusal", fixture_holdout, hybrid=False, case_ids=hold_ids, call_counter=call_counter)
        holdout["model_binding_sha"] = object_hash(binding)
        holdout["holdout_note"] = "Measured once; no retuning against holdout."
        write_evidence(holdout, f"rag-eval-{tag}-baseline-holdout-{DATE_TAG}.json")
        print("HOLDOUT summary", json.dumps(holdout["summary"], ensure_ascii=False), flush=True)

    hybrid = None
    # Probe hybrid only if refusal covers R12 on baseline (or R12 had no hits already) AND false refusal is low.
    r12 = next((row for row in dev["cases"] if row["case_id"] == "R12"), None)
    false_rate = dev["summary"]["false_refusal_rate"]
    # Round-2 default: only probe hybrid when explicitly requested (saves LLM budget).
    can_probe_hybrid = bool(args.hybrid_probe)
    if can_probe_hybrid:
        print("=== DEV hybrid+refusal probe ===", flush=True)
        hybrid_dev = run_suite("dev-hybrid-refusal", fixture_dev, hybrid=True, case_ids=dev_ids, call_counter=call_counter)
        write_evidence(hybrid_dev, f"rag-eval-{tag}-hybrid-dev-{DATE_TAG}.json")
        print("HYBRID DEV", json.dumps(hybrid_dev["summary"], ensure_ascii=False), flush=True)
        r12h = next((row for row in hybrid_dev["cases"] if row["case_id"] == "R12"), None)
        hybrid_ok = bool(r12h and r12h["passed"] and (hybrid_dev["summary"]["false_refusal_rate"] or 0) <= 0.2)
        hybrid = {"dev": hybrid_dev["summary"], "r12_ok": hybrid_ok}
        if hybrid_ok and not args.skip_holdout:
            print("=== HOLDOUT hybrid+refusal (once) ===", flush=True)
            hybrid_hold = run_suite(
                "holdout-hybrid-refusal", fixture_holdout, hybrid=True,
                case_ids=[case["case_id"] for case in fixture_holdout["cases"]], call_counter=call_counter,
            )
            write_evidence(hybrid_hold, f"rag-eval-{tag}-hybrid-holdout-{DATE_TAG}.json")
            print("HYBRID HOLDOUT", json.dumps(hybrid_hold["summary"], ensure_ascii=False), flush=True)
            hybrid["holdout"] = hybrid_hold["summary"]
            # Enable hybrid in example env only if holdout H07/H08 pass and H01 recall recovered.
            h01 = next((row for row in hybrid_hold["cases"] if row["case_id"] == "H01"), None)
            h07 = next((row for row in hybrid_hold["cases"] if row["case_id"] == "H07"), None)
            h08 = next((row for row in hybrid_hold["cases"] if row["case_id"] == "H08"), None)
            hybrid["enable_recommendation"] = bool(
                h01 and h01["passed"] and h07 and h07["passed"] and h08 and h08["passed"]
            )
        else:
            hybrid["enable_recommendation"] = False
            hybrid["skip_reason"] = "hybrid did not keep R12 refused with low false-refusal on DEV"
    else:
        hybrid = {"enable_recommendation": False, "skip_reason": "baseline refusal gate not met or probe disabled"}

    master = {
        "run_id": str(uuid4()),
        "started_at_utc": dev["started_at_utc"],
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "paid_llm_calls_total": call_counter["n"],
        "call_cap": MAX_PAID_CALLS,
        "baseline": {"dev": dev["summary"], "holdout": None if holdout is None else holdout["summary"]},
        "hybrid": hybrid,
    }
    write_evidence(master, f"rag-eval-{tag}-master-summary-{DATE_TAG}.json")
    print("MASTER", json.dumps(master, ensure_ascii=False, indent=2), flush=True)
    print(f"TOTAL_PAID_CALLS={call_counter['n']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
