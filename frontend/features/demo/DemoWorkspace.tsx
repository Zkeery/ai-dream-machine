"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { CreationPanel } from "@/features/creation/CreationPanel";
import { SessionLibrary } from "@/features/cinema/SessionLibrary";
import { type SessionMeta } from "@/lib/api/sessions";
import { loadDemoSessions } from "./loadSessions";
import { DEMO_MESSAGE, showDemoNotice } from "@/lib/demo";
import { MediaImage } from "@/features/creation/media";

// Capture before child handlers, including disabled generation buttons. The
// independent server also rejects every write; this is only the friendly UX.
const MUTATION = /新建|删除|编辑|修改提示词|重新生成|重新检查|继续未完成|确认.*进入|确认完成|生成本|生成视频|生成全部|保存|替换|应用|使用此|采用|选用/;

export function DemoWorkspace() {
  const router = useRouter(), params = useSearchParams();
  const [sessions, setSessions] = useState<SessionMeta[] | null>(null);
  const [failed, setFailed] = useState(false), [notice, setNotice] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const dialog = useRef<HTMLDialogElement>(null);
  const sid = params.get("session");
  const selected = sessions?.find(s => s.session_id === sid);
  const complete = sessions?.find(s => s.status === "session_completed");
  const cover = (complete?.artifacts.reference_generation as {shots?: {selected?: string; path?: string}[]})?.shots?.[0];
  const open = (id: string) => router.push(`/?session=${encodeURIComponent(id)}`);
  useEffect(() => {
    const controller = new AbortController();
    loadDemoSessions(controller.signal)
      .then(value => { if (!controller.signal.aborted) setSessions(value); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [loadAttempt]);
  useEffect(() => {
    const show = () => setNotice(true);
    const capture = (event: Event) => {
      const target = event.target instanceof Element ? event.target : null;
      const button = target?.closest("button");
      if (button && MUTATION.test(`${button.textContent} ${button.getAttribute("aria-label") ?? ""}`)) {
        event.preventDefault(); event.stopPropagation(); show();
      }
    };
    window.addEventListener("dream-demo-notice", show);
    document.addEventListener("click", capture, true);
    document.addEventListener("pointerdown", capture, true);
    return () => {
      window.removeEventListener("dream-demo-notice", show);
      document.removeEventListener("click", capture, true);
      document.removeEventListener("pointerdown", capture, true);
    };
  }, []);
  useEffect(() => { if (notice) dialog.current?.showModal(); else dialog.current?.close(); }, [notice]);
  const library = <SessionLibrary sessions={sessions} error={failed ? "连接暂未恢复，请稍后重试。" : ""} onReload={() => { setFailed(false); setLoadAttempt(value => value + 1); }} onSelect={open} selectedId={sid ?? undefined} />;
  return <div className="night-studio public-demo"><div className="shell">
    <header className="header">
      <button className="brand" onClick={() => router.push("/")} aria-label="AI造梦机首页"><span className="brand-name">AI造梦机</span><span className="brand-en">DREAM MACHINE</span></button>
      <nav className="nav" aria-label="主导航"><button className="active" onClick={() => router.push("/")}>示例作品</button><button onClick={showDemoNotice}>新建</button></nav>
      <span className="demo-badge">只读演示</span>
    </header>
    <main>
      {!selected ? <section className="studio-section">
        <div className="demo-hero"><div><p className="section-kicker">FROM A STORY TO A FILM</p><h1>从一句想象，<br />到一部电影。</h1><p className="demo-intro">走进《雨夜归途》，看一个故事如何经过剧本、分镜与内容检查，成为完整短片。</p><div className="demo-hero-actions"><button className="cinema-primary" onClick={() => complete && open(complete.session_id)}>观看示例作品 ↗</button><span>无需登录 · 自由浏览 · 示例内容只读</span></div></div>{complete && cover && <button className="demo-cover" onClick={() => open(complete.session_id)} aria-label="观看雨夜归途示例"><MediaImage sessionId={complete.session_id} path={cover.selected || cover.path || ""} alt="雨夜归途：灯街上的墨豆" /><span>雨夜归途 <small>进入创作流程 ↗</small></span></button>}</div>
        <div className="studio-section-header"><div><p className="section-kicker">THE CREATIVE PROCESS</p><h2>两份作品，一段完整创作流程。</h2><p>查看剧本与分镜、检查记录、阶段版本，以及中断时保留的素材。</p></div><button className="text-link" onClick={showDemoNotice}>＋ 新建作品</button></div>
        {library}
      </section> : <section className="studio-project">
        <div className="studio-section-header"><div><p className="section-kicker">STORY / WORKSPACE</p><h1>{(selected.artifacts.script_generation as {title?: string})?.title ?? "示例作品"}</h1></div><button className="text-link" onClick={() => router.push("/")}>返回示例作品 ↗</button></div>
        <div className="studio-work-grid"><aside className="studio-panel session-sidebar"><h2>示例作品</h2>{library}</aside><div className="studio-panel stage-workspace"><CreationPanel key={selected.session_id} sessionId={selected.session_id} userId="public-demo" onCreated={() => {}} onChanged={() => {}} /></div></div>
      </section>}
    </main>
    <footer className="page-footer"><span>AI造梦机 · 从一句想象，到一部电影。</span><span>示例作品只读展示</span></footer>
    <dialog ref={dialog} className="demo-dialog" onCancel={() => setNotice(false)} onClose={() => setNotice(false)} aria-labelledby="demo-notice-title"><p className="section-kicker">AI造梦机 · 只读演示</p><h2 id="demo-notice-title">{DEMO_MESSAGE}</h2><p>你可以浏览剧本、分镜、检查记录、恢复状态、版本和成片。</p><button className="cinema-primary" onClick={() => setNotice(false)}>继续浏览</button></dialog>
  </div></div>;
}
