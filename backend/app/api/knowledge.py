"""Authenticated knowledge library CRUD and retrieval preview."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.core.errors import AppError
from app.schemas.knowledge import Category, DocumentUpdate, LibraryCreate, LibraryUpdate, SearchRequest
from app.services import knowledge_retrieval, knowledge_store

router = APIRouter(prefix="/knowledge")


@router.get("/libraries")
def libraries(owner_id: str = Depends(get_current_user)):
    return knowledge_store.list_libraries(owner_id)


@router.post("/libraries", status_code=201)
def create_library(request: LibraryCreate, owner_id: str = Depends(get_current_user)):
    return knowledge_store.create_library(owner_id, request.name, request.description)


@router.patch("/libraries/{library_id}")
def update_library(library_id: str, request: LibraryUpdate, owner_id: str = Depends(get_current_user)):
    return knowledge_store.update_library(owner_id, library_id, request.model_dump(exclude_unset=True))


@router.delete("/libraries/{library_id}")
def delete_library(library_id: str, owner_id: str = Depends(get_current_user)):
    knowledge_store.delete_library(owner_id, library_id)
    return {"deleted": True}


@router.get("/libraries/{library_id}/documents")
def documents(library_id: str, owner_id: str = Depends(get_current_user)):
    return knowledge_store.list_documents(owner_id, library_id)


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    try:
        content = await file.read(knowledge_store.MAX_UPLOAD_BYTES + 1)
        if len(content) > knowledge_store.MAX_UPLOAD_BYTES:
            raise AppError("KNOWLEDGE_FILE_SIZE", "每份资料不能超过 5 MB", 422)
        return file.filename or "", content
    finally:
        await file.close()


@router.post("/libraries/{library_id}/documents", status_code=201)
async def upload_document(library_id: str, file: UploadFile = File(...), title: str | None = Form(None),
                          category: Category = Form("other"), is_constraint: bool = Form(False),
                          owner_id: str = Depends(get_current_user)):
    # Validate ownership before parsing/embedding an uploaded document.
    knowledge_store.validate_libraries(owner_id, [library_id])
    filename, content = await _read_upload(file)
    return await run_in_threadpool(knowledge_store.save_document, owner_id, library_id, filename, content, title, category, is_constraint)


@router.get("/documents/{document_id}")
def document(document_id: str, owner_id: str = Depends(get_current_user)):
    return knowledge_store.get_document(owner_id, document_id)


@router.get("/documents/{document_id}/versions/{version_id}")
def version(document_id: str, version_id: str, owner_id: str = Depends(get_current_user)):
    return knowledge_store.get_version(owner_id, document_id, version_id)


@router.post("/documents/{document_id}/versions", status_code=201)
async def replace_document(document_id: str, file: UploadFile = File(...), title: str | None = Form(None),
                           category: Category | None = Form(None), is_constraint: bool | None = Form(None),
                           owner_id: str = Depends(get_current_user)):
    knowledge_store.get_document(owner_id, document_id)
    filename, content = await _read_upload(file)
    return await run_in_threadpool(knowledge_store.replace_document, owner_id, document_id, filename, content, title, category, is_constraint)


@router.patch("/documents/{document_id}")
def update_document(document_id: str, request: DocumentUpdate, owner_id: str = Depends(get_current_user)):
    return knowledge_store.update_document(owner_id, document_id, request.model_dump(exclude_unset=True))


@router.delete("/documents/{document_id}")
def delete_document(document_id: str, owner_id: str = Depends(get_current_user)):
    knowledge_store.delete_document(owner_id, document_id)
    return {"deleted": True}


@router.post("/libraries/{library_id}/search")
def search(library_id: str, request: SearchRequest, owner_id: str = Depends(get_current_user)):
    return knowledge_retrieval.build_context(owner_id, [library_id], request.query, request.limit)
