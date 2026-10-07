"use client";

import Image from "next/image";
import { useRef, useState } from "react";
import { ArrowUpRight, Film, Mic, PersonStanding } from "lucide-react";
import { CreationForm } from "@/features/creation/CreationForm";
import type { PipelineTab } from "@/features/pipelines/PipelinesPanel";
import type { SessionMeta } from "@/lib/api/sessions";
import type { TaskMeta } from "@/lib/api/tasks";
import { PROJECT_STAGES, type ProjectType, type StoryDraft } from "@/lib/workflow";
import { CinemaScene, useReducedMotion } from "./CinemaScene";
import type { CinemaBackground, SceneMode } from "./cinema-scene";
import { SessionLibrary, STAGE_CN } from "./SessionLibrary";

const STORIES = [
  { title: "荒漠史诗", art: "dune", src: "/cinema/references/dune-paul.jpg", alt: "《沙丘2》保罗官方人物海报", tag: "EPIC / CHARACTER", kind: "史诗冒险", credit: "《沙丘2》官方人物海报", source: "https://www.dunepartedos.plataformasdigitaleswb.com/#gallery", width: 1280, height: 640, position: "center center", idea: "一位年轻旅人穿越荒漠，在被遗忘的绿洲中发现一座仍在等待归来者的城市。" },
  { title: "暗夜英雄", art: "batman", src: "/cinema/references/batman.png", alt: "《新蝙蝠侠》官方角色宣传图", tag: "NOIR / HERO", kind: "都市悬疑", credit: "《新蝙蝠侠》官方角色图", source: "https://wwws.warnerbros.co.jp/thebatman-movie/characters/", width: 1495, height: 934, position: "right center", idea: "雨夜的城市停电，一名匿名守夜人循着最后一盏灯，寻找一场失踪事件的真相。" },
  { title: "命运交锋", art: "dialogue", src: "/cinema/references/dune-dialogue.jpg", alt: "《沙丘2》保罗与契妮的双人剧照", tag: "DRAMA / DIALOGUE", kind: "人物对手戏", credit: "《沙丘2》官方剧照", source: "https://www.dunepartedos.plataformasdigitaleswb.com/#gallery", width: 1280, height: 640, position: "center center", idea: "久别重逢的两位旧友在日落前交换一个秘密，却发现彼此正站在同一个抉择的两端。" },
];
const TOOLS = [
  { tab: "literary" as const, title: "文艺短视频", copy: "让文字、音乐与画面一起表达", icon: Film },
  { tab: "motion" as const, title: "角色动作短片", copy: "角色图与动作描述生成 · 当前非动作迁移", icon: PersonStanding },
  { tab: "talking" as const, title: "图片配音口播", copy: "人物图随配音说话 · 静态模式可选", icon: Mic },
];

