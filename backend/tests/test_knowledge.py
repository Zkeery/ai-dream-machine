"""Knowledge contracts with real local Qdrant + deterministic test embeddings.

Chinese semantic model effectiveness is separately measured by evaluate_rag.py;
this suite makes no network/LLM calls and never opens the user's vector store.
"""
from __future__ import annotations

import io

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.core import config
from app.core.errors import AppError
from app.main import app
from app.services import knowledge_retrieval as retrieval
from app.services import knowledge_store as store


class ContractEmbedding:
    def embed(self, texts, **kwargs):
        for text in texts:
            vector = np.zeros(512, dtype=np.float32)
            vector[0 if "角色" in text else 1 if "地点" in text else 2] = 1
            yield vector

    def query_embed(self, text):
        return self.embed([text])


@pytest.fixture
def knowledge(data_dirs, monkeypatch):
    retrieval.close()
    monkeypatch.setattr(retrieval, "_embedding_model", lambda: ContractEmbedding())
    # Existing contract tests isolate retrieval; adequacy is covered in dedicated tests.
    monkeypatch.setattr(config.settings, "knowledge_adequacy_enabled", False)
    monkeypatch.setattr(config.settings, "knowledge_hybrid_retrieval_enabled", False)
    yield
    retrieval.close()


def add(owner="one", library=None, text="角色林澈是钟表修理师。", **kwargs):
    library = library or store.create_library(owner, "故事设定")["library_id"]
    return store.save_document(owner, library, "setting.md", text.encode(), **kwargs)


def test_immutable_versions_and_only_latest_retrieved(knowledge):
    doc = add()
    old_context = retrieval.build_context("one", [doc["library_id"]], "角色职业")
    changed = store.replace_document("one", doc["document_id"], "new.md", "角色林澈是飞行员。".encode())
    assert changed["version_number"] == 2
    assert changed["version_id"] != doc["version_id"]
    assert store.get_version("one", doc["document_id"], doc["version_id"])["text"] == "角色林澈是钟表修理师。"
    context = retrieval.build_context("one", [doc["library_id"]], "角色职业")
    assert {source["version_id"] for source in context["sources"]} == {changed["version_id"]}
    assert store.context_changes("one", old_context)
    assert not store.context_changes("one", context)


def test_owner_and_library_filters_are_applied_in_vector_database(knowledge):
    own = add()
    another = add("two", text="角色林澈掌握另一个账号的秘密。")
    second = add("one", text="角色林澈在第二个库。")
    context = retrieval.build_context("one", [own["library_id"]], "角色林澈")
    assert {s["document_id"] for s in context["sources"]} == {own["document_id"]}
    assert another["document_id"] not in str(context)
    assert second["document_id"] not in str(context)
    with pytest.raises(AppError, match="不存在"):
        retrieval.build_context("one", [another["library_id"]], "角色")
    for operation in (
        lambda: store.get_document("two", own["document_id"]),
        lambda: store.delete_document("two", own["document_id"]),
        lambda: store.update_document("two", own["document_id"], {"title": "窃取"}),
        lambda: store.replace_document("two", own["document_id"], "bad.txt", b"stolen"),
        lambda: store.delete_library("two", own["library_id"]),
    ):
        with pytest.raises(AppError) as error:
            operation()
        assert error.value.status_code == 404


def test_no_match_is_explicit(knowledge):
    doc = add()
    context = retrieval.build_context("one", [doc["library_id"]], "地点在哪")
    assert context["status"] == "no_match"
    assert context["sources"] == []
    assert context["warnings"]


def test_constraints_are_complete_even_without_similarity(knowledge):
    text = "角色永远不能恢复视力。\n\n" + "一条固定的规则。" * 100
    doc = add(text=text, is_constraint=True)
    context = retrieval.build_context("one", [doc["library_id"]], "地点在哪")
    assert context["sources"][0]["text"] == text
    assert context["sources"][0]["score"] is None
    assert context["sources"][0]["is_constraint"]
    assert context["sources"][0]["citation_id"] == "K1"


