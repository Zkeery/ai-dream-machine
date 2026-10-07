"""Owner-scoped immutable knowledge versions; active metadata is stored in SQLite.

Vector writes happen before publishing a version. Queries filter by the current
SQLite version IDs, so a failed publication can never expose orphan vector data.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.core import config
from app.core.errors import AppError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_CONSTRAINT_CHARS = 6000
MAX_CHUNKS = 800
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
 library_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
 description TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS libraries_owner ON libraries(owner_id, deleted);
CREATE TABLE IF NOT EXISTS documents (
 document_id TEXT PRIMARY KEY, library_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 version_id TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS documents_library ON documents(library_id, owner_id, deleted);
CREATE TABLE IF NOT EXISTS versions (
 version_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, version_number INTEGER NOT NULL,
 title TEXT NOT NULL, category TEXT NOT NULL, is_constraint INTEGER NOT NULL,
 original_name TEXT NOT NULL, text TEXT NOT NULL, chunks TEXT NOT NULL,
 chunk_count INTEGER NOT NULL, created_at REAL NOT NULL,
 UNIQUE(document_id, version_number)
);
"""


@contextmanager
def connection():
    root = config.DATA_DIR / "knowledge"
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "knowledge.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _library(conn, owner_id: str, library_id: str) -> dict:
    row = conn.execute("SELECT * FROM libraries WHERE library_id=? AND owner_id=? AND deleted=0", (library_id, owner_id)).fetchone()
    if not row:
        raise AppError("KNOWLEDGE_NOT_FOUND", "知识库不存在或无权访问", 404)
    return dict(row)


def _public_library(conn, row: dict) -> dict:
    count = conn.execute("SELECT COUNT(*) FROM documents WHERE library_id=? AND deleted=0", (row["library_id"],)).fetchone()[0]
    return {k: row[k] for k in ("library_id", "name", "description", "revision", "created_at", "updated_at")} | {"document_count": count}


def list_libraries(owner_id: str) -> list[dict]:
    with connection() as conn:
        return [_public_library(conn, dict(r)) for r in conn.execute("SELECT * FROM libraries WHERE owner_id=? AND deleted=0 ORDER BY updated_at DESC", (owner_id,))]


def create_library(owner_id: str, name: str, description: str = "") -> dict:
    now = time.time()
    library_id = uuid.uuid4().hex
    with _write_lock, connection() as conn:
        if conn.execute("SELECT COUNT(*) FROM libraries WHERE owner_id=? AND deleted=0", (owner_id,)).fetchone()[0] >= 50:
            raise AppError("KNOWLEDGE_LIBRARY_LIMIT", "每个账号最多创建 50 个知识库", 422)
        conn.execute("INSERT INTO libraries(library_id,owner_id,name,description,created_at,updated_at) VALUES(?,?,?,?,?,?)", (library_id, owner_id, name.strip(), description.strip(), now, now))
        return _public_library(conn, _library(conn, owner_id, library_id))


def update_library(owner_id: str, library_id: str, updates: dict) -> dict:
    with _write_lock, connection() as conn:
        row = _library(conn, owner_id, library_id)
        name = updates.get("name") if updates.get("name") is not None else row["name"]
        description = updates.get("description") if updates.get("description") is not None else row["description"]
        conn.execute("UPDATE libraries SET name=?,description=?,updated_at=? WHERE library_id=?", (name.strip(), description.strip(), time.time(), library_id))
        return _public_library(conn, _library(conn, owner_id, library_id))


def delete_library(owner_id: str, library_id: str) -> None:
    with _write_lock, connection() as conn:
        _library(conn, owner_id, library_id)
        conn.execute("UPDATE libraries SET deleted=1,revision=revision+1,updated_at=? WHERE library_id=?", (time.time(), library_id))


def validate_libraries(owner_id: str, library_ids: list[str]) -> list[dict]:
    if len(library_ids) > 5:
        raise AppError("KNOWLEDGE_LIBRARY_LIMIT", "一次创作最多绑定 5 个知识库", 422)
    with connection() as conn:
        return [_public_library(conn, _library(conn, owner_id, lib_id)) for lib_id in dict.fromkeys(library_ids)]


def _document(conn, owner_id: str, document_id: str) -> dict:
    row = conn.execute("""SELECT d.*,v.title,v.category,v.is_constraint,v.version_number,v.original_name,
        v.text,v.chunks,v.chunk_count,v.created_at AS version_created_at
        FROM documents d JOIN versions v ON v.version_id=d.version_id
        JOIN libraries l ON l.library_id=d.library_id
        WHERE d.document_id=? AND d.owner_id=? AND d.deleted=0 AND l.deleted=0""", (document_id, owner_id)).fetchone()
    if not row:
        raise AppError("KNOWLEDGE_NOT_FOUND", "资料不存在或无权访问", 404)
    result = dict(row)
    result["is_constraint"] = bool(result["is_constraint"])
    return result


