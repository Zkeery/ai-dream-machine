"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, SlidersHorizontal } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { createSession, type SessionMeta } from "@/lib/api/sessions";
import { creationInput, creationModelKeys, modelSelectionError, videoFormatError, resolvedModelSelection, PROJECT_LABELS, type ProjectType, type StoryDraft } from "@/lib/workflow";
import { KnowledgeLibraryPicker } from "@/features/knowledge/KnowledgeLibraryPicker";
import { useModelCatalog } from "@/lib/useModelCatalog";
import type { ModelSelection, ModelKey } from "@/lib/api/models";
import { ModelSelector } from "./ModelSelector";
import { ContentBoundary } from "./ContentBoundary";
import { useTrial } from "@/features/trial/TrialProvider";
import { recordTrialEvent, TRIAL_BLOCK_MESSAGE } from "@/lib/api/trial";

const inputCls =
  "w-full px-3 py-2 rounded-md bg-surface-2 border border-border text-foreground text-sm placeholder:text-muted focus:outline-none focus:border-primary";

export function CreationForm({
  onCreated,
  variant = "standard",
  idea: controlledIdea,
  onIdeaChange,
  focusOnMount = false,
  draft,
  onDraftChange,
  onKnowledge,
  projectType = "story",
  onProjectTypeChange,
}: {
  onCreated: (s: SessionMeta) => void;
  variant?: "standard" | "cinema";
  idea?: string;
  onIdeaChange?: (idea: string) => void;
  onPipeline?: (tab: "literary" | "motion" | "talking", idea: string) => void;
  focusOnMount?: boolean;
  draft?: StoryDraft;
  onDraftChange?: (draft: StoryDraft) => void;
  onKnowledge?: () => void;
  projectType?: ProjectType;
  onProjectTypeChange?: (type: ProjectType) => void;
}) {
  const [storedIdea, setStoredIdea] = useState("");
  const idea = draft?.idea ?? controlledIdea ?? storedIdea;
  const setIdea = (value: string) => {
    if (draft && onDraftChange) onDraftChange({ ...draft, idea: value });
    else if (onIdeaChange) onIdeaChange(value);
    else setStoredIdea(value);
  };
  const settings = useRef<HTMLDetailsElement>(null);
  const [storedStyle, setStoredStyle] = useState("realistic");
  const [storedRatio, setStoredRatio] = useState("16:9");
  const [submitting, setSubmitting] = useState(false);
  const trial = useTrial();
  const realTrial = trial.status?.mode === "real";
  const orchestrationMode = realTrial ? "workflow" as const : "multi_agent" as const;
  const [storedKnowledgeIds, setStoredKnowledgeIds] = useState<string[]>([]);
  const knowledgeIds = draft?.knowledgeLibraryIds ?? storedKnowledgeIds;
  const setKnowledgeIds = (knowledgeLibraryIds: string[]) => draft && onDraftChange ? onDraftChange({ ...draft, knowledgeLibraryIds }) : setStoredKnowledgeIds(knowledgeLibraryIds);
  const style = draft?.style ?? storedStyle;
  const episodes = 1;
  const ratio = draft?.ratio ?? storedRatio;
  const setStyle = (value: string) => draft && onDraftChange ? onDraftChange({ ...draft, style: value }) : setStoredStyle(value);
  const setEpisodes = () => {};
  const setRatio = (value: string) => draft && onDraftChange ? onDraftChange({ ...draft, ratio: value }) : setStoredRatio(value);
  const [error, setError] = useState("");
  const [storedModels, setStoredModels] = useState<ModelSelection>({});
  const models = useModelCatalog();
  const modelKeys: ModelKey[] = realTrial ? ["text", "image", "video_reference"] : creationModelKeys(projectType);
  const selection = resolvedModelSelection(draft?.modelSelection ?? storedModels, models.catalog, modelKeys);
  const modelError = modelSelectionError(selection, models.catalog, modelKeys) || videoFormatError(selection, models.catalog, modelKeys, ratio, "720P");
  const modelsReady = !models.loading && !models.error && !modelError;
  const setModels = (modelSelection: ModelSelection) => draft && onDraftChange ? onDraftChange({ ...draft, modelSelection: { ...draft.modelSelection, ...modelSelection } }) : setStoredModels(modelSelection);
  const quotaBlocked = trial.status?.generation_enabled === false || trial.status?.quota_remaining === 0;
  const estimatedCny = trial.status?.estimates?.[projectType === "comic" ? "comic" : "three_shot"] ?? trial.status?.estimated_charge_cny;
  const estimateRef = useRef<HTMLParagraphElement>(null);
  const displayedEstimates = useRef(new Set<string>());
  const estimateText = typeof estimatedCny === "number"
    ? trial.status?.mode === "mock" ? `本地 mock 模拟预估 ¥${estimatedCny}，实际不扣费（非真实报价）。` : `本次费用预留 ¥${estimatedCny}，实际费用待平台账单核对。`
    : "本次预估待服务端确认。";
  useEffect(() => {
    const element = estimateRef.current;
    if (!element || typeof estimatedCny !== "number") return;
    const key = `${projectType}:${estimatedCny}`;
    const observer = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting) || displayedEstimates.current.has(key)) return;
      displayedEstimates.current.add(key);
      void recordTrialEvent("estimate_shown", { estimated_cny: estimatedCny, mode: trial.status?.mode, project_type: projectType }).catch(error => {
        setError(error instanceof Error ? error.message : "费用说明未记录，请手动重新打开本页。");
      });
    }, { threshold: 0.1 });
    observer.observe(element);
    return () => observer.disconnect();
  }, [estimatedCny, projectType, trial.status?.mode]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    if (quotaBlocked) { setError(trial.status?.blocked_message || TRIAL_BLOCK_MESSAGE); return; }
    if (!modelsReady) { setError(models.error || modelError || "模型目录加载中，请稍候。"); return; }
    if (!idea.trim()) {
      setError("请输入创意");
      return;
    }
    if (!style.trim() || !Number.isInteger(episodes) || episodes < 1 || episodes > 20) {
      setError("请填写视觉风格，剧集数需为 1–20 的整数。");
      if (settings.current) settings.current.open = true;
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      setModels(selection);
      await recordTrialEvent("create_start", { project_type: projectType });
      const input = creationInput({ idea, style, episodes, ratio, knowledgeLibraryIds: knowledgeIds, modelSelection: selection, orchestrationMode }, projectType);
      const s = await createSession(realTrial ? { ...input, resolution: "720P", video_generation_mode: "reference", expand_idea: false } : input);
      void trial.refresh().catch(() => {});
      onCreated(s);
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  if (variant === "cinema") {
    return (
      <form onSubmit={handleSubmit} className="composer" noValidate>
        <div className="creation-kind-switch" aria-label="选择创作项目类型">
          {(["story", "comic"] as const).map(type => <button type="button" key={type} disabled={submitting} className={projectType === type ? "active" : ""} aria-pressed={projectType === type} onClick={() => onProjectTypeChange?.(type)}><span>{type === "story" ? "3 镜短视频" : PROJECT_LABELS[type]}</span><small>{type === "story" ? "单集 · 3 个镜头" : "单集 · 对白配音"}</small></button>)}
        </div>
        <label htmlFor="idea" className="sr-only">创作创意</label>
        <textarea
          id="idea"
          value={idea}
          onChange={(e) => { setIdea(e.target.value); setError(""); }}
          placeholder={projectType === "comic" ? "雨夜的车站，一位少女遇见了十年前的自己，她开口说……" : "一位旅人穿过光之门，来到云海之上的城市……"}
          maxLength={3000}
          autoFocus={focusOnMount}
          aria-invalid={Boolean(error)}
          aria-describedby={error ? "creation-error" : undefined}
        />
        {(error || modelError) && <p id="creation-error" className="composer-error" role="alert">{error || modelError}</p>}
        <KnowledgeLibraryPicker selected={knowledgeIds} onChange={setKnowledgeIds} disabled={submitting} onManage={onKnowledge} />
        <ModelSelector {...models} keys={modelKeys} selected={selection} compact disabled={submitting} onChange={setModels} onRetry={models.retry} />
        <ContentBoundary />
        {realTrial && <p className="text-xs text-muted">固定规格：720P，3个镜头，每镜5秒，参考图视频。</p>}
        <p ref={estimateRef} className="text-xs text-muted">{estimateText} 内测每码只能生成 1 次；单次预估超过 ¥{trial.status?.max_estimate_cny ?? 12} 将被拒绝。</p>
        {quotaBlocked && <p className="composer-error" role="status">{trial.status?.blocked_message || TRIAL_BLOCK_MESSAGE}<button className="text-link ml-3" type="button" onClick={trial.openSurvey}>填写问卷</button></p>}
        <div className="composer-bottom">
          <details ref={settings} className="creation-settings">
            <summary><SlidersHorizontal size={13} />创作设置<span>{ratio} · {episodes} 集</span></summary>
            <div className="creation-settings-panel">
              <div>
                <label htmlFor="style">视觉风格</label>
                <Input id="style" value={style} onChange={(e) => setStyle(e.target.value)} placeholder="realistic / anime" />
              </div>
              <div>
                <label htmlFor="episodes">内测单集</label>
                <Input id="episodes" type="number" min={1} max={1} disabled value={episodes} onChange={setEpisodes} />
              </div>
              <div>
                <label htmlFor="ratio">视频比例</label>
                <select id="ratio" value={ratio} onChange={(e) => setRatio(e.target.value)}>
                  <option value="16:9">16:9</option><option value="9:16">9:16</option><option value="1:1">1:1</option>
                </select>
              </div>
            </div>
          </details>
          <button className="cinema-primary" type="submit" disabled={submitting || !modelsReady || quotaBlocked}>
            {submitting ? "创建中…" : projectType === "comic" ? "开始漫剧创作" : "开始故事创作"}<ArrowUpRight size={14} />
          </button>
        </div>
      </form>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 max-w-xl">
      <div>
        <h2 className="text-base font-semibold text-foreground mb-3">{PROJECT_LABELS[projectType]}</h2>
        <label htmlFor="idea" className="block text-sm text-muted mb-1">
          创作创意
        </label>
        <textarea
          id="idea"
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          placeholder="例：一只流浪猫在雨夜被好心人收留"
          rows={4}
          autoFocus
          className={`${inputCls} resize-y`}
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div>
          <label htmlFor="style" className="block text-sm text-muted mb-1">
            视觉风格
          </label>
          <Input
            id="style"
            value={style}
            onChange={(e) => setStyle(e.target.value)}
            placeholder="realistic / anime"
          />
        </div>
        <div>
          <label htmlFor="episodes" className="block text-sm text-muted mb-1">
            内测单集
          </label>
          <Input
            id="episodes"
            type="number"
            min={1}
            max={1}
            disabled
            value={episodes}
            onChange={setEpisodes}
          />
        </div>
        <div>
          <label htmlFor="ratio" className="block text-sm text-muted mb-1">
            视频比例
          </label>
          <select
            id="ratio"
            value={ratio}
            onChange={(e) => setRatio(e.target.value)}
            className={inputCls}
          >
            <option value="16:9">16:9</option>
            <option value="9:16">9:16</option>
            <option value="1:1">1:1</option>
          </select>
        </div>
      </div>

      <KnowledgeLibraryPicker selected={knowledgeIds} onChange={setKnowledgeIds} disabled={submitting} onManage={onKnowledge} />
      <ModelSelector {...models} keys={modelKeys} selected={selection} disabled={submitting} onChange={setModels} onRetry={models.retry} />
      <ContentBoundary />
      <p ref={estimateRef} className="text-xs text-muted">{estimateText} 内测每码只能生成 1 次；单次预估超过 ¥{trial.status?.max_estimate_cny ?? 12} 将被拒绝。</p>
      {realTrial && <p className="text-xs text-muted">固定规格：720P，3个镜头，每镜5秒，参考图视频。</p>}
      {quotaBlocked && <p className="text-sm text-danger" role="status">{trial.status?.blocked_message || TRIAL_BLOCK_MESSAGE}<button className="text-link ml-3" type="button" onClick={trial.openSurvey}>填写问卷</button></p>}
      {(error || modelError) && <p className="text-sm text-danger" role="alert">{error || modelError}</p>}

      <Button type="submit" disabled={submitting || !modelsReady || quotaBlocked}>
        {submitting ? "创建中…" : "开始创作"}
      </Button>
    </form>
  );
}
