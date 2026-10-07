# -*- coding: utf-8 -*-
"""视频客户端：AIHubMix 原生 / OpenAI 兼容万相视频接口。

三种模式映射：
- first_frame → wan2.7-i2v / wan2.6-i2v（首帧生视频）
- start_end  → wan2.7-i2v（原生接口传入实际首尾帧）
- reference  → wan2.7-r2v（参考图生视频）
- talking_head → wan2.6-i2v（原生语音与口型同步，10 秒）
本地图片以 base64 data URI 传入；显式模型选择不会修改共享配置。

网络加固：查询瞬时失败有限重试；提交结果未知时不自动重提；
整体仍受 video_timeout 截止时间约束。
"""
from __future__ import annotations

import base64
import logging
import time
import struct
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.core import config
from app.core.errors import AppError
from app.services import provider_jobs
from app.models.video_contracts import VIDEO_MODE_MODELS, SEEDANCE, VEO, validate as validate_video

logger = logging.getLogger(__name__)

def _image_to_data_uri(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise AppError("IMAGE_NOT_FOUND", "参考图不存在", 400)
    ext = p.suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _image_dimensions(path: str) -> tuple[int, int]:
    """Read supported image headers without adding a runtime imaging dependency."""
    data = Path(path).read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            length = int.from_bytes(data[i:i + 2], "big")
            if length < 2:
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and i + 7 <= len(data):
                return int.from_bytes(data[i + 5:i + 7], "big"), int.from_bytes(data[i + 3:i + 5], "big")
            i += length
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    raise AppError("IMAGE_FORMAT_INVALID", "无法读取参考图尺寸，请重新生成有效参考图", 400)


class VideoClient:
    def __init__(self) -> None:
        self.api_key = config.settings.aihubmix_api_key
        self.base = config.settings.aihubmix_base
        self.timeout = config.settings.video_timeout  # 整体截止（秒）
        self.max_retries = config.settings.max_retries
        self.poll_interval = 15.0
        self.req_timeout = 60.0  # 单次 HTTP 请求超时

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _model_for(self, mode: str) -> str:
        if mode == "reference":
            return config.settings.video_reference_model
        if mode == "start_end":
            return config.settings.video_start_end_model
        return config.settings.video_first_frame_model

    def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """查询可有限重试；付费创建结果未知时不自动重提。"""
        read_only = method.upper() == "GET"
        attempts = self.max_retries + 1 if read_only else 1
        failure_kind = "unknown"
        for attempt in range(attempts):
            try:
                resp = httpx.request(method, url, headers=self._headers(), timeout=self.req_timeout, **kwargs)
                if resp.status_code >= 500:
                    failure_kind = f"HTTP_{resp.status_code}"
                    if not read_only:
                        raise AppError("VIDEO_SUBMIT_UNCONFIRMED", "视频服务异常，提交结果未确认，已停止自动重试；请检查任务记录", 502)
                elif resp.status_code >= 400:
                    if not read_only:
                        rejected = resp.status_code in provider_jobs.REJECTED_HTTP_STATUSES
                        error = AppError("VIDEO_SUBMIT_REJECTED" if rejected else "VIDEO_SUBMIT_UNCONFIRMED",
                                         f"视频接口返回 {resp.status_code}，" + ("请求未被受理；处理原因后可人工继续任务" if rejected else "提交结果未确认；请先核对供应商任务"), 502)
                        error.provider_http_status = resp.status_code
                        raise error
                    raise AppError("VIDEO_MODEL_ERROR", f"视频接口返回 {resp.status_code}", 502)
                else:
                    return resp
            except httpx.HTTPError as error:
                failure_kind = type(error).__name__
                if not read_only:
                    raise AppError("VIDEO_SUBMIT_UNCONFIRMED", "视频提交请求中断，结果未确认，已停止自动重试；请检查任务记录后再决定是否重新生成", 502) from None
            if attempt < attempts - 1:
                time.sleep(min(2 ** attempt, 4))
        logger.warning("视频状态查询失败：type=%s attempts=%s", failure_kind, attempts)
        raise AppError("VIDEO_QUERY_FAILED", "查询视频任务失败；未重新提交生成，请检查任务记录", 502)

    def image_to_video(self, image_path: str, prompt: str, out_path: Path, mode: str = "first_frame", *,
                       reference_paths: list[str] | None = None, end_image_path: str | None = None,
                       video_ratio: str | None = None, resolution: str = "720P",
                       model: str | None = None) -> Path:
        """图生视频：image_path 为本地图片路径，转 base64 后作为参考图。"""
        # The caller supplies one approved storyboard shot. A stage-wide Agent
        # brief can contain all shots and must never be appended to every clip.
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)
        if mode not in VIDEO_MODE_MODELS:
            raise AppError("VIDEO_MODE_UNSUPPORTED", "不支持的视频生成模式", 400)
        if mode == "start_end" and not end_image_path:
            raise AppError("END_FRAME_REQUIRED", "首尾帧模式需要选择实际尾帧图片", 400)
        selected_model = model or self._model_for(mode)
        if selected_model not in VIDEO_MODE_MODELS[mode]:
            raise AppError("VIDEO_MODEL_UNSUPPORTED", "所选视频模型不支持当前生成模式，请重新选择", 400)
        if selected_model in {SEEDANCE, VEO}:
            return self._native_story_video(image_path, prompt, out_path, mode, reference_paths or [],
                                            end_image_path, video_ratio or "16:9", resolution, model=selected_model)
        # New 2.6 selection always uses its reviewed native schema. Existing
        # first-frame calls without a format keep their OpenAI-compatible path.
        if mode in {"reference", "start_end"} or video_ratio is not None or selected_model == "wan2.6-i2v":
            return self._native_video(image_path, prompt, out_path, mode, reference_paths or [],
                                      end_image_path, video_ratio or "16:9", resolution,
                                      model=selected_model)
        payload = {
            "model": selected_model,
            "prompt": prompt,
            "seconds": "5",
            "size": "1280x720",
            "input_reference": _image_to_data_uri(image_path),
        }
        return self._create_or_resume("/v1/videos", payload, out_path, native=False)

    def _save_result(self, job: provider_jobs.Job, url: str, out_path: Path) -> Path:
        from app.services import cost_control
        provider_jobs.ready(job, url)
        cost_control.settle(job.id, status="completed")
        try:
            result = self._download(url, out_path)
        except AppError as error:
            provider_jobs.failed(job, error.code, uncertain=True)
            raise
        provider_jobs.completed(job, result)
        return result

    def talking_head(self, image_path: str, script: str, out_path: Path, *,
                     model: str, duration: int = 10) -> Path:
        """Generate speech and mouth motion together; never dub a silent result.

        Reviewed 2026-10-05 against AIHubMix's wan2.6-i2v endpoint schema and
        Alibaba's image-to-video API / prompt guide. Standard 2.6-i2v produces
        audio by default; `audio` is a flash-only switch. shot_type requires
        prompt_extend=True. No external audio URL or fixed TTS voice is claimed.
        """
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)
        if model != "wan2.6-i2v":
            raise AppError("VIDEO_MODEL_UNSUPPORTED", "所选模型不支持当前人物嘴型口播", 422)
        if not isinstance(script, str) or not script.strip():
            raise AppError("VALIDATION_ERROR", "请填写口播文案", 422)
        if len("".join(script.split())) > 40:
            raise AppError("TALKING_SCRIPT_TOO_LONG", "人物嘴型口播目前为10秒，请将文案缩短到40个非空白字符以内", 422)
        if type(duration) is not int or duration != 10:
            raise AppError("TALKING_DURATION_UNSUPPORTED", "人物嘴型口播目前固定为10秒", 422)
        prompt = (
            "固定机位，单一连续镜头，参考图中的同一个人物面向镜头，以自然清晰的普通话说话。"
            f"人物完整、逐字说出以下台词：\u201c{script.strip()}\u201d。"
            "台词在视频结束前说完，语速自然。必须有人物本人的清晰讲话声，嘴唇开合与发音同步，"
            "说话时嘴部自然运动，停顿时停止说话动作，带有轻微自然表情。"
            "保持人物身份特征、服饰与背景稳定，不切镜头，不换人。不要旁白、字幕、配乐或额外台词。"
        )
        payload = {
            "model": model, "prompt": prompt, "duration": duration, "resolution": "720p",
            "frame_images": [{"frame_type": "first_frame", "image_url": {"url": _image_to_data_uri(image_path)}}],
            "extra": {"prompt_extend": True, "shot_type": "single"},
        }
        return self._create_or_resume("/ai/v1/videos", payload, out_path, native=True)

    def _create_or_resume(self, endpoint: str, payload: dict, out_path: Path, *, native: bool) -> Path:
        from app.services import cost_control
        url = f"{self.base}{endpoint}"
        job = provider_jobs.begin("video", url, payload)
        if provider_jobs.reuse_local(job, out_path):
            return out_path
        if job.row.get("result_url"):
            return self._save_result(job, job.row["result_url"], out_path)
        deadline = time.time() + self.timeout
        if job.new:
            try:
                cost_control.reserve(job.id, payload["model"], "video", {
                    "seconds": float(payload.get("duration", payload.get("seconds", 5))),
                    "resolution": str(payload.get("resolution", "720P")).upper(),
                })
            except AppError as error:
                provider_jobs.not_submitted(job, error.code)
                raise
            try:
                provider_jobs.check_active(job)
            except AppError as error:
                provider_jobs.not_submitted(job, error.code)
                cost_control.settle(job.id, status="rejected")
                raise
            try:
                response = self._request_with_retry("POST", url, json=payload)
                data = response.json()
                video_id = data.get("id") if isinstance(data, dict) else None
                if not isinstance(video_id, str) or not video_id:
                    raise ValueError
                # Save the remote identity before another GET or async boundary.
                provider_jobs.submitted(job, video_id)
            except AppError as error:
                rejected = error.code == "VIDEO_SUBMIT_REJECTED"
                if rejected:
                    provider_jobs.rejected(job, error.provider_http_status)
                else:
                    provider_jobs.failed(job, error.code, uncertain=True)
                cost_control.settle(job.id, status="rejected" if rejected else "uncertain")
                raise
            except (ValueError, TypeError):
                provider_jobs.failed(job, "VIDEO_RESPONSE_INVALID", uncertain=True)
                cost_control.settle(job.id, status="uncertain")
                raise AppError("VIDEO_RESPONSE_INVALID", "视频接口未返回有效任务编号，提交结果未确认；请先核对供应商任务", 502) from None
        else:
            video_id = job.row["vendor_job_id"]
            data = {"status": "pending"}
        query_failures = 0
        while True:
            try:
                status = data.get("status")
                if status == "completed":
                    if native:
                        output = data.get("output") or []
                        result_url = output[0].get("content_url") if output else None
                    else:
                        result_url = data.get("url")
                    return self._save_result(job, result_url or f"{url}/{video_id}/content", out_path)
                if status in {"failed", "cancelled"}:
                    provider_jobs.failed(job, "VIDEO_TASK_FAILED")
                    cost_control.settle(job.id, status="uncertain")
                    raise AppError("VIDEO_TASK_FAILED", "视频生成任务失败", 502)
                provider_jobs.check_active(job)
                if time.time() >= deadline:
                    raise AppError("VIDEO_TIMEOUT", "视频生成超时，已保留任务，可稍后续接", 504)
                time.sleep(min(self.poll_interval, max(0, deadline - time.time())))
                provider_jobs.check_active(job)
                data = self._request_with_retry("GET", f"{url}/{video_id}").json()
                query_failures = 0
                if not isinstance(data, dict):
                    raise ValueError
            except AppError as error:
                if error.code == "VIDEO_QUERY_FAILED" and query_failures < 2 and time.time() < deadline:
                    query_failures += 1
                    logger.warning("视频任务查询暂时中断，保留原任务继续查询：job=%s recovery=%s", job.id, query_failures)
                    continue
                if error.code != "VIDEO_TASK_FAILED":
                    provider_jobs.failed(job, error.code, uncertain=True)
                    if not job.row.get("result_url"):
                        cost_control.settle(job.id, status="uncertain")
                raise
            except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                provider_jobs.failed(job, "VIDEO_RESPONSE_INVALID", uncertain=True)
                cost_control.settle(job.id, status="uncertain")
                raise AppError("VIDEO_RESPONSE_INVALID", "视频查询响应不完整，已保留任务，可稍后续接", 502) from None

    def _native_story_video(self, image_path: str, prompt: str, out_path: Path, mode: str,
                            reference_paths: list[str], end_image_path: str | None,
                            video_ratio: str, resolution: str, *, model: str) -> Path:
        paths = list(dict.fromkeys([image_path, *reference_paths]))
        caps = validate_video(model, mode, video_ratio, resolution, reference_count=len(paths))
        payload = {"model": model, "prompt": prompt, "duration": caps["duration_seconds"],
                   "resolution": resolution.lower(), "aspect_ratio": video_ratio}
        if mode == "reference":
            payload["input_references"] = [{"type": "image_url", "url": _image_to_data_uri(p)} for p in paths]
        else:
            ratio_a, ratio_b = (int(x) for x in video_ratio.split(":"))
            for path in [image_path, *([end_image_path] if end_image_path else [])]:
                width, height = _image_dimensions(path)
                if height <= 0 or abs(width / height - ratio_a / ratio_b) > 0.02:
                    raise AppError("REFERENCE_RATIO_MISMATCH", "首尾帧比例与项目不一致，请按项目比例重新生成参考图", 400)
            payload["frame_images"] = [{"frame_type": "first_frame", "image_url": {"url": _image_to_data_uri(image_path)}}]
            if mode == "start_end":
                payload["frame_images"].append({"frame_type": "last_frame", "image_url": {"url": _image_to_data_uri(end_image_path)}})
            if model == SEEDANCE:
                payload["aspect_ratio"] = "adaptive"
        # No Wan-specific extra fields. Veo's native schema has no audio switch.
        if model == SEEDANCE:
            payload["generate_audio"] = True
        return self._create_or_resume("/ai/v1/videos", payload, out_path, native=True)

    def _native_video(self, image_path: str, prompt: str, out_path: Path, mode: str,
                      reference_paths: list[str], end_image_path: str | None,
                      video_ratio: str, resolution: str, *, model: str) -> Path:
        """Current Wan reviewed native contract; media inputs are data URIs.

        docs.aihubmix.com/cn/api/aihubmix-video-generation; model endpoint schemas
        /call/schema/models/{wan2.7-r2v,wan2.7-i2v,wan2.6-i2v}/endpoints
        reviewed 2026-10-04. 2.6-i2v has no input_references/aspect_ratio.
        """
        resolution = resolution.lower()
        if resolution not in {"720p", "1080p"} or video_ratio not in {"16:9", "9:16", "1:1"}:
            raise AppError("VIDEO_FORMAT_UNSUPPORTED", "当前视频模型支持 720P/1080P 与 16:9、9:16、1:1", 400)
        payload = {"model": model, "prompt": prompt, "duration": 5, "resolution": resolution}
        # Wan 2.7 does not support shot_type; keep the explicit single-shot
        # prompt intact. Wan 2.6 supports single only with prompt_extend=True.
        # https://help.aliyun.com/zh/model-studio/wan-video-to-video-api-reference
        payload["extra"] = ({"prompt_extend": True, "shot_type": "single"}
                            if model == "wan2.6-i2v" else {"prompt_extend": False})
        if mode == "reference":
            paths = list(dict.fromkeys([image_path, *reference_paths]))
            payload["input_references"] = [{"type": "image_url", "url": _image_to_data_uri(p)} for p in paths]
            payload["aspect_ratio"] = video_ratio
        else:
            # i2v 的宽高比由首帧确定，首帧由上游按项目比例生成。
            ratio_a, ratio_b = (int(x) for x in video_ratio.split(":"))
            for path in [image_path, *([end_image_path] if end_image_path else [])]:
                width, height = _image_dimensions(path)
                if height <= 0 or abs(width / height - ratio_a / ratio_b) > 0.02:
                    raise AppError("REFERENCE_RATIO_MISMATCH", "首尾帧比例与项目不一致，请按项目比例重新生成参考图", 400)
            payload["frame_images"] = [{"frame_type": "first_frame", "image_url": {"url": _image_to_data_uri(image_path)}}]
            if mode == "start_end":
                payload["frame_images"].append({"frame_type": "last_frame", "image_url": {"url": _image_to_data_uri(end_image_path)}})
        return self._create_or_resume("/ai/v1/videos", payload, out_path, native=True)

    def _download(self, url: str, out_path: Path) -> Path:
        url = urljoin(f"{self.base}/", url)
        headers = self._headers() if urlparse(url).netloc == urlparse(self.base).netloc else {}
        for attempt in range(self.max_retries + 1):
            try:
                resp = httpx.get(url, headers=headers, timeout=self.timeout, follow_redirects=True)
                if resp.status_code < 400:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(resp.content)
                    return out_path
                if resp.status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            if attempt < self.max_retries:
                time.sleep(min(2**attempt, 4))
        raise AppError("VIDEO_DOWNLOAD_FAILED", "视频已生成，但下载失败；未重新提交生成，请检查任务记录", 502)
