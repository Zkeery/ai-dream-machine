#!/usr/bin/env python3
"""Real local embedding acceptance on a synthetic, temporary knowledge store.

No paid model, no installation, no real user collection, no surrogate embedding.
Results require an explicit evidence destination; existing evidence is preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dependency_versions() -> dict:
    names = ("fastembed", "qdrant-client", "onnxruntime", "numpy", "huggingface-hub",
             "tokenizers", "pydantic", "pypdf", "python-docx")
    result = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def model_binding(cache: Path) -> dict:
    files = []
    # Snapshot files identify actual weights/tokenizer and revision, not lock files.
    for path in sorted(cache.glob("models--*/snapshots/*/*")):
        if path.is_file():
            files.append({"path": str(path.relative_to(cache)), "bytes": path.stat().st_size,
                          "sha256": sha256_file(path)})
    if not any(x["path"].endswith(".onnx") for x in files):
        raise RuntimeError("A preloaded local ONNX model snapshot is required; runner does not download models")
    return {"cache": str(cache), "files": files,
            "snapshot_revisions": sorted({x["path"].split("/")[2] for x in files}),
            "file_manifest_sha256": object_hash(files)}


def validate_fixture(fixture: dict) -> None:
    documents = {d["id"]: d for d in fixture["documents"]}
    libraries = {library["id"]: library for library in fixture["libraries"]}
    cases = fixture["cases"]
    if len(documents) != len(fixture["documents"]) or len(libraries) != len(fixture["libraries"]):
        raise ValueError("duplicate document/library fixture IDs")
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("duplicate case IDs")
    for document in documents.values():
        if document["library"] not in libraries or not document["text"].strip():
            raise ValueError("invalid document fixture")
    for case in cases:
        if any(library not in libraries for library in case["libraries"]):
            raise ValueError(f"unknown library in {case['case_id']}")
        for key in ("expected_documents", "forbidden_documents", "expected_versions"):
            if any(doc not in documents for doc in case.get(key, [])):
                raise ValueError(f"unknown gold ID in {case['case_id']}")
        if not case.get("expect_permission_error"):
            if any(libraries[library]["owner"] != case["owner"] for library in case["libraries"]):
                raise ValueError("foreign library allowed without permission expectation")
        for document in case.get("expected_documents", []):
            if documents[document]["library"] not in case["libraries"]:
                raise ValueError("gold document lies outside selected libraries")
    config = fixture["configuration"]
    if config["top_k"] != 5 or config["repeats"] < 1 or config["retry_count"] != 0:
        raise ValueError("unsupported frozen evaluation settings")


def score_case(case: dict, context: dict, error: dict | None, catalog: dict,
               reverse_documents: dict, reverse_libraries: dict, library_definitions: dict, limit: int) -> dict:
    sources = context.get("sources", [])
    hits = [source for source in sources if not source.get("is_constraint")][:limit]
    constraints = [source for source in sources if source.get("is_constraint")]
    labels = [reverse_documents.get(source.get("document_id"), "UNKNOWN") for source in hits]
    expected = set(case.get("expected_documents", []))
    recall = len(expected.intersection(labels)) / len(expected) if expected else None
    checks = []

    def check(rule: str, passed: bool, detail: str) -> None:
        checks.append({"rule": rule, "passed": bool(passed), "detail": detail})

    if case.get("expect_permission_error"):
        check("SEC01", bool(error and error.get("http_status") in (403, 404) and not sources), "foreign library must reject with no sources")
    elif case.get("expected_error"):
        check("EMP01", bool(error and error.get("code") == case["expected_error"] and not sources), "empty query must explicitly reject with no hits")
    else:
        check("DATA01", error is None, "retrieval must finish without unexpected exception")
    allowed = {library for library in case["libraries"] if library_definitions[library]["owner"] == case["owner"]}
    check("SEC01", all(reverse_libraries.get(source.get("library_id")) in allowed for source in sources), "all sources must belong to selected authorized libraries")
    check("DATA01", all(all(field in source for field in ("document_id", "library_id", "version_id", "version_number", "chunk_id", "text", "score")) for source in sources), "source provenance fields are complete")
    check("VER01", all(source.get("document_id") in catalog and
                         source.get("version_id") == catalog[source["document_id"]]["version_id"] and
                         source.get("version_number") == catalog[source["document_id"]]["version_number"] for source in sources), "all source versions must be active at query time")
    check("SEC01", not set(case.get("forbidden_documents", [])).intersection(labels), "forbidden or deleted documents must not appear")
    if expected:
        check("RET01", recall == 1.0, "all labeled evidence documents retrieved in ordinary Top-5")
    if case.get("expect_empty_hits"):
        check("EMP01", not hits, "ordinary hit list must be empty; constraints are not answers")
    expected_constraints = case.get("expected_constraints", [] if case.get("expected_error") or case.get("expect_permission_error") else case["libraries"])
    found_constraints = {reverse_libraries.get(source.get("library_id")): source for source in constraints}
    covered = 0
    for library in expected_constraints:
        source = found_constraints.get(library, {})
        present = library_definitions[library]["constraints"] in source.get("text", "")
        covered += int(present)
        check("CON01", present, f"fixed constraint from {library} must be preserved verbatim")
    combined = "\n".join(source.get("text", "") for source in hits)
    for text in case.get("required_text", []):
        check("VER01", text in combined, "latest required fact: " + text)
    for text in case.get("forbidden_text", []):
        check("VER01", text not in combined, "superseded fact absent: " + text)
    for label, version in case.get("expected_versions", {}).items():
        matched = [hit for hit in hits if reverse_documents.get(hit.get("document_id")) == label]
        check("VER01", bool(matched) and all(hit["version_number"] == version for hit in matched), f"{label} must use version {version}")
    # RET01 losses contribute to the frozen macro-recall threshold. Other rules
    # and all checks on critical cases are non-compensable.
    return {"hits": hits, "constraints": constraints, "retrieved_document_labels": labels,
            "recall_at_5": recall, "constraints_expected": len(expected_constraints),
            "constraints_covered": covered, "checks": checks,
            "passed": all(check["passed"] for check in checks)}


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(quantile * len(values)) - 1)]


def run_evaluation(fixture: dict, selected: list[dict], evidence: dict, cache: Path) -> None:
    # Freeze network downloads before model initialization; cache is read-only.
    os.environ["KNOWLEDGE_MODEL_CACHE"] = str(cache)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from app.core import config
    from app.core.errors import AppError
    from app.services import knowledge_store as store, knowledge_retrieval as retrieval

    original_data_dir = config.DATA_DIR
    library_definitions = {library["id"]: library for library in fixture["libraries"]}
    definitions = {doc["id"]: doc for doc in fixture["documents"]}
    source_files = [Path(store.__file__), Path(retrieval.__file__), Path(__file__).resolve()]
    evidence["implementation"] = [{"path": str(path.relative_to(PROJECT)), "sha256": sha256_file(path)} for path in source_files]
    evidence["retrieval_configuration"] = {
        "embedding_model": retrieval.EMBEDDING_MODEL, "dimension": retrieval.VECTOR_SIZE,
        "score_threshold": retrieval.SCORE_THRESHOLD, "max_context_chars": retrieval.MAX_CONTEXT_CHARS,
        "top_k": fixture["configuration"]["top_k"], "collection": retrieval.COLLECTION,
        "chunk_probe": store.chunk_text("甲" * 900), "max_constraint_chars": store.MAX_CONSTRAINT_CHARS,
        "generation_model": None, "llm_judge": None, "seed": "unsupported; deterministic local embedding",
    }
    evidence["retrieval_configuration"]["chunk_probe"] = [
        {"start": chunk.get("start"), "length": len(chunk["text"])}
        for chunk in evidence["retrieval_configuration"]["chunk_probe"]]
    with tempfile.TemporaryDirectory(prefix="dream-machine-rag-eval-") as directory:
        config.DATA_DIR = Path(directory)
        evidence["isolated_data"] = {"temporary_directory": True, "real_user_data_written": False}
        try:
            start = time.perf_counter()
            retrieval._embedding_model()
            evidence["model_initialization_ms"] = (time.perf_counter() - start) * 1000
            library_ids, document_ids, catalog = {}, {}, {}
            start = time.perf_counter()
            for library in fixture["libraries"]:
                created = store.create_library(library["owner"], library["name"], "合成本地检索评测资料")
                library_ids[library["id"]] = created["library_id"]
            for doc in fixture["documents"]:
                library = library_definitions[doc["library"]]
                saved = store.save_document(library["owner"], library_ids[doc["library"]], doc["id"] + ".txt",
                                            doc["text"].encode(), title=doc["title"], category=library["kind"])
                document_ids[doc["id"]] = saved["document_id"]
                catalog[saved["document_id"]] = saved
            for library in fixture["libraries"]:
                saved = store.save_document(library["owner"], library_ids[library["id"]], "constraint.txt",
                                            library["constraints"].encode(), title=library["name"] + "固定约束",
                                            category=library["kind"], is_constraint=True)
                document_ids["constraint:" + library["id"]] = saved["document_id"]
                catalog[saved["document_id"]] = saved
            evidence["initial_index_build_ms"] = (time.perf_counter() - start) * 1000
            evidence["bindings"] = {"libraries": library_ids, "documents": document_ids}
            reverse_documents = {value: key for key, value in document_ids.items()}
            reverse_libraries = {value: key for key, value in library_ids.items()}
            evidence["cases"] = []
            for phase in ("initial", "updated"):
                if phase == "updated":
                    start = time.perf_counter()
                    for update in fixture["updates"]:
                        definition = definitions[update["id"]]
                        owner = library_definitions[definition["library"]]["owner"]
                        saved = store.replace_document(owner, document_ids[update["id"]], update["id"] + ".txt", update["text"].encode())
                        catalog[saved["document_id"]] = saved
                    for label in fixture["deletions"]:
                        owner = library_definitions[definitions[label]["library"]]["owner"]
                        store.delete_document(owner, document_ids[label])
                        catalog.pop(document_ids[label])
                    evidence["update_and_delete_ms"] = (time.perf_counter() - start) * 1000
                for case in selected:
                    if case.get("phase", "initial") != phase:
                        continue
                    for repetition in range(1, fixture["configuration"]["repeats"] + 1):
                        context, error = {}, None
                        start = time.perf_counter()
                        try:
                            context = retrieval.build_context(case["owner"], [library_ids[label] for label in case["libraries"]],
                                                              case["query"], limit=fixture["configuration"]["top_k"])
                        except AppError as exc:
                            error = {"type": type(exc).__name__, "code": exc.code, "http_status": exc.status_code, "message": exc.message}
                        except Exception as exc:
                            error = {"type": type(exc).__name__, "code": "UNEXPECTED_EXCEPTION", "message": str(exc)}
                        elapsed = (time.perf_counter() - start) * 1000
                        scored = score_case(case, context, error, catalog, reverse_documents, reverse_libraries,
                                            library_definitions, fixture["configuration"]["top_k"])
                        diagnostic = None
                        if evidence.get("score_diagnostics") and not error and case["query"].strip():
                            # Inspect scores below the configured cutoff without
                            # changing the primary run, labels, or acceptance gate.
                            threshold = retrieval.SCORE_THRESHOLD
                            try:
                                retrieval.SCORE_THRESHOLD = -1.0
                                raw = retrieval.build_context(case["owner"], [library_ids[label] for label in case["libraries"]],
                                                              case["query"], limit=fixture["configuration"]["top_k"])
                                diagnostic = {"diagnostic_only": True, "score_threshold": -1.0,
                                              "hits": [hit for hit in raw["sources"] if not hit["is_constraint"]]}
                            except Exception as exc:
                                diagnostic = {"diagnostic_only": True, "error": {"type": type(exc).__name__, "message": str(exc)}}
                            finally:
                                retrieval.SCORE_THRESHOLD = threshold
                        evidence["cases"].append({"case_id": case["case_id"], "category": case["category"], "phase": phase,
                                                  "repetition": repetition, "owner_id": case["owner"], "selected_libraries": case["libraries"],
                                                  "query": case["query"], "latency_ms": elapsed, "error": error,
                                                  "retrieval_status": context.get("status"), "warnings": context.get("warnings", []),
                                                  "library_versions": context.get("library_versions", {}),
                                                  "raw_score_diagnostic": diagnostic, **scored})
        finally:
            retrieval.close()
            config.DATA_DIR = original_data_dir


def summarize(fixture: dict, selected: list[dict], evidence: dict) -> None:
    rows = evidence.get("cases", [])
    expected_rows = len(selected) * fixture["configuration"]["repeats"]
    actual_keys = {(row["case_id"], row["repetition"]) for row in rows}
    expected_keys = {(case["case_id"], repeat) for case in selected for repeat in range(1, fixture["configuration"]["repeats"] + 1)}
    data_complete = len(rows) == expected_rows and actual_keys == expected_keys
    recalls = [row["recall_at_5"] for row in rows if row["recall_at_5"] is not None]
    expected_constraints = sum(row["constraints_expected"] for row in rows)
    covered = sum(row["constraints_covered"] for row in rows)
    constraint_coverage = covered / expected_constraints if expected_constraints else None
    critical_ids = {case["case_id"] for case in selected if case.get("critical")}
    critical_rows = [row for row in rows if row["case_id"] in critical_ids]
    critical_rate = sum(row["passed"] for row in critical_rows) / len(critical_rows) if critical_rows else None
    blockers = [{"case_id": row["case_id"], "repetition": row["repetition"], "rule": check["rule"], "detail": check["detail"]}
                for row in rows for check in row["checks"] if not check["passed"] and check["rule"] != "RET01"]
    config = fixture["configuration"]
    macro_recall = statistics.mean(recalls) if recalls else None
    passed = data_complete and not blockers and (macro_recall is not None and macro_recall >= config["recall_pass_threshold"]) and (
        constraint_coverage is not None and constraint_coverage >= config["constraint_pass_threshold"]) and (
        critical_rate is not None and critical_rate >= config["critical_pass_threshold"])
    evidence["summary"] = {
        "case_count": len(selected), "query_runs": len(rows), "expected_query_runs": expected_rows,
        "data_complete": data_complete, "recall_cases_per_repetition": len(recalls) // config["repeats"],
        "macro_recall_at_5": macro_recall, "constraint_coverage": constraint_coverage,
        "critical_case_pass_rate": critical_rate, "failed_case_ids": sorted({row["case_id"] for row in rows if not row["passed"]}),
        "noncompensable_failures": blockers,
        "latency_ms": {"p50": percentile([row["latency_ms"] for row in rows], .5),
                       "p95": percentile([row["latency_ms"] for row in rows], .95)},
        "decision": ("PILOT_PASS" if passed else "PILOT_FAIL") if evidence["mode"] == "pilot" else (
            "STOP" if not data_complete else "LOCAL_ENGINEERING_PASS" if passed else "LOCAL_ENGINEERING_FAIL"),
        "paid_model_calls": 0, "generation_effect_accepted": False,
        "limitations": ["Synthetic small corpus only", "No LLM answer/creation evaluation", "No production-capacity evaluation",
                        "Two retrieval repeats do not evaluate generation stability", "No human dual-rater aesthetic calibration"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=BACKEND / "tests/fixtures/rag_eval.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, default=PROJECT / "data/knowledge/models")
    parser.add_argument("--case-ids", help="Comma-separated calibration/pilot subset; never produces full acceptance")
    parser.add_argument("--score-diagnostics", action="store_true", help="Record additional unthresholded scores separately; never changes the scored context")
    args = parser.parse_args()
    output = args.output.resolve()
    evidence_root = (PROJECT / "docs/evidence/RAG").resolve()
    if not output.is_relative_to(evidence_root) or output.suffix != ".json":
        parser.error("--output must be a .json file inside this project's docs/evidence/RAG")
    if output.exists():
        parser.error("output exists; choose a new batch filename to preserve evidence")
    evidence = {"schema_version": 1, "run_id": uuid4().hex, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "mode": "pilot" if args.case_ids else "full", "python": sys.version,
                "platform": platform.platform(), "dependencies": dependency_versions(),
                "score_diagnostics": args.score_diagnostics,
                "scoring_protocol": "Deterministic gold ID/version/text assertions; no LLM judge; no synthetic scores"}
    started = time.perf_counter()
    exit_code = 2
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        validate_fixture(fixture)
        ids = {label.strip() for label in args.case_ids.split(",")} if args.case_ids else {case["case_id"] for case in fixture["cases"]}
        known = {case["case_id"] for case in fixture["cases"]}
        if not ids or ids - known:
            raise ValueError("unknown or empty requested case_ids")
        selected = [case for case in fixture["cases"] if case["case_id"] in ids]
        evidence.update(eval_version=fixture["eval_version"], fixture_sha256=sha256_file(args.fixture),
                        frozen_corpus_sha256=object_hash({key: fixture[key] for key in ("libraries", "documents", "updates", "deletions")}),
                        frozen_configuration=fixture["configuration"], selected_case_ids=[case["case_id"] for case in selected],
                        model=model_binding(args.model_cache.resolve()))
        run_evaluation(fixture, selected, evidence, args.model_cache.resolve())
        summarize(fixture, selected, evidence)
        exit_code = 0 if evidence["summary"]["decision"] in ("LOCAL_ENGINEERING_PASS", "PILOT_PASS") else 1
    except Exception as exc:
        evidence["execution_error"] = {"type": type(exc).__name__, "message": str(exc)}
        evidence["summary"] = {"decision": "STOP", "paid_model_calls": 0, "generation_effect_accepted": False}
    evidence["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    evidence["elapsed_ms"] = (time.perf_counter() - started) * 1000
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"evidence": str(output), **evidence["summary"]}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
