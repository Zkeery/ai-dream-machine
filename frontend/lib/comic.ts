import type { ComicStoryboardArtifact } from "./api/comic";

export const COMIC_MOTIONS = { static: "静态镜头", push_in: "缓慢推进", pan_left: "向左平移", pan_right: "向右平移" } as const;

/** Save the edited dialogue plan, never trigger image, audio or video generation. */
export function comicStoryboardSave(artifact: ComicStoryboardArtifact, speakerIds: string[], voiceIds: string[]): Record<string, unknown> {
  if (!Array.isArray(artifact.shots) || artifact.shots.length < 1 || artifact.shots.length > 12) throw new Error("漫剧需要 1–12 个镜头");
  const shotIds = new Set<string>(), lineIds = new Set<string>();
  const speakers = new Set(["narrator", ...speakerIds]);
  for (const shot of artifact.shots) {
    if (!shot.shot_id || shotIds.has(shot.shot_id) || shot.episode_number !== 1) throw new Error("镜头标识不可重复，第一版仅支持单集漫剧");
    shotIds.add(shot.shot_id);
    if (!shot.description?.trim() || !shot.prompt?.trim()) throw new Error("请为每个镜头填写画面与生成描述");
    if (!Object.hasOwn(COMIC_MOTIONS, shot.motion)) throw new Error("请选择支持的基础运镜");
    if (!Number.isFinite(shot.silent_duration) || shot.silent_duration < 1 || shot.silent_duration > 10) throw new Error("无对白停留时长需为 1–10 秒");
    if (!Array.isArray(shot.dialogues) || shot.dialogues.length > 8) throw new Error("每个镜头最多 8 句对白");
    for (const line of shot.dialogues) {
      if (!line.line_id || lineIds.has(line.line_id)) throw new Error("对白标识不可重复");
      lineIds.add(line.line_id);
      if (!speakers.has(line.speaker_id)) throw new Error("请为每句对白选择剧本角色或旁白");
      if (!line.text?.trim() || line.text.length > 400) throw new Error("每句对白需为 1–400 字");
    }
  }
  for (const [speaker, voice] of Object.entries(artifact.voice_map ?? {})) if (!speakers.has(speaker) || !voiceIds.includes(voice)) throw new Error("请为角色选择支持的声线");
  return { operation: "save", artifact };
}