def test_constraint_budget_rejects_without_truncating_or_publishing(knowledge):
    doc = add(text="甲" * 3500, is_constraint=True)
    with pytest.raises(AppError) as error:
        add(library=doc["library_id"], text="乙" * 3000, is_constraint=True)
    assert error.value.code == "KNOWLEDGE_CONSTRAINT_LIMIT"
    assert len(store.list_documents("one", doc["library_id"])) == 1
    second = add(text="丙" * 3500, is_constraint=True)
    with pytest.raises(AppError) as error:
        retrieval.build_context("one", [doc["library_id"], second["library_id"]], "角色")
    assert error.value.code == "KNOWLEDGE_CONSTRAINT_LIMIT"


def test_deletion_excludes_search_but_retains_citation_snapshot(knowledge):
    doc = add()
    snapshot = retrieval.build_context("one", [doc["library_id"]], "角色")
    store.delete_document("one", doc["document_id"])
    assert retrieval.build_context("one", [doc["library_id"]], "角色")["sources"] == []
    assert snapshot["sources"][0]["text"] == "角色林澈是钟表修理师。"
    assert store.context_changes("one", snapshot)
    store.delete_library("one", doc["library_id"])
    assert store.list_libraries("one") == []
    assert store.context_changes("one", snapshot)


def test_metadata_edit_creates_new_immutable_version(knowledge):
    doc = add()
    edited = store.update_document("one", doc["document_id"], {"title": "角色卡", "category": "character", "is_constraint": True})
    assert edited["version_number"] == 2
    assert edited["is_constraint"]
    detail = store.get_document("one", doc["document_id"])
    assert len(detail["versions"]) == 2
    assert detail["text"] == "角色林澈是钟表修理师。"
    assert store.get_version("one", doc["document_id"], doc["version_id"])["is_constraint"] is False


def test_failed_index_does_not_replace_current_version(knowledge, monkeypatch):
    doc = add()
    def fail(*args, **kwargs):
        raise AppError("KNOWLEDGE_INDEX_FAILED", "索引失败", 503)
    monkeypatch.setattr(retrieval, "index_version", fail)
    with pytest.raises(AppError):
        store.replace_document("one", doc["document_id"], "new.md", "角色新资料".encode())
    detail = store.get_document("one", doc["document_id"])
    assert detail["version_id"] == doc["version_id"]
    assert len(detail["versions"]) == 1


def test_missing_vector_index_is_failure_not_no_match(knowledge):
    doc = add()
    retrieval._vector_client().delete_collection(retrieval.COLLECTION)
    retrieval.close()
    with pytest.raises(AppError) as error:
        retrieval.build_context("one", [doc["library_id"]], "角色")
    assert error.value.code == "KNOWLEDGE_INDEX_INCONSISTENT"


@pytest.mark.parametrize("filename,content,code", [
    ("a.exe", b"valid text", "KNOWLEDGE_FILE_TYPE"),
    ("a.pdf", b"fake pdf", "KNOWLEDGE_FILE_TYPE"),
    ("a.txt", b"\x00binary", "KNOWLEDGE_FILE_TYPE"),
    ("a.md", b"\xff", "KNOWLEDGE_ENCODING"),
    ("a.md", b"", "KNOWLEDGE_FILE_SIZE"),
    ("a.txt", b" " * 50, "KNOWLEDGE_NO_TEXT"),
    ("a.txt", b"a" * (store.MAX_UPLOAD_BYTES + 1), "KNOWLEDGE_FILE_SIZE"),
    ("a.txt", b"a" * (store.MAX_TEXT_CHARS + 1), "KNOWLEDGE_TEXT_LIMIT"),
])
def test_upload_validation(filename, content, code):
    with pytest.raises(AppError) as error:
        store.extract_text(filename, content)
    assert error.value.code == code


def test_text_pdf_empty_and_encrypted_are_explicit():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(AppError) as error:
        store.extract_text("scan.pdf", stream.getvalue())
    assert error.value.code == "KNOWLEDGE_NO_TEXT"
    writer.encrypt("secret")
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(AppError) as error:
        store.extract_text("locked.pdf", stream.getvalue())
    assert error.value.code == "KNOWLEDGE_PDF_ENCRYPTED"


