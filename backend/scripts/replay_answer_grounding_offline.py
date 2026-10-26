"""Read-only replay of frozen historical answers, not new model evaluation.

Only this new CLI and its pure tests are involved. It writes JSON to stdout;
the caller can save reviewed evidence without changing source/score files.
"""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import hashlib
import http.client
import importlib.abc
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import urllib.request

VERSION = "historical-answer-replay-v1"
CASE_IDS = tuple([f"R{i:02d}" for i in range(1, 19)] + [f"H{i:02d}" for i in range(1, 9)])
EXPECTED = frozenset(f"{case}-{group}" for case in CASE_IDS for group in ("no_rag", "with_rag"))
NOANSWER = frozenset(("R12", "R15", "R16", "R18", "H07", "H08"))
INVALID = frozenset(("R13", "R14"))
HEADER = re.compile(r"(?m)^\[(K\d+)\] ([^\n]+)（文档([^，\n]+)，版本(\d+)）\s*\n")


class ReplayError(ValueError):
    pass


class OfflineBlocked(RuntimeError):
    pass


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReplayError("duplicate JSON object key: " + key)
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique)


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_json(value, *, sort=True):
    return digest_bytes(json.dumps(value, ensure_ascii=False, sort_keys=sort,
                                  separators=(",", ":")).encode())


def validate_items(manifest):
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) != 52:
        raise ReplayError("manifest must contain exactly 52 items")
    names = []
    for item in items:
        if not isinstance(item, dict):
            raise ReplayError("invalid manifest item")
        name = item.get("item")
        if name != f"{item.get('case_id')}-{item.get('group')}" or name not in EXPECTED:
            raise ReplayError("invalid case/group identity")
        names.append(name)
    if len(set(names)) != 52 or set(names) != EXPECTED:
        raise ReplayError("duplicate or omitted manifest identity")
    metadata = manifest.get("metadata", {})
    if metadata.get("expected_requests") != 52 or set(metadata.get("case_ids", [])) != set(CASE_IDS):
        raise ReplayError("frozen metadata coverage mismatch")
    return items


def validate_inventory(items, payload_names, response_names):
    expected = {item["item"] for item in items}
    if set(payload_names) != expected or len(payload_names) != 52:
        raise ReplayError("payload file set has missing/extra/duplicate identities")
    if set(response_names) != expected or len(response_names) != 52:
        raise ReplayError("response file set has missing/extra/duplicate identities")


def parse_sources(payload, group, binding, query):
    messages = payload.get("messages")
    if not isinstance(messages, list) or any(not isinstance(m, dict) or
            not isinstance(m.get("content"), str) for m in messages):
        raise ReplayError("invalid messages schema")
    users = [m["content"] for m in messages if m.get("role") == "user"]
    if len(users) != 1:
        raise ReplayError("expected one frozen user message")
    prefix = "问题：" + query + "\n"
    if not users[0].startswith(prefix):
        raise ReplayError("frozen query and message prefix differ")
    # Strip the exact frozen query first. Query text may itself contain fake
    # K headers or a copy of the source-section marker; neither is a source.
    user = users[0][len(prefix):]
    if group == "no_rag":
        if user != "本组没有提供任何外部资料。":
            raise ReplayError("no-RAG marker missing")
        if re.search(r"(?m)^\[K\d+\]", user):
            raise ReplayError("unexpected source block in no-RAG payload")
        # retrieval_binding can list candidates, but none were provided here.
        return [], "not_provided"
    marker = "本组检索资料如下（只有列出的K编号可引用）："
    if not user.startswith(marker + "\n"):
        raise ReplayError("with-RAG source section missing")
    context = user[len(marker):]
    headers = list(HEADER.finditer(context))
    if len(headers) != len(re.findall(r"(?m)^\[K\d+\]", context)):
        raise ReplayError("malformed source header")
    status_match = re.search(r"检索状态：([a-z_]+)。", context)
    if not status_match:
        raise ReplayError("retrieval state missing")
    status = status_match.group(1)
    if not headers:
        if "没有可用来源" not in context:
            raise ReplayError("empty source section lacks explicit no-source marker")
        if binding.get("sources"):
            raise ReplayError("provided source binding exists but source block is absent")
        return [], status
    metadata = {}
    for row in binding.get("sources", []):
        key = row.get("citation_id")
        if key in metadata:
            raise ReplayError("duplicate source binding")
        metadata[key] = row
    sources = []
    seen = set()
    for index, header in enumerate(headers):
        cid, title, document, version = header.groups()
        if cid in seen:
            raise ReplayError("duplicate provided citation ID")
        seen.add(cid)
        text = context[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(context)].strip()
        if not text:
            raise ReplayError("empty provided source text")
        bound = metadata.get(cid)
        if not bound or bound.get("fixture_document") != document or bound.get("version_number") != int(version):
            raise ReplayError("source header and frozen version binding differ")
        sources.append({"citation_id": cid, "title": title, "fixture_document": document,
                        "version_number": int(version), "document_id": bound.get("document_id"),
                        "version_id": bound.get("version_id"), "chunk_id": bound.get("chunk_id"),
                        "is_constraint": bound.get("is_constraint"), "text": text})
    if seen != set(metadata):
        raise ReplayError("frozen source binding is not one-to-one with provided blocks")
    return sources, status


