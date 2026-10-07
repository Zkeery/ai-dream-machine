"use client";

import { useEffect, useState } from "react";
import { loadMediaBlob } from "@/lib/media";

function useMediaSrc(sessionId: string, path: string) {
  const [result, setResult] = useState<{ sessionId: string; path: string; src: string | null; error: boolean } | null>(null);

  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    loadMediaBlob(sessionId, path)
      .then((u) => {
        if (cancelled) URL.revokeObjectURL(u);
        else {
          url = u;
          setResult({ sessionId, path, src: u, error: false });
        }
      })
      .catch(() => {
        if (!cancelled) setResult({ sessionId, path, src: null, error: true });
      });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [sessionId, path]);

  // A new selection starts loading immediately; prior media and errors belong to their own path.
  return result?.sessionId === sessionId && result.path === path
    ? { src: result.src, error: result.error }
    : { src: null, error: false };
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

export function MediaAudio({ sessionId, path, label }: { sessionId: string; path: string; label: string }) {
  const { src, error } = useMediaSrc(sessionId, path);
  if (error) return <p className="text-xs text-danger" role="alert">配音加载失败，请重新打开当前阶段。</p>;
  if (!src) return <p className="text-xs text-muted" role="status">加载配音…</p>;
  return <audio src={src} controls preload="metadata" aria-label={label} className="comic-audio-player" />;
}
