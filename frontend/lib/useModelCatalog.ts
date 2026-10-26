"use client";

import { useCallback, useEffect, useState } from "react";
import { getModelCatalog, type ModelCatalog } from "./api/models";
import { READ_ONLY_DEMO } from "./demo";

export function useModelCatalog() {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const retry = useCallback(() => { setLoading(true); setError(""); setRevision(value => value + 1); }, []);
  useEffect(() => {
    if (READ_ONLY_DEMO) return;
    let cancelled = false;
    void getModelCatalog().then(value => {
      if (!cancelled) setCatalog(value);
    }).catch(e => {
      if (!cancelled) { setCatalog(null); setError(e instanceof Error ? e.message : "模型目录加载失败，请重试"); }
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [revision]);
  return { catalog, loading, error, retry };
}