def test_text_layer_pdf_is_extracted():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 10 100 Td (A watchmaker in the city.) Tj ET")
    page[NameObject("/Contents")] = content
    stream = io.BytesIO()
    writer.write(stream)
    assert "A watchmaker in the city." in store.extract_text("world.pdf", stream.getvalue())


def test_chunks_cover_long_source_and_safe_display_filename(knowledge):
    text = "角色" + "长" * 1200
    chunks = store.chunk_text(text)
    assert max(len(chunk["text"]) for chunk in chunks) <= 380
    reconstructed = chunks[0]["text"] + "".join(chunk["text"][60:] for chunk in chunks[1:])
    assert reconstructed == text
    lib = store.create_library("one", "库")
    doc = store.save_document("one", lib["library_id"], "../../safe.txt", text.encode())
    assert doc["original_name"] == "safe.txt"


@pytest.mark.asyncio
async def test_api_full_crud_search_and_owner_guard(knowledge):
    app.dependency_overrides[get_current_user] = lambda: "one"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/knowledge/libraries", json={"name": "故事知识库"})
            assert created.status_code == 201
            library_id = created.json()["library_id"]
            base = f"/api/knowledge/libraries/{library_id}"
            uploaded = await client.post(base + "/documents", files={"file": ("character.md", "角色林澈是钟表修理师。".encode(), "text/markdown")}, data={"category": "character", "is_constraint": "true"})
            assert uploaded.status_code == 201
            doc = uploaded.json()
            path = f'/api/knowledge/documents/{doc["document_id"]}'
            assert (await client.get(path)).json()["text"]
            found = (await client.post(base + "/search", json={"query": "地点在哪"})).json()
            assert found["sources"][0]["is_constraint"]
            changed = await client.patch(path, json={"title": "人物设定", "is_constraint": False})
            assert changed.json()["version_number"] == 2
            assert (await client.get(path + f'/versions/{doc["version_id"]}')).status_code == 200
            assert (await client.patch(base, json={"name": "更新的库"})).json()["name"] == "更新的库"
            app.dependency_overrides[get_current_user] = lambda: "two"
            for method, url, options in [
                ("get", base + "/documents", {}), ("get", path, {}),
                ("post", base + "/search", {"json": {"query": "角色"}}),
                ("post", base + "/documents", {"files": {"file": ("test.md", b"hello")}}),
                ("patch", path, {"json": {"is_constraint": True}}),
                ("delete", path, {}),
            ]:
                response = await getattr(client, method)(url, **options)
                assert response.status_code == 404
                assert "error" in response.json()
            app.dependency_overrides[get_current_user] = lambda: "one"
            assert (await client.delete(path)).status_code == 200
            assert (await client.delete(base)).status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_adequacy_refusal_moves_ordinary_hits(knowledge, monkeypatch):
    import app.models.llm_client as llm_mod
    monkeypatch.setattr(config.settings, "knowledge_adequacy_enabled", True)

    class Judge:
        def generate_json(self, system, user, model_cls, **kwargs):
            return model_cls.model_validate({
                "sufficient": False,
                "reason": "资料未写明母亲姓名",
                "unsupported_facts": ["母亲姓名"],
            })

    monkeypatch.setattr(llm_mod, "LLMClient", Judge)
    doc = add(text="角色陆遥是照相馆摄影师，左眼视力较弱。")
    context = retrieval.build_context("one", [doc["library_id"]], "角色陆遥母亲的姓名是什么？")
    assert context["status"] == "insufficient"
    assert context["sources"] == []
    assert context["rejected_sources"]
    assert context["adequacy"]["sufficient"] is False
    assert any("资料未覆盖" in warning for warning in context["warnings"])


def test_adequacy_pass_keeps_sources(knowledge, monkeypatch):
    import app.models.llm_client as llm_mod
    monkeypatch.setattr(config.settings, "knowledge_adequacy_enabled", True)

    class Judge:
        def generate_json(self, system, user, model_cls, **kwargs):
            return model_cls.model_validate({
                "sufficient": True,
                "reason": "资料写明了职业",
                "unsupported_facts": [],
            })

    monkeypatch.setattr(llm_mod, "LLMClient", Judge)
    doc = add(text="角色林澈是钟表修理师。")
    context = retrieval.build_context("one", [doc["library_id"]], "角色职业")
    assert context["status"] == "matched"
    assert context["sources"]
    assert context["rejected_sources"] == []
    assert context["adequacy"]["sufficient"] is True