export function CinemaHome({ onCreated, onPipeline, onSelectSession, onSelectTask, sessions, tasks, sessionsError, onReload, onProjects, focusOnMount, draft, onDraftChange, onKnowledge, projectType, onProjectTypeChange }: {
  onCreated: (session: SessionMeta) => void;
  onPipeline: (tab: PipelineTab, idea: string) => void;
  onSelectSession: (id: string) => void;
  sessions: SessionMeta[] | null;
  tasks: TaskMeta[] | null;
  onSelectTask: (task: TaskMeta) => void;
  draft: StoryDraft;
  onDraftChange: (draft: StoryDraft) => void;
  onKnowledge: () => void;
  projectType: ProjectType;
  onProjectTypeChange: (type: ProjectType) => void;
  sessionsError: string;
  onReload: () => void;
  onProjects: () => void;
  focusOnMount: boolean;
}) {
  const hero = useRef<HTMLElement>(null);
  const idea = draft.idea;
  const setIdea = (idea: string) => onDraftChange({ ...draft, idea });
  const [background, setBackground] = useState<CinemaBackground>("portal");
  const [mode, setMode] = useState<SceneMode | "loading">("loading");
  const reducedMotion = useReducedMotion();
  const motionPaused = reducedMotion;
  const characterIdea = "在雨夜的未来城市，一位女孩寻找丢失的记忆。";
  const worldIdea = "一位旅人穿过云海，来到悬浮在群山之上的未来城市，发现这里仍在等待一位失踪已久的归来者。";
  function chooseStory(index: number) {
    chooseIdea(STORIES[index].idea);
  }
  function chooseIdea(nextIdea: string) {
    setIdea(nextIdea);
    const input = document.getElementById("idea");
    input?.focus({ preventScroll: true });
    if (input && (input.getBoundingClientRect().top < 0 || input.getBoundingClientRect().bottom > window.innerHeight)) {
      input.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
    }
  }

  return <>
    <section className={"hero" + (motionPaused ? " paused" : "") + (projectType === "comic" ? " hero-comic" : "")} ref={hero} data-render={mode} data-paused={motionPaused} aria-labelledby="hero-title">
      <CinemaScene host={hero} background={background} paused={motionPaused} mode={mode} onMode={setMode} />
      <div className="hero-shade" /><div className="hero-noise" />
      <div className="hero-copy">
        <p className="eyebrow">YOUR AI FILMMAKING STUDIO</p>
        <h1 id="hero-title">让想象，<br /><span>{projectType === "comic" ? "成为下一部漫剧。" : "成为下一部电影。"}</span></h1>
        <p className="hero-description">{projectType === "comic" ? <>漫画镜头、角色对白与配音，<br />用基础运镜和字幕，把故事变成漫剧视频。</> : <>一句话打开故事。从剧本、角色和分镜开始，<br />在一个创作空间里，把想象带到银幕。</>}</p>
        <CreationForm key={projectType} variant="cinema" projectType={projectType} onProjectTypeChange={onProjectTypeChange} draft={draft} onDraftChange={onDraftChange} onCreated={onCreated} focusOnMount={focusOnMount} onKnowledge={onKnowledge} />
        <div className="examples"><span>灵感起点</span>{STORIES.map((story, index) => <button key={story.art} onClick={() => chooseStory(index)}>{story.kind} ↗</button>)}</div>
      </div>
      <div className="floating-assets" aria-label="人物与场景创作灵感">
        <button className="portrait" onClick={() => chooseIdea(characterIdea)} aria-label="以雨夜女孩为创作灵感">
          <Image src="/cinema/character.png" alt="雨夜城市中的女孩人物概念图" width={1086} height={1448} unoptimized />
          <span className="asset-label">雨夜女孩 / 人物灵感</span>
        </button>
        <button className="film-card" onClick={() => chooseIdea(worldIdea)} aria-label="以云上城市为创作灵感">
          <Image src="/cinema/world.png" alt="云海与群山之上的未来城市概念图" width={1672} height={941} style={{ objectPosition: "center 35%" }} unoptimized />
          <span className="film-caption"><span>云上城市 / 场景灵感</span><small>从一个世界开始 ↗</small></span>
        </button>
      </div>
      <div className="hero-bottom">
        <div className="workflow-mini" aria-label={projectType === "comic" ? "漫剧视频流程" : "故事短视频流程"}>{PROJECT_STAGES[projectType].map((stage, i) => <span key={stage}>{i > 0 && <i>→</i>}{STAGE_CN[stage]}</span>)}</div>
        <span className="film-signature">IMAGINATION, ON SCREEN.</span>
        <div className="scene-controls">
          <div className="scene-switch" aria-label="切换梦境场景">
            <button className={background === "portal" ? "active" : ""} onClick={() => setBackground("portal")} aria-pressed={background === "portal"}>夜海之门</button>
            <button className={background === "world" ? "active" : ""} onClick={() => setBackground("world")} aria-pressed={background === "world"}>云上之城</button>
          </div>
        </div>
      </div>
    </section>
    <section className="section" aria-labelledby="inspiration-title">
      <div className="section-heading"><h2 id="inspiration-title">电影灵感参考</h2></div>
      <div className="projects">{STORIES.map((story, i) => <article key={story.art} className="inspiration-card"><button className="project" onClick={() => chooseStory(i)}>
        <div className={`project-picture reference-${story.art}`}>
          <Image src={story.src} alt={story.alt} width={story.width} height={story.height} unoptimized /><span className="tag">{story.tag}</span>
        </div>
        <div className="project-info"><div><h3>{story.title}</h3><p>{story.kind}</p></div><ArrowUpRight size={14} /></div>
      </button></article>)}</div>
    </section>
    {projectType === "story" && <div className="tools" aria-label="短视频快捷工具">{TOOLS.map(tool => <button key={tool.tab} className="tool" onClick={() => onPipeline(tool.tab, idea)}>
      <span className="icon"><tool.icon size={17} /></span><div><h3>{tool.title}</h3><p>{tool.copy}</p></div><ArrowUpRight className="arrow" size={13} />
    </button>)}</div>}
    <section className="section" aria-label="我的作品">
      <div className="section-heading"><h2>我的作品</h2><button className="text-link" onClick={onProjects}>查看全部 ↗</button></div>
      <SessionLibrary sessions={sessions} tasks={tasks} error={sessionsError} onReload={onReload} onSelect={onSelectSession} onSelectTask={onSelectTask} compact />
    </section>
  </>;
}
