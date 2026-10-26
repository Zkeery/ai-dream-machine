"""Deterministic offline providers and a process-wide outbound network guard.

Mock media is synthetic test material, never a model-quality result. The guard
uses Python's socket audit events as well as DNS interception: client stubs
alone cannot protect an accidental Edge TTS/SDK call. Only loopback and Unix
domain sockets remain available for local HTTP/UI tests.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path
from uuid import uuid4

from app.core.errors import AppError

_guard_installed = False
_lock = threading.RLock()
_blocked = Counter()
_calls = Counter()
_fail_next: dict[str, str] = {}
_media_counts = Counter()
_LOCAL_PROTOCOLS = "file,pipe,lavfi"
_MACOS_MEDIA_PROFILE = "(version 1) (allow default) (deny network*)"
_original_getaddrinfo = socket.getaddrinfo
_original_gethostbyname = socket.gethostbyname
_original_gethostbyname_ex = socket.gethostbyname_ex


def _is_local_address(address) -> bool:
    if isinstance(address, (str, bytes)):
        # AF_UNIX is a local filesystem path, not a host/port tuple.
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "strict")
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
        return ip.is_loopback or bool(getattr(ip, "ipv4_mapped", None) and ip.ipv4_mapped.is_loopback)
    except (TypeError, ValueError):
        return False


def _deny(event: str) -> None:
    with _lock:
        _blocked[event] += 1
    # Do not include hosts, URLs, user input or credentials in errors/logs.
    raise PermissionError("OFFLINE_NETWORK_BLOCKED: mock execution permits loopback only")


def _socket_audit(event: str, args: tuple) -> None:
    if event in {"socket.connect", "socket.sendto"}:
        sock, address = args[0], args[1]
        if getattr(sock, "family", None) == socket.AF_UNIX:
            return
        if not _is_local_address(address):
            _deny(event)
    elif event in {"socket.getaddrinfo", "socket.gethostbyname"}:
        host = args[0]
        if host is not None and not _is_local_address((host, 0)):
            _deny(event)


def install_network_guard() -> None:
    """Install once; cannot be removed by replacing a provider/httpx method."""
    global _guard_installed
    with _lock:
        if _guard_installed:
            return
        sys.addaudithook(_socket_audit)

        def local_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host is None:
                return _original_getaddrinfo(host, port, family, type, proto, flags)
            if not _is_local_address((host, port)):
                _deny("dns")
            if host in {"localhost", b"localhost"}:
                host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
            return _original_getaddrinfo(host, port, family, type, proto, flags)

        def local_gethostbyname(host):
            if not _is_local_address((host, 0)):
                _deny("dns")
            return "127.0.0.1" if host in {"localhost", b"localhost"} else _original_gethostbyname(host)

        def local_gethostbyname_ex(host):
            if not _is_local_address((host, 0)):
                _deny("dns")
            return ("localhost", [], ["127.0.0.1"]) if host in {"localhost", b"localhost"} else _original_gethostbyname_ex(host)

        socket.getaddrinfo = local_getaddrinfo
        socket.gethostbyname = local_gethostbyname
        socket.gethostbyname_ex = local_gethostbyname_ex
        _guard_installed = True


def network_guard_status() -> dict:
    with _lock:
        return {"installed": _guard_installed, "blocked_attempts": sum(_blocked.values()), "by_event": dict(_blocked)}


def media_guard_status() -> dict:
    """Report only construction counters, not unverified OS deployment claims."""
    with _lock:
        return {"protocol_whitelist": _LOCAL_PROTOCOLS, "platform": sys.platform,
                "macos_sandbox_available": sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
                "commands": dict(_media_counts)}


def safe_media_environment() -> dict[str, str]:
    from app.core import config
    if not (config.settings.mode_mock or config.settings.disable_outbound_network):
        return dict(os.environ)
    clean = {key: value for key, value in os.environ.items()
             if not any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    clean.update({"MODE_MOCK": "1", "DISABLE_OUTBOUND_NETWORK": "1", "SKIP_PROJECT_ENV": "1"})
    return clean


def safe_media_command(command: list[str]) -> list[str]:
    """Restrict every local media subprocess before execution.

    URL operands and network-capable filter sources are rejected before a
    process is spawned. Every media input also receives a protocol whitelist,
    so a local concat/HLS manifest cannot point FFmpeg at a remote resource.
    On this Mac, sandbox-exec additionally denies the child's OS networking.
    Linux deployment still requires its separately verified OS firewall.
    """
    from app.core import config
    if not (config.settings.mode_mock or config.settings.disable_outbound_network):
        return list(command)
    if not command or any(not isinstance(item, str) or "\x00" in item for item in command):
        raise AppError("OFFLINE_MEDIA_COMMAND_INVALID", "离线媒体命令无效", 422)
    executable = Path(command[0]).name
    if executable not in {"ffmpeg", "ffprobe"}:
        raise AppError("OFFLINE_MEDIA_COMMAND_INVALID", "离线媒体处理仅允许 FFmpeg 与 ffprobe", 422)
    # Do not echo the rejected operand: it could contain a signed media URL.
    remote = re.compile(r"(?:https?|tcp|udp|tls|ftp|sftp|rtmp|rtmps|rtsp|srt|gopher|icecast|sap|rist|zmq|unix|concatf|subfile|crypto|data):", re.I)
    for operand in command[1:]:
        if "://" in operand or remote.search(operand) or re.search(r"(?:^|[;,\s])a?movie\s*=", operand) or operand in {"-filter_script", "-filter_complex_script"}:
            with _lock:
                _media_counts["rejected_before_spawn"] += 1
            raise AppError("OFFLINE_MEDIA_INPUT_BLOCKED", "离线媒体处理拒绝远程地址或网络来源", 422)
    protected = [command[0]]
    input_format = ""
    index = 1
    while index < len(command):
        operand = command[index]
        if operand == "-protocol_whitelist":
            index += 2
            continue
        if operand == "-f" and index + 1 < len(command):
            input_format = command[index + 1]
        if executable == "ffmpeg" and operand == "-i":
            if index + 1 >= len(command):
                raise AppError("OFFLINE_MEDIA_COMMAND_INVALID", "离线媒体输入缺失", 422)
            source = command[index + 1]
            if input_format != "lavfi" and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", source) and not source.startswith(("file:", "pipe:")):
                raise AppError("OFFLINE_MEDIA_INPUT_BLOCKED", "离线媒体处理只接受本地文件", 422)
            protected += ["-protocol_whitelist", _LOCAL_PROTOCOLS]
            input_format = ""
        protected.append(operand)
        index += 1
    if executable == "ffprobe":
        protected[1:1] = ["-protocol_whitelist", _LOCAL_PROTOCOLS]
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        protected = ["/usr/bin/sandbox-exec", "-p", _MACOS_MEDIA_PROFILE, *protected]
        with _lock:
            _media_counts["os_sandbox_wrapped"] += 1
    with _lock:
        _media_counts["local_command_protected"] += 1
    return protected


def mock_call_counts() -> dict[str, int]:
    with _lock:
        return dict(_calls)


def reset_mock_state() -> None:
    """For isolated acceptance tests only; does not disable the network guard."""
    with _lock:
        _calls.clear()
        _fail_next.clear()


def fail_next_mock_call(kind: str, mode: str) -> None:
    if mode not in {"timeout", "disconnect"}:
        raise ValueError("Mock failure must be timeout or disconnect")
    with _lock:
        _fail_next[kind] = mode


def mock_invocation(kind: str) -> str:
    """Exactly one invocation is counted even when its result is unknown."""
    install_network_guard()
    with _lock:
        _calls[kind] += 1
        failure = _fail_next.pop(kind, None)
    if failure:
        raise AppError("MODEL_REQUEST_INTERRUPTED", f"MOCK 模拟{('超时' if failure == 'timeout' else '断网')}：待对账，已停止自动重试", 502,
                       details={"mock": True, "result_state": "awaiting_reconciliation", "failure": failure})
    return "mock_" + uuid4().hex


def mock_image(out_path: Path, size: str = "1280x720") -> Path:
    from PIL import Image, ImageDraw
    mock_invocation("image")
    try:
        width, height = [int(x) for x in size.lower().split("x", 1)]
        if not 32 <= width <= 4096 or not 32 <= height <= 4096:
            raise ValueError
    except (TypeError, ValueError):
        raise AppError("MOCK_IMAGE_SIZE_INVALID", "测试图片尺寸不支持", 422) from None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), "#26364a")
    draw = ImageDraw.Draw(image)
    draw.rectangle((width * 0.12, height * 0.12, width * 0.88, height * 0.88), outline="#80b5f5", width=4)
    draw.text((width * 0.18, height * 0.38), "MOCK - OFFLINE TEST ONLY", fill="white")
    draw.text((width * 0.18, height * 0.52), "No model call / No charge", fill="#80b5f5")
    image.save(out_path)
    return out_path


def _ffmpeg(args: list[str], out_path: Path) -> Path:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise AppError("MOCK_FFMPEG_MISSING", "离线测试媒体需要本机 FFmpeg", 503)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Inputs are only fixed lavfi expressions or caller-owned local paths.
        command = safe_media_command([executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args, str(out_path)])
        result = subprocess.run(command, capture_output=True, timeout=45, env=safe_media_environment())
        if result.returncode or not out_path.is_file():
            raise ValueError
    except (OSError, subprocess.TimeoutExpired, ValueError):
        out_path.unlink(missing_ok=True)
        raise AppError("MOCK_MEDIA_FAILED", "离线占位媒体生成失败", 503) from None
    return out_path


def mock_audio(text: str, out_path: Path) -> Path:
    mock_invocation("tts")
    duration = min(8.0, max(1.0, len(text.strip()) * 0.12))
    # Audible synthetic tone provides a valid track, never fake human speech.
    return _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", str(duration),
                    "-af", "volume=0.08", "-c:a", "libmp3lame", "-metadata", "comment=MOCK synthetic tone"], out_path)


def mock_video(out_path: Path, *, video_ratio: str = "16:9", duration: int = 5) -> Path:
    mock_invocation("video")
    dimensions = {"16:9": "640x360", "9:16": "360x640", "1:1": "480x480"}
    if video_ratio not in dimensions or not 1 <= duration <= 10:
        raise AppError("MOCK_VIDEO_FORMAT_INVALID", "离线视频参数不支持", 422)
    return _ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size={dimensions[video_ratio]}:rate=24", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=44100", "-t", str(duration), "-af", "volume=0.03",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-movflags", "+faststart", "-metadata", "comment=MOCK offline test pattern"], out_path)


class MockEmbedding:
    """Deterministic hashed lexical vectors, explicitly not semantic quality."""
    @staticmethod
    def embed(texts, **kwargs):
        import numpy as np
        for text in texts:
            vector = np.zeros(512, dtype=float)
            for character in str(text):
                slot = int.from_bytes(hashlib.sha256(character.encode("utf-8")).digest()[:2], "big") % 512
                vector[slot] += 1
            norm = float(np.linalg.norm(vector))
            yield vector / norm if norm else vector

    query_embed = embed


def mock_structured(model_cls, user: str) -> dict:
    """Fixtures are validated by the same Pydantic contract as live output."""
    name = model_cls.__name__
    if name == "ScriptArtifact":
        return {"title": "MOCK 离线测试片", "logline": "离线测试：主角在雨夜遇见一只猫", "genre": ["测试"], "mood": "平静",
                "characters": [{"name": "小林", "character_id": "c1", "description": "测试角色", "role": "主角"}],
                "settings": [{"name": "街道", "setting_id": "l1", "description": "雨夜街道"}],
                "episodes": [{"episode_number": 1, "act_title": "相遇", "content": "雨夜，小林发现躲雨的猫，停步为它撑伞。"}]}
    if name in {"StoryboardArtifact", "ComicStoryboardArtifact"}:
        shots = [{"shot_id": f"s{i}", "episode_number": 1, "description": f"MOCK 测试镜头 {i}",
                  "prompt": f"offline test shot {i}", "character_ids": ["c1"], "setting_ids": ["l1"]} for i in range(1, 4)]
        if name == "ComicStoryboardArtifact":
            for i, shot in enumerate(shots, 1):
                shot.update({"motion": "static", "silent_duration": 1.0,
                             "dialogues": [{"line_id": f"line{i}", "speaker_id": "c1", "text": f"这是第{i}个测试镜头。"}]})
            return {"shots": shots, "voice_map": {"c1": "zh-CN-YunjianNeural"}}
        return {"shots": shots}
    if name == "AdequacyJudgment":
        return {"sufficient": False, "reason": "MOCK 不判断真实资料质量", "unsupported_facts": []}
    try:
        value = json.loads(user)
    except (ValueError, TypeError):
        value = {}
    if name == "AgentReview":
        return {"verdict": "pass", "summary": "MOCK 合同测试通过，创作质量未验证"}
    if name == "PlannerIntent":
        return {"summary": "MOCK 离线规划", "objective": "生成当前阶段的离线测试产物", "preparation": []}
    if name == "AgentPlan":
        stage = value.get("stage", "script")
        role = "writer" if stage == "script" else "director" if "storyboard" in stage else "designer" if stage in {"character_design", "reference", "comic_panels"} else "producer"
        return {"summary": "MOCK 离线任务计划", "tasks": [{"id": "mock_task", "role": role, "objective": "生成当前阶段的离线测试产物", "depends_on": []}]}
    if name == "AgentAction":
        observations = value.get("observations") or []
        serialized = json.dumps(observations, ensure_ascii=False)
        task = value.get("task") or {}
        if task.get("role") == "researcher":
            return {"tool": "finish", "note": "MOCK 未进行外部检索"}
        if "generate_stage" in serialized or "stage_generated" in serialized:
            return {"tool": "finish", "note": "MOCK 当前阶段已生成"}
        if "set_brief" in serialized:
            return {"tool": "generate_stage", "note": "生成离线测试产物"}
        return {"tool": "set_brief", "note": "设置离线测试创作备注", "brief": "MOCK 离线测试，不调用真实模型"}
    # Unsupported schemas fail closed instead of silently fabricating validity.
    raise AppError("MOCK_SCHEMA_UNSUPPORTED", f"离线测试尚未配置结构：{name}", 503)