def _public_document(row: dict, include_text: bool = False) -> dict:
    keys = ("document_id", "library_id", "version_id", "version_number", "title", "category", "is_constraint", "original_name", "chunk_count", "created_at", "updated_at")
    result = {k: row[k] for k in keys}
    result["character_count"] = len(row["text"])
    if include_text:
        result["text"] = row["text"]
    return result


def list_documents(owner_id: str, library_id: str) -> list[dict]:
    with connection() as conn:
        _library(conn, owner_id, library_id)
        ids = conn.execute("SELECT document_id FROM documents WHERE library_id=? AND owner_id=? AND deleted=0 ORDER BY updated_at DESC", (library_id, owner_id)).fetchall()
        return [_public_document(_document(conn, owner_id, r[0])) for r in ids]


def get_document(owner_id: str, document_id: str) -> dict:
    with connection() as conn:
        result = _public_document(_document(conn, owner_id, document_id), True)
        result["versions"] = [dict(r) for r in conn.execute("SELECT version_id,version_number,title,category,is_constraint,created_at,chunk_count FROM versions WHERE document_id=? ORDER BY version_number DESC", (document_id,))]
        for version in result["versions"]:
            version["is_constraint"] = bool(version["is_constraint"])
        return result


def get_version(owner_id: str, document_id: str, version_id: str) -> dict:
    with connection() as conn:
        _document(conn, owner_id, document_id)
        row = conn.execute("SELECT * FROM versions WHERE document_id=? AND version_id=?", (document_id, version_id)).fetchone()
        if not row:
            raise AppError("KNOWLEDGE_NOT_FOUND", "资料版本不存在", 404)
        result = dict(row)
        result.pop("chunks")
        result["is_constraint"] = bool(result["is_constraint"])
        return result


def extract_text(filename: str, content: bytes) -> str:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise AppError("KNOWLEDGE_FILE_SIZE", "资料不能为空，且每份不能超过 5 MB", 422)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".md", ".txt", ".pdf"}:
        raise AppError("KNOWLEDGE_FILE_TYPE", "仅支持 Markdown、TXT 和有文字层的 PDF", 422)
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise AppError("KNOWLEDGE_FILE_TYPE", "文件内容不是有效 PDF", 422)
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise AppError("KNOWLEDGE_PDF_ENCRYPTED", "请先解密 PDF 再上传", 422)
            if len(reader.pages) > 300:
                raise AppError("KNOWLEDGE_TEXT_LIMIT", "PDF 最多支持 300 页，请拆分上传", 422)
            parts = []
            count = 0
            for page in reader.pages:
                part = page.extract_text() or ""
                count += len(part)
                if count > MAX_TEXT_CHARS:
                    raise AppError("KNOWLEDGE_TEXT_LIMIT", "提取文本不能超过 20 万字符，请拆分上传", 422)
                parts.append(part)
            text = "\n\n".join(parts)
        except AppError:
            raise
        except Exception:
            raise AppError("KNOWLEDGE_PDF_INVALID", "无法读取 PDF，请重新导出文字版文件", 422) from None
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise AppError("KNOWLEDGE_ENCODING", "文本文件需要使用 UTF-8 编码", 422) from None
        if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise AppError("KNOWLEDGE_FILE_TYPE", "检测到非文本内容，请上传有效文本文件", 422)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise AppError("KNOWLEDGE_NO_TEXT", "未提取到文字；扫描版 PDF 请先转换为文字版", 422)
    if len(text) > MAX_TEXT_CHARS:
        raise AppError("KNOWLEDGE_TEXT_LIMIT", "提取文本不能超过 20 万字符，请拆分上传", 422)
    return text


def chunk_text(text: str) -> list[dict]:
    """Prefer paragraphs/headings; bound Chinese input below the model's 512 tokens."""
    chunks = []
    for paragraph in re.split(r"\n\s*\n|(?=^#{1,6}\s)", text, flags=re.MULTILINE):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        start = 0
        while start < len(paragraph):
            end = min(start + 380, len(paragraph))
            if end < len(paragraph):
                split = max(paragraph.rfind(mark, start + 220, end) for mark in ("。", "！", "？", "\n", ". "))
                if split > start:
                    end = split + 1
            chunks.append({"chunk_id": uuid.uuid4().hex, "text": paragraph[start:end]})
            if end == len(paragraph):
                break
            start = end - 60
    if len(chunks) > MAX_CHUNKS:
        raise AppError("KNOWLEDGE_CHUNK_LIMIT", "资料段落过多，请分成较小的文件上传", 422)
    return chunks


def _check_constraints(conn, library_id: str, text: str, is_constraint: bool, exclude_id: str = "") -> None:
    if not is_constraint:
        return
    total = conn.execute("""SELECT COALESCE(SUM(LENGTH(v.text)),0) FROM documents d
        JOIN versions v ON v.version_id=d.version_id
        WHERE d.library_id=? AND d.deleted=0 AND v.is_constraint=1 AND d.document_id<>?""", (library_id, exclude_id)).fetchone()[0]
    if total + len(text) > MAX_CONSTRAINT_CHARS:
        raise AppError("KNOWLEDGE_CONSTRAINT_LIMIT", "每个知识库的固定约束合计最多 6000 字，请精简后重试", 422)


