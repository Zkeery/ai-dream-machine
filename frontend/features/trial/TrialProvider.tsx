"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { useAuth } from "@/features/auth/AuthProvider";
import { acceptTrialConsent, getTrialStatus, recordTrialEvent, requestTrialDeletion, submitTrialSurvey, TRIAL_BLOCK_MESSAGE, type SurveyFields, type TrialStatus } from "@/lib/api/trial";

type TrialContextValue = {
  status: TrialStatus | null;
  refresh: () => Promise<void>;
  generationCompleted: (taskId: string) => void;
  openSurvey: () => void;
};

const TrialContext = createContext<TrialContextValue | null>(null);
const fieldClass = "w-full px-3 py-2 rounded-md bg-surface-2 border border-border text-foreground text-sm";

export function useTrial() {
  const value = useContext(TrialContext);
  if (!value) throw new Error("useTrial 必须在 TrialProvider 内使用");
  return value;
}

export function TrialProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  return <AccountTrialProvider key={user?.user_id ?? "signed-out"}>{children}</AccountTrialProvider>;
}

function AccountTrialProvider({ children }: { children: React.ReactNode }) {
  const { user, loading, logout } = useAuth();
  const [status, setStatus] = useState<TrialStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [consentChecked, setConsentChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [paywallTaskId, setPaywallTaskId] = useState<string | null>(null);
  const [surveyOpen, setSurveyOpen] = useState(false);
  const [selectedPlan, setSelectedPlan] = useState(() => {
    try { return localStorage.getItem(`dream-machine:trial-plan:${user?.user_id}`) ?? ""; } catch { return ""; }
  });
  const [paywallClicked, setPaywallClicked] = useState(Boolean(selectedPlan));
  const [surveyError, setSurveyError] = useState("");
  const completedSeen = useRef(new Set<string>());
  const userId = user?.user_id;

  const refresh = useCallback(async () => {
    if (!userId) return;
    try {
      const next = await getTrialStatus();
      setStatus(next); setStatusError("");
    } catch (error) {
      setStatusError(error instanceof Error ? error.message : "内测状态读取失败");
      throw error;
    }
  }, [userId]);

  useEffect(() => {
    let cancelled = false;
    if (userId) {
      getTrialStatus().then(next => {
        if (!cancelled) { setStatus(next); setStatusError(""); }
      }).catch(error => {
        if (!cancelled) setStatusError(error instanceof Error ? error.message : "内测状态读取失败");
      });
    }
    return () => { cancelled = true; };
  }, [userId]);

  const generationCompleted = useCallback((taskId: string) => {
    if (completedSeen.current.has(taskId)) return;
    completedSeen.current.add(taskId);
    try {
      const storageKey = `dream-machine:trial-paywall:${userId}:${taskId}`;
      if (localStorage.getItem(storageKey) === "shown") return;
      localStorage.setItem(storageKey, "shown");
    } catch { /* Only suppress repeat display when browser storage is available. */ }
    setNotice(""); setPaywallTaskId(taskId);
    void refresh().catch(() => {});
  }, [userId, refresh]);

  async function acceptConsent() {
    if (!consentChecked || busy) return;
    setBusy(true); setNotice("");
    try { await acceptTrialConsent(); await refresh(); }
    catch (error) { setNotice(error instanceof Error ? error.message : "同意未保存，请稍后手动再试"); }
    finally { setBusy(false); }
  }

  async function choosePlan(planId: string) {
    if (busy) return;
    setBusy(true); setNotice("内测免费，没有扣费，谢谢你的选择");
    try {
      await recordTrialEvent("paywall_click", { plan_id: planId, task_id: paywallTaskId });
      setSelectedPlan(planId); setPaywallClicked(true);
      try { localStorage.setItem(`dream-machine:trial-plan:${userId}`, planId); } catch { /* Server event is authoritative. */ }
      setNotice("内测免费，没有扣费，谢谢你的选择");
    } catch (error) { setNotice(error instanceof Error ? error.message : "选择未记录，请稍后手动再试；没有扣费"); }
    finally { setBusy(false); }
  }

  async function requestDeletion() {
    if (busy) return;
    setBusy(true); setNotice("");
    try {
      const result = await requestTrialDeletion();
      setNotice(`删除申请已记录${result.request_id ? `（${result.request_id}）` : ""}，项目负责人会处理。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : "删除申请未记录，请稍后手动再试"); }
    finally { setBusy(false); }
  }

  async function submitSurvey(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const data = new FormData(event.currentTarget);
    const stringValue = (name: string) => String(data.get(name) ?? "");
    const scoreValue = (name: string) => Number(stringValue(name));
    const fields: SurveyFields = {
      participant_code: stringValue("participant_code"), intent_raw: stringValue("intent_raw"),
      independent_completion: stringValue("independent_completion") === "yes" ? "yes" : "no",
      blocker_raw: stringValue("blocker_raw"), need_fit: scoreValue("need_fit"),
      character_consistency: scoreValue("character_consistency"), visual_quality: scoreValue("visual_quality"),
      camera_motion: scoreValue("camera_motion"), audio_subtitles: stringValue("audio_subtitles") === "n/a" ? "n/a" : scoreValue("audio_subtitles"),
      satisfaction: scoreValue("satisfaction"), recommendation: scoreValue("recommendation"),
      paywall_clicked: paywallClicked, selected_plan: selectedPlan,
      willing_monthly_raw: stringValue("willing_monthly_raw"), no_pay_reason_raw: stringValue("no_pay_reason_raw"), other_raw: stringValue("other_raw"),
    };
    setBusy(true); setSurveyError("");
    try {
      await submitTrialSurvey(fields);
      setSurveyOpen(false); setNotice("反馈已记录，谢谢你帮助改进造梦机。");
    } catch (error) { setSurveyError(error instanceof Error ? error.message : "反馈未保存，请稍后手动再试"); }
    finally { setBusy(false); }
  }

  const context: TrialContextValue = { status, refresh, generationCompleted, openSurvey: () => { setSurveyError(""); setSurveyOpen(true); } };
  if (loading || !user) return <TrialContext.Provider value={context}>{children}</TrialContext.Provider>;
  if (!status) return <main className="trial-consent-page"><section className="studio-panel trial-card"><h1>正在确认内测状态</h1>{statusError ? <><p role="alert">{statusError}</p><button className="cinema-primary" onClick={() => void refresh().catch(() => {})}>重新读取状态</button></> : <p>请稍候…</p>}<button className="text-link" onClick={() => void logout()}>退出登录</button></section></main>;
  if (!status.consented) return <main className="trial-consent-page"><section className="studio-panel trial-card" aria-labelledby="trial-consent-title">
    <p className="section-kicker">造梦机 · 内测知情说明</p><h1 id="trial-consent-title">开始前，请了解这些事项</h1>
    <p>会调用第三方模型生成；内测数据只用于改进产品；可联系我删除数据。</p>
    <p>当前演示结果为 mock，未调用第三方。生成和配音均为本地模拟，不产生模型费用。</p>
    <p>你提交的真人照片原图会在 7 天后自动删除；需要删除其他内测数据时，可点击下方“申请删除我的数据”联系项目负责人。</p>
    <p className="trial-task-card">请用造梦机做一条你想要的漫剧或 3 镜短视频。</p>
    <label className="trial-checkbox"><input type="checkbox" checked={consentChecked} onChange={event => setConsentChecked(event.target.checked)} />我已阅读并同意以上内测说明</label>
    {notice && <p role="status">{notice}</p>}
    <div className="trial-actions"><button className="cinema-primary" disabled={!consentChecked || busy} onClick={() => void acceptConsent()}>{busy ? "保存中…" : "同意并进入创作"}</button><button className="text-link" disabled={busy} onClick={() => void requestDeletion()}>申请删除我的数据</button><button className="text-link" onClick={() => void logout()}>退出登录</button></div>
  </section></main>;

  const plans = status.plans ?? [];
  const selectedPlanLabel = selectedPlan ? plans.find(plan => plan.id === selectedPlan)?.label ?? "档位待确认【确认】" : "未点击";
  return <TrialContext.Provider value={context}>
    <aside className="trial-banner" aria-label="内测任务与额度">
      <div><strong>请用造梦机做一条你想要的漫剧或 3 镜短视频。</strong><p>{status.mode === "mock" ? "当前为零成本 mock 演示，未调用第三方模型。" : "内测生成"} 每码 1 次；剩余 {status.quota_remaining} 次。</p>{!status.generation_enabled && <p role="status">{status.blocked_message || TRIAL_BLOCK_MESSAGE}</p>}</div>
      <div className="trial-actions"><button className="text-link" onClick={context.openSurvey}>填写问卷</button><button className="text-link" disabled={busy} onClick={() => void requestDeletion()}>申请删除我的数据</button></div>
    </aside>
    {notice && !paywallTaskId && <p className="trial-global-notice" role="status">{notice}</p>}
    {children}
    {paywallTaskId && <div className="trial-overlay"><section className="studio-panel trial-modal" role="dialog" aria-modal="true" aria-labelledby="trial-paywall-title">
      <h2 id="trial-paywall-title">成片完成，选一个你感兴趣的套餐</h2><p>本页只了解你的选择，不收集支付信息。</p>
      {plans.length ? <div className="trial-plans">{plans.map(plan => <article key={plan.id}><h3>{plan.label}</h3><p>{plan.price_cny === null || plan.confirmation_required ? "价格待确认【确认】" : `¥${plan.price_cny}/月`}</p><p>{plan.benefits}</p><button className="cinema-primary" disabled={busy} onClick={() => void choosePlan(plan.id)}>购买</button></article>)}</div> : <p>套餐档位及价格待确认【确认】</p>}
      {notice && <p role="status">{notice}</p>}<div className="trial-actions"><button className="cinema-primary" onClick={() => { setPaywallTaskId(null); context.openSurvey(); }}>填写反馈问卷</button><button className="text-link" onClick={() => setPaywallTaskId(null)}>先查看成片</button></div>
    </section></div>}
    {surveyOpen && <div className="trial-overlay"><section className="studio-panel trial-modal trial-survey" role="dialog" aria-modal="true" aria-labelledby="trial-survey-title">
      <div className="trial-actions"><h2 id="trial-survey-title">造梦机内测反馈</h2><button className="text-link" onClick={() => setSurveyOpen(false)}>稍后填写</button></div>
      <p>按你的真实体验填写，未体验的内容请说明；不会收集支付信息。</p>
      <form onSubmit={submitSurvey}>
        <SurveyText name="participant_code" label="代号" required />
        <SurveyText name="intent_raw" label="你想做什么？请用自己的原话描述" required />
        <SurveySelect name="independent_completion" label="是否独立出片" options={[{ value: "yes", label: "是" }, { value: "no", label: "否" }]} />
        <SurveyText name="blocker_raw" label="卡在哪一步？请用原话描述" />
        <SurveyScore name="need_fit" label="需求符合程度" max={3} />
        <SurveyScore name="character_consistency" label="人物一致性" max={3} />
        <SurveyScore name="visual_quality" label="画面质量" max={3} />
        <SurveyScore name="camera_motion" label="镜头运动" max={3} />
        <SurveySelect name="audio_subtitles" label="配音字幕" options={[{ value: "1", label: "1" }, { value: "2", label: "2" }, { value: "3", label: "3" }, { value: "n/a", label: "不适用" }]} />
        <SurveyScore name="satisfaction" label="整体满意" max={5} />
        <SurveyScore name="recommendation" label="愿意推荐" max={10} min={0} />
        <p>假门是否点击：{paywallClicked ? "是" : "否"}；点击档位：{selectedPlanLabel}</p>
        <SurveyText name="willing_monthly_raw" label="愿付多少/月？请用原话填写" />
        <SurveyText name="no_pay_reason_raw" label="不愿付费原因（原话）" />
        <SurveyText name="other_raw" label="其他建议（原话）" />
        {surveyError && <p role="alert">{surveyError}</p>}<button className="cinema-primary" type="submit" disabled={busy}>{busy ? "保存中…" : "提交反馈"}</button>
      </form>
    </section></div>}
  </TrialContext.Provider>;
}

function SurveyText({ name, label, required = false }: { name: string; label: string; required?: boolean }) {
  return <label className="trial-field" htmlFor={`survey-${name}`}><span>{label}</span><textarea id={`survey-${name}`} name={name} rows={name === "participant_code" ? 1 : 2} className={fieldClass} required={required} maxLength={3000} /></label>;
}
function SurveySelect({ name, label, options }: { name: string; label: string; options: { value: string; label: string }[] }) {
  return <label className="trial-field" htmlFor={`survey-${name}`}><span>{label}</span><select id={`survey-${name}`} name={name} className={fieldClass} required defaultValue=""><option value="" disabled>请选择</option>{options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>;
}
function SurveyScore({ name, label, max, min = 1 }: { name: string; label: string; max: number; min?: number }) {
  return <SurveySelect name={name} label={`${label}（${min}–${max}）`} options={Array.from({ length: max - min + 1 }, (_, index) => ({ value: String(min + index), label: String(min + index) }))} />;
}
