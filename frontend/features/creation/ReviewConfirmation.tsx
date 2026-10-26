"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import type { PendingReview } from "@/lib/api/sessions";
import { MediaImage } from "./media";

export function ReviewConfirmation({ sessionId, review, busy, onDecision }: {
  sessionId: string;
  review: PendingReview | null | undefined;
  busy: boolean;
  onDecision: (accepted: boolean) => Promise<void>;
}) {
  const [loadedImage, setLoadedImage] = useState<string | null>(null);
  const imageKey = `${review?.review_id ?? ""}:${review?.image_url ?? ""}`;
  const ready = Boolean(review?.review_id?.trim() && review.execution_id?.trim()
    && review.image_url?.trim() && review.status === "pending");
  const imageLoaded = loadedImage === imageKey;
  return <section className="rounded-lg border border-primary/40 bg-surface-2 p-4 space-y-4" aria-label="图片人工确认">
    <div className="space-y-2">
      <h3 className="text-base font-semibold">请确认这张图片</h3>
      <p className="text-sm text-muted">生成已暂停。请查看图片和审查理由，再决定是否继续。</p>
    </div>
    {review?.image_url ? <MediaImage key={imageKey} sessionId={sessionId} path={review.image_url} alt="等待人工确认的原图" onLoad={() => setLoadedImage(imageKey)} />
      : <p className="text-sm text-danger" role="alert">待确认图片未加载，请重新加载作品。</p>}
    <div className="space-y-2">
      <h4 className="text-sm font-semibold">审查理由</h4>
      <p className="text-sm whitespace-pre-wrap break-words">{review?.reason?.trim() ? review.reason : "未给出理由"}</p>
    </div>
    {!ready && <p className="text-sm text-danger" role="alert">待确认记录不完整，暂不能提交决定。</p>}
    <div className="flex gap-3 flex-wrap">
      <Button disabled={busy || !ready || !imageLoaded} onClick={() => { if (ready && imageLoaded && !busy) void onDecision(true); }}>符合，继续</Button>
      <Button variant="secondary" disabled={busy || !ready} onClick={() => { if (ready && !busy) void onDecision(false); }}>不符合，停止</Button>
    </div>
    {busy && <p className="text-sm text-muted" role="status">正在提交确认…</p>}
  </section>;
}