def test_adequacy_failure_degrades_to_insufficient(knowledge, monkeypatch):
    import app.models.llm_client as llm_mod
    monkeypatch.setattr(config.settings, "knowledge_adequacy_enabled", True)

    class Judge:
        def generate_json(self, system, user, model_cls, **kwargs):
            raise AppError("MODEL_REQUEST_INTERRUPTED", "文本请求超时", 502)

    monkeypatch.setattr(llm_mod, "LLMClient", Judge)
    doc = add(text="角色林澈是钟表修理师。")
    context = retrieval.build_context("one", [doc["library_id"]], "角色职业")
    assert context["status"] == "insufficient"
    assert context["adequacy"]["degraded"] is True
    assert context["adequacy"]["sufficient"] is False
    assert context["rejected_sources"]
    assert not any(not source.get("is_constraint") for source in context["sources"])


def test_with_knowledge_includes_insufficiency_fields():
    from app.services import prompts
    payload = prompts.with_knowledge("写剧本", {
        "status": "insufficient",
        "warnings": ["资料未覆盖：母亲姓名"],
        "adequacy": {"sufficient": False, "reason": "未写明", "unsupported_facts": ["母亲姓名"], "degraded": False},
        "sources": [],
        "rejected_sources": [{"citation_id": "R1", "title": "陆遥", "version_id": "v1", "text": "摄影师", "is_constraint": False}],
    })
    assert "insufficient" in payload
    assert "rejected_sources" in payload
    assert "母亲姓名" in payload


def test_knowledge_rule_mentions_insufficient():
    from app.services import prompts
    assert "insufficient" in prompts.KNOWLEDGE_RULE
    assert "rejected_sources" in prompts.KNOWLEDGE_RULE


def test_adequacy_judgment_accepts_common_aliases():
    from app.services.knowledge_retrieval import AdequacyJudgment
    parsed = AdequacyJudgment.model_validate({
        "is_sufficient": False,
        "reasoning": "资料未写明售价",
        "missing_facts": ["销售价格"],
    })
    assert parsed.sufficient is False
    assert "售价" in parsed.reason
    assert parsed.unsupported_facts == ["销售价格"]


def test_classify_knowledge_query_fact_vs_creative():
    from app.services.knowledge_retrieval import classify_knowledge_query
    assert classify_knowledge_query("照相馆的陆遥，他母亲的姓名是什么？") == "fact"
    assert classify_knowledge_query("青禾杯玻璃茶杯的销售价格是多少钱？") == "fact"
    assert classify_knowledge_query("照相馆的陆遥遇到红月时，拍摄安排需要考虑哪些人物弱点和小镇规则？") == "creative"
    assert classify_knowledge_query("写一句保证拾光能治好失眠的广告语") == "creative"
    assert classify_knowledge_query("能不能在第一幕让观众知道修钟表的老人就是失踪船长？") == "creative"


def test_creative_query_skips_llm_and_keeps_sources(knowledge, monkeypatch):
    import app.models.llm_client as llm_mod
    monkeypatch.setattr(config.settings, "knowledge_adequacy_enabled", True)

    class Boom:
        def generate_json(self, *args, **kwargs):
            raise AssertionError("creative grounding must not call adequacy LLM")

    monkeypatch.setattr(llm_mod, "LLMClient", Boom)
    doc = add(text="角色陆遥是照相馆摄影师，左眼视力较弱。")
    context = retrieval.build_context("one", [doc["library_id"]], "角色陆遥遇到红月时，拍摄安排需要考虑哪些人物弱点？")
    assert context["status"] == "matched"
    assert context["sources"]
    assert context["rejected_sources"] == []
    assert context["adequacy"]["sufficient"] is True
    assert context["adequacy"]["skipped_llm"] is True
    assert context["adequacy"]["question_kind"] == "creative"