def save_document(owner_id: str, library_id: str, filename: str, content: bytes, title: str | None = None,
                  category: str = "other", is_constraint: bool = False, document_id: str | None = None) -> dict:
    text = extract_text(filename, content)
    return _save_text(owner_id, library_id, filename, text, title, category, is_constraint, document_id)


def _save_text(owner_id: str, library_id: str, filename: str, text: str, title: str | None,
               category: str, is_constraint: bool, document_id: str | None) -> dict:
    from app.services import knowledge_retrieval
    # Only basename is retained as display metadata; the supplied filename is never a path.
    filename = Path(filename.replace("\\", "/")).name[:180]
    title = (title if title is not None else Path(filename).stem).strip()
    if not title or len(title) > 160:
        raise AppError("KNOWLEDGE_TITLE_INVALID", "资料标题应为 1 至 160 个字符", 422)
    if category not in {"world", "character", "plot", "style", "brand", "other"}:
        raise AppError("KNOWLEDGE_CATEGORY_INVALID", "资料分类不合法", 422)
    with _write_lock, connection() as conn:
        _library(conn, owner_id, library_id)
        old = _document(conn, owner_id, document_id) if document_id else None
        if old and old["library_id"] != library_id:
            raise AppError("KNOWLEDGE_NOT_FOUND", "资料不属于该知识库", 404)
        if not old and conn.execute("SELECT COUNT(*) FROM documents WHERE library_id=? AND deleted=0", (library_id,)).fetchone()[0] >= 100:
            raise AppError("KNOWLEDGE_DOCUMENT_LIMIT", "每个知识库最多存放 100 份资料", 422)
        _check_constraints(conn, library_id, text, is_constraint, document_id or "")
        document_id = document_id or uuid.uuid4().hex
        version_id = uuid.uuid4().hex
        version_number = old["version_number"] + 1 if old else 1
        chunks = chunk_text(text)
        knowledge_retrieval.index_version(owner_id, library_id, document_id, version_id, chunks)
        now = time.time()
        conn.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (version_id, document_id, version_number, title, category, int(is_constraint), filename, text, json.dumps(chunks, ensure_ascii=False), len(chunks), now))
        if old:
            conn.execute("UPDATE documents SET version_id=?,updated_at=? WHERE document_id=?", (version_id, now, document_id))
        else:
            conn.execute("INSERT INTO documents(document_id,library_id,owner_id,version_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", (document_id, library_id, owner_id, version_id, now, now))
        conn.execute("UPDATE libraries SET revision=revision+1,updated_at=? WHERE library_id=?", (now, library_id))
        return _public_document(_document(conn, owner_id, document_id))


def replace_document(owner_id: str, document_id: str, filename: str, content: bytes,
                     title: str | None = None, category: str | None = None, is_constraint: bool | None = None) -> dict:
    with _write_lock, connection() as conn:
        old = _document(conn, owner_id, document_id)
        return save_document(owner_id, old["library_id"], filename, content, title if title is not None else old["title"], category or old["category"], old["is_constraint"] if is_constraint is None else is_constraint, document_id)


def update_document(owner_id: str, document_id: str, updates: dict) -> dict:
    with _write_lock, connection() as conn:
        old = _document(conn, owner_id, document_id)
        values = {k: v for k, v in updates.items() if v is not None}
        return _save_text(owner_id, old["library_id"], old["original_name"], old["text"], values.get("title", old["title"]), values.get("category", old["category"]), values.get("is_constraint", old["is_constraint"]), document_id)


def delete_document(owner_id: str, document_id: str) -> None:
    with _write_lock, connection() as conn:
        row = _document(conn, owner_id, document_id)
        now = time.time()
        conn.execute("UPDATE documents SET deleted=1,updated_at=? WHERE document_id=?", (now, document_id))
        conn.execute("UPDATE libraries SET revision=revision+1,updated_at=? WHERE library_id=?", (now, row["library_id"]))


def active_snapshot(owner_id: str, library_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """Take library revisions and active documents under the same mutation lock."""
    with _write_lock, connection() as conn:
        libraries = [_library(conn, owner_id, x) for x in dict.fromkeys(library_ids)]
        docs = []
        for library in libraries:
            ids = conn.execute("SELECT document_id FROM documents WHERE library_id=? AND owner_id=? AND deleted=0", (library["library_id"], owner_id)).fetchall()
            docs.extend(_document(conn, owner_id, r[0]) for r in ids)
        return libraries, docs


def context_changes(owner_id: str, context: dict) -> list[str]:
    changes = []
    with connection() as conn:
        for library_id, revision in context.get("library_versions", {}).items():
            row = conn.execute("SELECT name,revision,deleted FROM libraries WHERE library_id=? AND owner_id=?", (library_id, owner_id)).fetchone()
            if not row or row["deleted"]:
                changes.append("引用的知识库已删除或不可访问；历史来源快照仍保留")
            elif row["revision"] != revision:
                changes.append(f'知识库「{row["name"]}」已有更新；当前作品仍使用生成时的来源版本')
    return changes
