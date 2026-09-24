"use client";

import { useEffect, useState } from "react";
import { loadMediaBlob } from "@/lib/media";

function useMediaSrc(sessionId: string, path: string) {
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    loadMediaBlob(sessionId, path)
      .then((u) => {
        if (cancelled) URL.revokeObjectURL(u);
        else {
          url = u;
          setSrc(u);
        }
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [sessionId, path]);

  return { src, error };
}

export function MediaImage({
  sessionId,
  path,
  alt = "产物图片",
}: {
  sessionId: string;
  path: string;
  alt?: string;
}) {
  const { src, error } = useMediaSrc(sessionId, path);
  if (error) return <div className="text-xs text-muted py-3">图片加载失败</div>;
  if (!src) return <div className="text-xs text-muted py-3">加载中…</div>;
  // blob ObjectURL 无法走 next/image 优化，用原生 img
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={src} alt={alt} className="rounded-lg max-w-full" />;
}

export function MediaVideo({ sessionId, path }: { sessionId: string; path: string }) {
  const { src, error } = useMediaSrc(sessionId, path);
  if (error) return <div className="text-xs text-muted py-3">视频加载失败</div>;
  if (!src) return <div className="text-xs text-muted py-3">加载中…</div>;
  return <video src={src} controls className="rounded-lg max-w-full aspect-video bg-black" />;
}
