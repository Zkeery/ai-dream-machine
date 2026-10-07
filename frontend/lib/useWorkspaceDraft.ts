"use client";

import { useCallback, useRef, useState } from "react";
import { emptyDraft, readDraft, draftKey, type WorkspaceDraft } from "./workflow";

export function useWorkspaceDraft(userId: string) {
  const [draft, setDraft] = useState<WorkspaceDraft>(() => {
    try { return typeof window === "undefined" ? emptyDraft() : readDraft(window.localStorage, userId); }
    catch { return emptyDraft(); }
  });
  const [storageError, setStorageError] = useState("");
  const currentDraft = useRef(draft);
  const updateDraft = useCallback((update: (current: WorkspaceDraft) => WorkspaceDraft) => {
    const next = update(currentDraft.current);
    currentDraft.current = next;
    setDraft(next);
    try { window.localStorage.setItem(draftKey(userId), JSON.stringify(next)); setStorageError(""); }
    catch { setStorageError("浏览器无法保存草稿，当前内容仅在本页保留。请检查存储权限。"); }
  }, [userId]);
  return { draft, updateDraft, storageError };
}
