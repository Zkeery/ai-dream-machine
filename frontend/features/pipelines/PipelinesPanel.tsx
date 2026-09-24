"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import {
  createTask,
  downloadTask,
  listTasks,
  streamTask,
  uploadFile,
  type TaskMeta,
} from "@/lib/api/tasks";

const TYPE_CN: Record<string, string> = {
  literary_video: "文艺短视频",
  motion_transfer: "动作迁移",
  talking_head: "数字人口播",
};
const STATUS_CN: Record<string, string> = {
  pending: "排队中",
  running: "生成中",
  completed: "完成",
  failed: "失败",
};

const inputCls =
  "w-full px-3 py-2 rounded-md bg-surface-2 border border-border text-foreground text-sm placeholder:text-muted focus:outline-none focus:border-primary";

export function PipelinesPanel() {
  const [tab, setTab] = useState<"literary" | "motion" | "talking">("literary");
  const [tasks, setTasks] = useState<TaskMeta[]>([]);
  const [msg, setMsg] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // literary
  const [text, setText] = useState("");
  const [style, setStyle] = useState("realistic");
  // motion
  const [charFile, setCharFile] = useState<File | null>(null);
  const [motionFile, setMotionFile] = useState<File | null>(null);
  const [prompt, setPrompt] = useState("");
  // talking
  const [personFile, setPersonFile] = useState<File | null>(null);
  const [script, setScript] = useState("");

  const reloadTasks = useCallback(async () => {
    try {
      setTasks(await listTasks());
    } catch {
      // 忽略
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    listTasks()
      .then((t) => {
        if (!cancelled) setTasks(t);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  function subscribe(taskId: string) {
    void streamTask(taskId, (ev) => {
      if (ev.type === "done" || ev.type === "error") {
        void reloadTasks();
      }
    });
  }

  async function submit(type: string, input: Record<string, unknown>) {
    setSubmitting(true);
    setMsg("");
    try {
      const { task_id } = await createTask(type, input);
      setMsg("已提交，后台生成中…");
      await reloadTasks();
      subscribe(task_id);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function submitLiterary() {
    if (!text.trim()) return setMsg("请输入文案或灵感");
    await submit("literary_video", { text: text.trim(), style });
    setText("");
  }

  async function submitMotion() {
    if (!charFile || !motionFile || !prompt.trim())
      return setMsg("请选择角色图、动作视频并填写提示词");
    try {
      setSubmitting(true);
      const character_image = await uploadFile(charFile);
      const motion_video = await uploadFile(motionFile);
      await submit("motion_transfer", { character_image, motion_video, prompt: prompt.trim() });
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function submitTalking() {
    if (!personFile || !script.trim()) return setMsg("请选择人物图并填写口播文案");
    try {
      setSubmitting(true);
      const person_image = await uploadFile(personFile);
      await submit("talking_head", { person_image, script: script.trim() });
      setScript("");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h2 className="text-base font-semibold text-foreground mb-3">短管线（一次性出片）</h2>
        <div className="flex gap-2">
          {(["literary", "motion", "talking"] as const).map((t) => (
            <Button
              key={t}
              variant={tab === t ? "primary" : "secondary"}
              onClick={() => setTab(t)}
            >
              {TYPE_CN[t]}
            </Button>
          ))}
        </div>
      </div>

      {tab === "literary" && (
        <div className="space-y-4">
          <div>
            <label htmlFor="lit-text" className="block text-sm text-muted mb-1">文案 / 灵感</label>
            <textarea
              id="lit-text"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              placeholder="灵感或完整文案"
              className={`${inputCls} resize-y`}
            />
          </div>
          <div>
            <label htmlFor="lit-style" className="block text-sm text-muted mb-1">风格</label>
            <Input id="lit-style" value={style} onChange={(e) => setStyle(e.target.value)} />
          </div>
          <Button onClick={() => void submitLiterary()} disabled={submitting}>
            生成
          </Button>
        </div>
      )}

      {tab === "motion" && (
        <div className="space-y-4">
          <div>
            <label htmlFor="motion-char" className="block text-sm text-muted mb-1">角色图</label>
            <input id="motion-char" type="file" accept="image/*" onChange={(e) => setCharFile(e.target.files?.[0] ?? null)} />
          </div>
          <div>
            <label htmlFor="motion-video" className="block text-sm text-muted mb-1">动作视频</label>
            <input id="motion-video" type="file" accept="video/*" onChange={(e) => setMotionFile(e.target.files?.[0] ?? null)} />
          </div>
          <div>
            <label htmlFor="motion-prompt" className="block text-sm text-muted mb-1">提示词</label>
            <Input id="motion-prompt" value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="描述角色要做的动作" />
          </div>
          <Button onClick={() => void submitMotion()} disabled={submitting}>
            生成
          </Button>
        </div>
      )}

      {tab === "talking" && (
        <div className="space-y-4">
          <div>
            <label htmlFor="talking-person" className="block text-sm text-muted mb-1">人物图</label>
            <input id="talking-person" type="file" accept="image/*" onChange={(e) => setPersonFile(e.target.files?.[0] ?? null)} />
          </div>
          <div>
            <label htmlFor="talking-script" className="block text-sm text-muted mb-1">口播文案</label>
            <textarea
              id="talking-script"
              value={script}
              onChange={(e) => setScript(e.target.value)}
              rows={3}
              placeholder="口播文案"
              className={`${inputCls} resize-y`}
            />
          </div>
          <Button onClick={() => void submitTalking()} disabled={submitting}>
            生成
          </Button>
        </div>
      )}

      {msg && <p className="text-sm text-muted">{msg}</p>}

      <div className="border-t border-border pt-4">
        <h3 className="text-sm font-medium text-foreground mb-3">任务</h3>
        {tasks.length === 0 ? (
          <p className="text-sm text-muted">暂无任务</p>
        ) : (
          <ul className="space-y-2">
            {tasks.map((t) => (
              <li key={t.task_id} className="flex items-center gap-3 text-sm border border-border rounded-lg px-3 py-2">
                <span className="text-foreground">{TYPE_CN[t.type] ?? t.type}</span>
                <span className={`text-xs ${t.status === "failed" ? "text-danger" : "text-muted"}`}>
                  {STATUS_CN[t.status] ?? t.status}
                </span>
                {t.error && <span className="text-xs text-danger flex-1 truncate">{t.error}</span>}
                {t.status === "completed" && (
                  <Button variant="secondary" onClick={() => void downloadTask(t.task_id)}>
                    下载
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
