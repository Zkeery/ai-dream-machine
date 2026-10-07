"""Isolated browser acceptance server with local media and the real renderer.

Run from backend: .venv/bin/python scripts/serve_comic_acceptance.py
Always binds 127.0.0.1:8031 and uses .runtime/comic-acceptance-data.
Credentials are written privately to .runtime/comic-acceptance-auth.json.
The production .env is never loaded. No external model or TTS calls are made.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
RUNTIME = PROJECT / ".runtime"
DATA = RUNTIME / "comic-acceptance-data"
AUTH_FILE = RUNTIME / "comic-acceptance-auth.json"
CALLS_FILE = DATA / "acceptance-calls.json"
HOST, PORT = "127.0.0.1", 8031

# Set before importing app: these paths cannot be overridden from the shell.
os.environ["DATA_DIR"] = str(DATA)
os.environ["AIHUBMIX_API_KEY"] = ""
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["CONTENT_REVIEW_ENABLED"] = "0"
os.environ["HOST"], os.environ["PORT"] = HOST, str(PORT)
sys.path.insert(0, str(BACKEND))
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: False

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from app.schemas.session import ComicStoryboardArtifact, ScriptArtifact, StoryboardArtifact  # noqa: E402

_calls: list[dict] = []
_lock = threading.Lock()


def record(kind: str, **fields) -> None:
    with _lock:
        _calls.append({"kind": kind, "at": time.time(), **fields})
        CALLS_FILE.write_text(json.dumps(_calls, ensure_ascii=False, indent=2), encoding="utf-8")


class LocalLLM:
    def generate(self, system, user, *, model=None):
        record("llm", operation="generate", model=model)
        return "雨夜里，一只猫收到一封来自灯塔的神秘信。"

    def generate_json(self, system, user, model_cls, *, model=None):
        record("llm", operation="generate_json", schema=model_cls.__name__, model=model)
        if model_cls is ScriptArtifact:
            data = {"title": "灯塔来信 · 本地验收", "logline": "雨夜的猫带着来信走向灯塔。", "genre": ["奇幻"], "mood": "温暖",
                    "characters": [{"character_id": "c1", "name": "小夜", "description": "黑白猫，琥珀色眼睛，红围巾。", "role": "主角"}],
                    "settings": [{"setting_id": "l1", "name": "海边灯塔", "description": "雨后的深蓝海岸，灯塔发出暖光。"}],
                    "episodes": [{"episode_number": 1, "act_title": "启程", "content": "画面：雨夜街道。小夜拾起信封。对白：雨停了。画面：海边灯塔。对白：灯塔在等你。"}]}
        elif model_cls is ComicStoryboardArtifact:
            data = {"shots": [
                {"shot_id": "s1", "description": "漫画雨夜街道，小夜举起神秘信封。", "prompt": "manga cat with letter, rainy midnight street",
                 "character_ids": ["c1"], "setting_ids": [], "motion": "push_in",
                 "dialogues": [{"line_id": "line1", "speaker_id": "c1", "text": "雨停了。"}]},
                {"shot_id": "s2", "description": "漫画海边灯塔，小夜迎着暖光前行。", "prompt": "manga cat lighthouse, blue coast, warm beacon",
                 "character_ids": ["c1"], "setting_ids": ["l1"], "motion": "pan_left",
                 "dialogues": [{"line_id": "line2", "speaker_id": "narrator", "text": "灯塔在等你。"}]}
            ], "voice_map": {"c1": "zh-CN-YunxiNeural", "narrator": "zh-CN-XiaoxiaoNeural"}}
        elif model_cls is StoryboardArtifact:
            data = {"shots": [{"shot_id": "s1", "episode_number": 1, "description": "小夜拾起信封", "prompt": "manga cat letter", "character_ids": ["c1"], "setting_ids": ["l1"]},
                              {"shot_id": "s2", "episode_number": 1, "description": "小夜走向灯塔", "prompt": "manga lighthouse", "character_ids": ["c1"], "setting_ids": ["l1"]}]}
        else:
            raise RuntimeError("Acceptance fixture does not support this model schema")
        return model_cls.model_validate(data)


class LocalImage:
    def text_to_image(self, prompt, out_path, model=None, **kwargs):
        record("image", operation="text_to_image", prompt=prompt)
        output = Path(out_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        width, height = 720, 1280
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            draw.line((0, y, width, y), fill=(14 + y // 100, 22 + y // 42, 48 + y // 20))
        draw.ellipse((475, 80, 610, 215), fill=(244, 218, 169))
        draw.polygon([(0, 875), (145, 790), (310, 860), (480, 770), (720, 900), (720, 1280), (0, 1280)], fill=(13, 24, 37))
        if "lighthouse" in prompt.lower() or "灯塔" in prompt:
            draw.polygon([(482, 430), (545, 430), (570, 910), (459, 910)], fill=(219, 201, 179))
            draw.rectangle((467, 390, 560, 448), fill=(253, 212, 121))
            draw.polygon([(455, 390), (573, 390), (514, 330)], fill=(38, 44, 67))
            draw.polygon([(514, 419), (715, 305), (715, 520)], fill=(170, 145, 85))
        # A valid locally drawn panel; deliberately not a claim of model quality.
        draw.ellipse((190, 640, 460, 930), fill=(233, 228, 217), outline=(9, 13, 25), width=9)
        draw.polygon([(210, 682), (214, 580), (291, 661)], fill=(233, 228, 217), outline=(9, 13, 25), width=9)
        draw.polygon([(363, 660), (445, 580), (441, 692)], fill=(41, 48, 62), outline=(9, 13, 25), width=9)
        draw.ellipse((247, 735, 277, 775), fill=(191, 135, 47))
        draw.ellipse((360, 735, 390, 775), fill=(191, 135, 47))
        draw.polygon([(310, 785), (329, 785), (319, 799)], fill=(36, 28, 37))
        draw.rounded_rectangle((218, 885, 434, 940), radius=12, fill=(183, 58, 61))
        draw.rounded_rectangle((382, 929, 432, 1060), radius=8, fill=(183, 58, 61))
        draw.rectangle((90, 92, 393, 145), fill=(13, 19, 34))
        draw.text((111, 108), "LOCAL COMIC ACCEPTANCE", font=ImageFont.load_default(size=19), fill=(220, 231, 248))
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:10]
        draw.text((91, 1175), "Fixture " + digest, font=ImageFont.load_default(size=16), fill=(138, 156, 187))
        image.save(output, format="PNG")
        return output

    def image_to_image(self, image_path, prompt, out_path, model=None, **kwargs):
        record("image_references", paths=[str(value) for value in image_path] if isinstance(image_path, list) else [str(image_path)])
        return self.text_to_image(prompt, out_path, model, **kwargs)


class LocalTTS:
    async def synthesize(self, text, out_path, *, voice=None):
        record("tts", text=text, voice=voice)
        output = Path(out_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Generate PCM WAV locally, then encode the requested MP3 for browser MIME.
        with tempfile.TemporaryDirectory(prefix="acceptance-voice-", dir=output.parent) as directory:
            wav = Path(directory) / "voice.wav"
            rate, duration = 24000, min(2.5, max(1.0, len(text) * 0.16))
            frequency = 330 if voice == "zh-CN-YunxiNeural" else 440
            frames = bytearray()
            for index in range(round(rate * duration)):
                position = index / rate
                envelope = min(1.0, position * 20, (duration - position) * 20)
                amplitude = int(6000 * envelope * math.sin(2 * math.pi * frequency * position))
                frames.extend(struct.pack("<h", amplitude))
            with wave.open(str(wav), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(frames)
            process = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-codec:a", "libmp3lame", str(output),
                                                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, error = await process.communicate()
            if process.returncode:
                raise RuntimeError("Local acceptance audio encoding failed")
        return output


class LocalVideo:
    def image_to_video(self, *args, **kwargs):
        raise RuntimeError("Story video model is disabled in this acceptance service; story creation remains available")


class LocalReviewer:
    enabled = False

    def review_image(self, path):
        return {"safe": True, "reason": "local acceptance image"}


def prepare_app():
    DATA.mkdir(parents=True, exist_ok=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    from app.core import config
    from app.main import app
    from app.api.deps import get_orchestrator
    from app.services import auth, db
    from app.services.orchestrator import Orchestrator
    if config.DATA_DIR != DATA.resolve() or config.settings.aihubmix_api_key:
        raise RuntimeError("Acceptance isolation check failed")
    db.init_db()
    credentials = None
    if AUTH_FILE.exists():
        credentials = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        with db.connect() as connection:
            valid = connection.execute("SELECT 1 FROM invite_codes WHERE code=?", (credentials.get("invite_code"),)).fetchone()
        if not valid:
            credentials = None
    if credentials is None:
        invite = auth.generate_invite_codes(1)[0]
        user = auth._bind_user(invite)
        credentials = {"invite_code": invite, "user_id": user["user_id"], "backend_url": f"http://{HOST}:{PORT}"}
        AUTH_FILE.write_text(json.dumps(credentials, ensure_ascii=False, indent=2), encoding="utf-8")
    AUTH_FILE.chmod(0o600)
    CALLS_FILE.write_text("[]", encoding="utf-8")
    orchestrator = Orchestrator(llm=LocalLLM(), image=LocalImage(), video=LocalVideo(), tts=LocalTTS(), reviewer=LocalReviewer())
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    return app


def main():
    # Check before importing main or creating any DB files; never displace a server.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((HOST, PORT))
        except OSError:
            raise SystemExit("Acceptance port 8031 is occupied; existing service left running") from None
    import uvicorn
    app = prepare_app()
    print("Isolated comic acceptance: http://127.0.0.1:8031; credentials saved privately in .runtime")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
