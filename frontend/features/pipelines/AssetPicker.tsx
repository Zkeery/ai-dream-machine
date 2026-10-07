"use client";

import { useEffect, useState } from "react";
import { listAssets, loadAssetImage, reuseAsset, type AssetItem } from "@/lib/api/assets";

function AssetThumbnail({ asset }: { asset: AssetItem }) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    let cancelled = false, url = "";
    loadAssetImage(asset.asset_id).then(value => { if (cancelled) URL.revokeObjectURL(value); else { url = value; setSrc(value); } }).catch(() => {});
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [asset.asset_id]);
  // Generated account-owned images require authenticated blob URLs.
  // eslint-disable-next-line @next/next/no-img-element
  return src ? <img src={src} alt={asset.name} /> : <div className="asset-placeholder">图片加载中</div>;
}

export function AssetPicker({ onSelected, disabled }: { onSelected: (filename: string, name: string, assetId: string) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [assets, setAssets] = useState<AssetItem[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function show() {
    setOpen(true); setError("");
    try { setAssets(await listAssets()); } catch (e) { setError(e instanceof Error ? e.message : "素材加载失败"); }
  }
  async function choose(asset: AssetItem) {
    setBusy(true); setError("");
    try { const saved = await reuseAsset(asset.asset_id); onSelected(saved.filename, saved.original_name || asset.name, asset.asset_id); setOpen(false); }
    catch (e) { setError(e instanceof Error ? e.message : "素材不可用"); }
    finally { setBusy(false); }
  }
  return <div className="asset-picker">
    <button className="text-link" type="button" disabled={disabled || busy} onClick={() => void show()}>从我的素材选择 ↗</button>
    {open && <section className="asset-picker-panel"><div className="flex justify-between gap-3 mb-3"><h3>我的生成素材</h3><button className="text-link" onClick={() => setOpen(false)}>收起</button></div>
      {error && <p className="text-sm text-danger" role="alert">{error}</p>}
      {assets === null ? <p className="text-sm text-muted">加载素材…</p> : !assets.length ? <p className="text-sm text-muted">还没有生成图片。故事工作区生成的角色、场景与参考图会出现在这里。</p> : <div className="asset-picker-grid">{assets.map(asset => <button key={asset.asset_id} type="button" disabled={busy || disabled} onClick={() => void choose(asset)}><AssetThumbnail asset={asset} /><strong>{asset.name}</strong><small>{asset.source_title}{asset.stale ? " · 历史素材" : ""}</small></button>)}</div>}
    </section>}
  </div>;
}