def parse_response(envelope):
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ReplayError("expected one historical completion choice")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise ReplayError("incomplete historical completion")
    message = choice.get("message", {})
    if message.get("role") != "assistant" or not isinstance(message.get("content"), str):
        raise ReplayError("historical assistant content missing")
    raw = message["content"]
    text = raw.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\s*```", text, re.S | re.I)
        if not match:
            raise ReplayError("invalid JSON fence")
        text = match.group(1)
    try:
        parsed = strict_json(text)
    except (ValueError, TypeError) as exc:
        raise ReplayError("answer is not standalone JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answer"), str):
        raise ReplayError("answer must be a string")
    citations = parsed.get("citations")
    if not isinstance(citations, list) or any(not isinstance(c, str) or not c for c in citations):
        raise ReplayError("citations must be a string array")
    if not isinstance(parsed.get("abstained"), bool):
        raise ReplayError("abstained must be boolean")
    return parsed, raw


def human_binding(human, item, response_sha):
    key = "rag-" + item
    label = human.get("answers", {}).get(key)
    required = {"fabrication", "used", "refusal", "read"}
    if not isinstance(label, dict) or not required <= set(label) or label["read"] is not True:
        raise ReplayError("human label coverage incomplete: " + key)
    if label["fabrication"] not in ("是", "否") or label["used"] not in ("是", "否", "不适用") or label["refusal"] not in ("是", "否", "不适用"):
        raise ReplayError("human label unresolved: " + key)
    matches = [b for b in human.get("source_bindings", []) if
               Path(str(b.get("source", ""))).name == item + "-response.json"]
    if len(matches) != 1 or matches[0].get("sha256") != response_sha:
        raise ReplayError("human label response SHA binding mismatch: " + key)
    return {"key": key, "version": human.get("version"), "record_sha256": digest_json(label),
            "labels": {k: label[k] for k in ("fabrication", "used", "refusal", "read")},
            "binding": "exact historical response SHA256; read-only comparison"}


def load_checker(root):
    path = root / "backend/app/services/answer_grounding.py"
    spec = importlib.util.spec_from_file_location("replay_pure_answer_grounding", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replay(root, human_path):
    base = root / "docs/evidence/第二批收尾-20261011/B1"
    manifest_path = base / "frozen-52-payloads.json"
    manifest = strict_json(manifest_path.read_text())
    items = validate_items(manifest)
    validate_inventory(items, [p.stem for p in (base / "payloads").glob("*.json")],
                       [p.name.removesuffix("-response.json") for p in base.glob("*-response.json")])
    protected = [manifest_path, human_path,
                 root / "backend/app/services/answer_grounding.py",
                 root / "backend/tests/test_answer_grounding.py",
                 root / "backend/scripts/verify_answer_grounding_offline.py",
                 root / "backend/app/services/knowledge_retrieval.py",
                 root / "backend/app/core/config.py"]
    protected += [base / "payloads" / (x["item"] + ".json") for x in items]
    protected += [base / (x["item"] + "-response.json") for x in items]
    before = {str(p): digest_bytes(p.read_bytes()) for p in protected}
    human = strict_json(human_path.read_text())
    if {key for key in human.get("answers", {}) if key.startswith("rag-")} != {"rag-" + x for x in EXPECTED}:
        raise ReplayError("human RAG label identities are not exactly the frozen 52")
    checker = load_checker(root)
    records = []
    for item in items:
        name = item["item"]
        payload_path = base / "payloads" / (name + ".json")
        response_path = base / (name + "-response.json")
        payload = strict_json(payload_path.read_text())
        if (payload != item["payload"] or digest_json(item["payload"], sort=False) != item["payload_sha256"]
                or digest_json(payload, sort=False) != item["payload_sha256"]):
            raise ReplayError("frozen payload object or declared hash mismatch: " + name)
        sources, retrieval_status = parse_sources(payload, item["group"], item.get("retrieval_binding", {}), item["query"])
        envelope = strict_json(response_path.read_text())
        answer, raw = parse_response(envelope)
        response_sha = digest_bytes(response_path.read_bytes())
        labels = human_binding(human, name, response_sha)
        result = checker.check_answer_grounding(answer["answer"], sources, answer["citations"])
        if result["answer"] != answer["answer"]:
            raise ReplayError("checker changed historical answer")
        descriptors = [{k: v for k, v in s.items() if k != "text"} |
                       {"text_sha256": digest_bytes(s["text"].encode())} for s in sources]
        records.append({"item": name, "case_id": item["case_id"], "group": item["group"],
                        "query": item["query"], "query_input_empty": not item["query"].strip(),
                        "case_scope": "invalid_input" if item["case_id"] in INVALID else
                        "valid_noanswer" if item["case_id"] in NOANSWER else "other_answerable_or_constraint_case",
                        "payload_file": str(payload_path.relative_to(root)),
                        "payload_file_sha256": before[str(payload_path)],
                        "frozen_payload_sha256": item["payload_sha256"],
                        "response_file": str(response_path.relative_to(root)), "response_sha256": response_sha,
                        "completion_version": {k: envelope.get(k) for k in ("id", "model", "created")},
                        "raw_content_sha256": digest_bytes(raw.encode()),
                        "answer": answer["answer"], "answer_sha256": digest_bytes(answer["answer"].encode()),
                        "abstained": answer["abstained"], "citations": answer["citations"],
                        "provided_sources_sha256": digest_json(sources), "source_count": len(sources),
                        "sources": descriptors, "retrieval_status": retrieval_status,
                        "assistant_check": {k: result[k] for k in
                                            ("version", "status", "scope", "claims", "citation_issues")},
                        "human_comparison": labels})
    after = {str(p): digest_bytes(p.read_bytes()) for p in protected}
    if before != after:
        raise ReplayError("protected historical/source/label files changed")
    h05 = next(x for x in records if x["item"] == "H05-with_rag")
    retained = h05["assistant_check"]["status"] == "NEED_REVIEW"
    groups = {}
    for group in ("no_rag", "with_rag"):
        rows = [x for x in records if x["group"] == group]
        groups[group] = {"records": len(rows), "hint_states": dict(Counter(x["assistant_check"]["status"] for x in rows)),
                         "valid_noanswer_records": sum(x["case_scope"] == "valid_noanswer" for x in rows),
                         "invalid_input_records": sum(x["case_scope"] == "invalid_input" for x in rows),
                         "human_fabrication_labels": dict(Counter(x["human_comparison"]["labels"]["fabrication"] for x in rows))}
    return {"schema": VERSION, "date": "2026-10-25",
            "status": "OFFLINE_REPLAY_COMPLETE" if retained else "H05_REGRESSION_NOT_RETAINED",
            "scope": "existing historical model answers only; no new generation or production integration",
            "coverage": {"manifest_items": len(items), "payloads": len(items), "responses": len(records),
                         "parsed_responses": len(records), "source_parses": len(records),
                         "human_labels_sha_bound": len(records), "missing": [], "duplicates": [],
                         "provided_source_blocks": sum(x["source_count"] for x in records),
                         "empty_provided_source_records": sum(x["source_count"] == 0 for x in records)},
            "by_group": groups, "h05_hint_retained": retained,
            "human_label_source": {"path": str(human_path), "sha256": before[str(human_path)],
                                   "version": human.get("version"), "exported_at": human.get("exported_at")},
            "protected_hashes_before": before, "protected_hashes_after": after,
            "protected_unchanged": before == after,
            "source_sha256": {str(p.relative_to(root)): digest_bytes(p.read_bytes()) for p in
                              [root / "backend/app/services/answer_grounding.py", Path(__file__).resolve(),
                               root / "backend/tests/test_answer_grounding_replay.py"]},
            "records": records,
            "limitations": ["NEED_REVIEW is not a fabrication label",
                            "NOT_CHECKED is not correctness or acceptance",
                            "Text evidence is not full semantic verification or source truth",
                            "Existing human judgments and strict original gate are unchanged",
                            "No new model answer was generated; limited lexical coverage only"]}


class OfflineGuard:
    def __init__(self):
        self.phase = "execution"
        self.events = {"probe": [], "execution": []}

    def block(self, kind):
        def denied(*args, **kwargs):
            self.events[self.phase].append(kind)
            raise OfflineBlocked("offline replay blocks " + kind)
        return denied

    def install(self):
        guard = self

        class NoModelImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "app" or fullname.startswith("app.") or fullname.split(".")[0] in {
                        "httpx", "requests", "openai", "anthropic", "torch", "transformers",
                        "sentence_transformers", "fastembed", "qdrant_client", "dotenv"}:
                    guard.block("forbidden_import:" + fullname)()
                return None

        def audit(event, args):
            if event.startswith("socket."):
                self.block("socket_audit:" + event)()
            if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
                name = Path(os.fsdecode(args[0])).name
                if name == ".env" or name.startswith(".env."):
                    self.block("dotenv_read")()

        sys.meta_path.insert(0, NoModelImports())
        sys.addaudithook(audit)
        socket.create_connection = self.block("socket_connect")
        socket.getaddrinfo = self.block("socket_dns")
        for name in ("connect", "connect_ex", "sendto", "sendmsg"):
            if hasattr(socket.socket, name):
                setattr(socket.socket, name, self.block("socket_" + name))
        http.client.HTTPConnection.connect = self.block("http_client")
        http.client.HTTPSConnection.connect = self.block("https_client")
        urllib.request.urlopen = self.block("urllib_urlopen")

    def probe_once(self):
        self.phase = "probe"
        for call in (lambda: socket.create_connection(("127.0.0.1", 9)),
                     lambda: urllib.request.urlopen("https://offline.invalid"),
                     lambda: open("/__offline_probe__/.env")):
            try:
                call()
            except OfflineBlocked:
                pass
        self.phase = "execution"
        return len(self.events["probe"]) == 3

    def report(self):
        return {"probe_events": self.events["probe"], "probe_blocked": len(self.events["probe"]),
                "execution_attempts": self.events["execution"], "real_model_calls": 0,
                "model_cost_cny": 0, "dotenv_execution_attempts": sum(
                    "dotenv" in event for event in self.events["execution"]),
                "os_network_sandbox": "not applied; socket audit + HTTP/import/.env blocks"}


def run_tests(root, guard):
    probe_ok = guard.probe_once()
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest

    class Collector:
        def __init__(self):
            self.cases = []
            self.errors = 0

        def pytest_runtest_logreport(self, report):
            if report.when == "call":
                self.cases.append({"nodeid": report.nodeid, "outcome": report.outcome})
            elif report.failed:
                self.errors += 1

    collector = Collector()
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = pytest.main(["--noconftest", "-p", "no:cacheprovider", "-c", "/dev/null",
                            "--rootdir", str(root / "backend"), "-q",
                            str(root / "backend/tests/test_answer_grounding_replay.py")], plugins=[collector])
    counts = dict(Counter(x["outcome"] for x in collector.cases))
    ok = code == 0 and probe_ok and not guard.events["execution"]
    return {"schema": "historical-replay-tests-v1", "status": "PASS" if ok else "FAIL",
            "pytest_exit_code": int(code), "counts": counts, "errors": collector.errors,
            "cases": collector.cases, "output": output.getvalue(), "network": guard.report()}, 0 if ok else 1


def main(argv=None):
    guard = OfflineGuard()
    guard.install()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--human-labels", type=Path)
    parser.add_argument("--run-tests", action="store_true", help="run only new replay tests")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    if args.run_tests:
        result, code = run_tests(root, guard)
    else:
        if args.human_labels is None:
            parser.error("--human-labels is required for historical replay")
        try:
            result = replay(root, args.human_labels.resolve())
            result["network"] = guard.report()
            code = 0 if result["h05_hint_retained"] and not guard.events["execution"] else 1
        except (ReplayError, OSError, ValueError, OfflineBlocked) as exc:
            result = {"schema": VERSION, "status": "INTEGRITY_STOP", "error": str(exc),
                      "network": guard.report()}
            code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
