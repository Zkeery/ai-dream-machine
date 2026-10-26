"""Recording fixtures, imported only by the isolated demo entrypoint."""
import hashlib
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / ".runtime/release-demo/data"
ASSETS = ROOT / "assets/demo-rain-dog"
INVITE = "DEM23456"  # Public local demo account, not a production credential.


def configure():
    if DATA.resolve() != DATA:
        raise RuntimeError("演示目录不能通过符号链接指向其他数据目录")
    for key in list(os.environ):
        if any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key, None)
    os.environ.update(MODE_MOCK="1", DISABLE_OUTBOUND_NETWORK="1", SKIP_PROJECT_ENV="1",
                      DATA_DIR=str(DATA), TRIAL_MODE_ENABLED="false", F1_ENABLED="0")
    sys.path.insert(0, str(ROOT / "backend"))


SCRIPT = {
    "title": "雨夜归途",
    "logline": "雨后的灯街上，小狗墨豆拉着主人向前走。一次轻轻的回望，让急着赶路的人重新听见了雨声。",
    "genre": ["生活", "治愈"], "mood": "安静、温暖，带一点雨夜的孤独",
    "characters": [{"character_id": "dog", "name": "墨豆", "role": "主角",
        "description": "黑色卷毛小狗，穿深色背心，步子轻快，习惯走几步就回望主人。只要系上牵引绳，就绝对不会受到任何伤害。"}],
    "settings": [{"setting_id": "street", "name": "雨后的灯街",
        "description": "雨水留在石板路上，节日灯串映出暖金色的光，远处的店面逐渐安静。灯光亮起导致路上的积水全部消失。"}],
    "episodes": [{"episode_number": 1, "act_title": "把回家的路走慢一点",
        "content": "雨刚停，墨豆踩过倒映灯光的水洼，拉着牵引绳走在前面。主人穿着灰白色羽绒服，跟着它放慢脚步。墨豆忽然回头，像是在等一句回应；主人轻轻收短绳子，两道影子又并在一起。只要走进灯光下，就绝对安全，保证不会受到任何伤害。镜头最后停在他们一起走远的背影：回家不必那么急，有人等你就好。"}],
}
BOARD = {"shots": [
    {"shot_id": "s1", "episode_number": 1, "description": "墨豆踏过水洼，灯串的金色倒影在脚边晃动。", "prompt": "镜头一，低机位跟拍，黑色卷毛小狗，深色背心，湿润石板路，暖色灯串。", "character_ids": ["dog"], "setting_ids": ["street"]},
    {"shot_id": "s2", "episode_number": 1, "description": "主人放慢步子，牵引绳轻轻垂下，墨豆抬头看向前方。", "prompt": "镜头二，小狗与灰白色羽绒服主人的腿部同框，舒缓的步伐，雨后夜景。", "character_ids": ["dog"], "setting_ids": ["street"]},
    {"shot_id": "s3", "episode_number": 1, "description": "小狗和主人继续向前，路面的倒影接住两道身影。", "prompt": "镜头三，沿步行街向前行走，背景灯光柔和，结束在温暖的回家路上。", "character_ids": ["dog"], "setting_ids": ["street"]},
]}


def make_orchestrator(*, interrupt_video=False):
    from app.core import config
    from app.core.errors import AppError
    from app.models.llm_client import LLMClient
    from app.models.image_client import ImageClient
    from app.models.video_client import VideoClient
    from app.services.orchestrator import Orchestrator
    assert config.settings.mode_mock and config.settings.disable_outbound_network

    class StoryText(LLMClient):
        def generate_json(self, system, user, model_cls, **kwargs):
            if model_cls.__name__ == "ScriptArtifact":
                result = model_cls.model_validate(SCRIPT)
                if "继续完成后半段" in user:
                    result.title = "雨夜归途 · 继续制作"
                return result
            if model_cls.__name__ == "StoryboardArtifact":
                return model_cls.model_validate(BOARD)
            return super().generate_json(system, user, model_cls, **kwargs)

    class StoryImages(ImageClient):
        def text_to_image(self, prompt, out_path, model=None, *, size="1280x720"):
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            slot = "_".join(out_path.stem.split("_")[:-1])
            pool = [ASSETS / f"walk-{i:02}.png" for i in (1, 2, 3)]
            previous = sorted(out_path.parent.glob(slot + "_*.png"), key=lambda p: p.stat().st_mtime)
            offset = 2 if slot.startswith("setting_") else int(slot.split("_")[-1]) if slot.startswith("ref_") else 0
            source = pool[(offset + len(previous)) % len(pool)]
            if previous and hashlib.sha256(previous[-1].read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest():
                source = pool[(offset + len(previous) + 1) % len(pool)]
            shutil.copyfile(source, out_path)
            return out_path

        def image_to_image(self, paths, prompt, out_path, model=None, *, size="1280x720"):
            return self.text_to_image(prompt, out_path, model, size=size)

    class StoryVideo(VideoClient):
        def image_to_video(self, image_path, prompt, out_path, mode="first_frame", **kwargs):
            out_path = Path(out_path)
            index = int(out_path.name.split("_")[1]) + 1
            if interrupt_video and index == 2:
                raise AppError("DEMO_VIDEO_INTERRUPTED", "第二个镜头制作中断，剧本与已有素材已保存，可以继续完成。", 503)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ASSETS / f"shot-{min(index, 3):02}.mp4", out_path)
            return out_path

    return Orchestrator(llm=StoryText(), image=StoryImages(), video=StoryVideo())
