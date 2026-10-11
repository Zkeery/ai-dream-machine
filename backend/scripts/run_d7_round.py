# -*- coding: utf-8 -*-
"""按 D7 改写提示词重跑样片（默认与 S1 实测一致：Seedream + 可灵 1080P + qwen3-vl-plus）。

用法：
  # 零费用预检（强制假密钥 + 拦截全部 httpx，确认不会漏出真实请求）
  AIHUBMIX_API_KEY=dry-run-invalid-key .venv/bin/python scripts/run_d7_round.py --dry-run --only S2

  # 正式生成（代码预算硬上限；平台报错最多重试 1 次，重试前先 GET 任务列表）
  .venv/bin/python scripts/run_d7_round.py --only S2 --budget-usd 3.00

没有 AIHUBMIX_API_KEY 时只记录原因，不调用模型，不写假成片或假分数。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
# dry-run 必须在 import app 之前把 DATA_DIR 指到临时目录，避免动真实台账
if "--dry-run" in sys.argv:
    os.environ["DATA_DIR"] = "/tmp/d7-dry-run-data"
    Path(os.environ["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    os.environ["AIHUBMIX_API_KEY"] = os.environ.get("AIHUBMIX_API_KEY") or "dry-run-invalid-key"
else:
    os.environ.setdefault("DATA_DIR", str(PROJECT / ".runtime" / "d7-round-20261010"))
sys.path.insert(0, str(PROJECT / "backend"))

import httpx  # noqa: E402
from app.core import config  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.models.image_client import ImageClient  # noqa: E402
from app.models.video_client import VideoClient  # noqa: E402
from app.schemas.session import SessionCreate  # noqa: E402
from app.services import cost_control, db, d7_post, d7_runner, d7_set, execution_store, provider_jobs, session_store  # noqa: E402
from app.services.content_review import ContentReviewer  # noqa: E402
from app.services.orchestrator import Orchestrator  # noqa: E402

OWNER = "d7-round-20261010"
CNY_PER_USD = 8.0
RETRYABLE = {"VIDEO_SUBMIT_UNCONFIRMED", "VIDEO_MODEL_ERROR", "VIDEO_QUERY_FAILED", "VIDEO_TIMEOUT",
             "IMAGE_SUBMIT_UNCONFIRMED", "IMAGE_MODEL_ERROR", "IMAGE_TIMEOUT", "VLM_ERROR"}
REQUIRED_ZH = ["景别", "构图", "动作", "镜头", "对焦"]
REQUIRED_EN = ["Shot size", "Framing", "Action", "Camera", "Focus"]
NEG_MARKERS = ["主体被裁切", "出画", "滑步", "画面静止", "动态模糊", "失焦"]


def _generation() -> dict:
    return d7_set.load_document()["generation"]


def _models(args) -> dict:
    gen = _generation()
    return {
        "image": args.image_model or gen.get("image_model") or "doubao-seedream-5-0-pro-260628",
        "video": args.video_model or gen.get("video_model") or "kling-v3-omni",
        "vlm": args.vlm_model or gen.get("vlm_model") or config.settings.vlm_model or "qwen3-vl-plus",
        "resolution": args.resolution or gen.get("resolution") or "1080P",
        "ratio": gen.get("video_ratio") or "16:9",
        "seconds": int(gen.get("clip_seconds") or 5),
    }


def _estimate_sample(sample: dict, models: dict, plan: dict | None = None) -> dict:
    """plan 为 d7_post.reuse_plan 的结果时，只计缺的定妆图和镜头。"""
    if plan is None:
        n_img = len(sample["assets"])
        n_vid = len(sample["shots"])
    else:
        n_img = sum(1 for v in plan["assets"].values() if not v)
        n_vid = sum(1 for v in plan["shots"].values() if not v)
    img = cost_control.estimate_cny(models["image"], "image", {"images": 1})
    vid = cost_control.estimate_cny(models["video"], "video",
                                    {"seconds": models["seconds"], "resolution": models["resolution"]})
    # 审查按本地价格表估算：保守用固定 token（与台账实测接近的预留额）
    try:
        vlm = cost_control.estimate_cny(models["vlm"], "text",
                                        {"input_tokens": 33279, "output_tokens": 512})
    except AppError:
        vlm = 0.063  # 台账预留均值，仅预检用
    return {
        "images": n_img, "videos": n_vid, "reviews": n_img,
        "image_cny": img, "video_cny": vid, "vlm_cny": vlm,
        "total_cny": n_img * img + n_vid * vid + n_img * vlm,
        "total_usd": (n_img * img + n_vid * vid + n_img * vlm) / CNY_PER_USD,
    }


def _is_quota_error(error: AppError) -> bool:
    """平台余额不足（HTTP 402 / insufficient_quota）：整轮立即停止，不跑后续样本、不重试。"""
    text = f"{error.code} {error.message}"
    return error.code.endswith("_SUBMIT_REJECTED") and ("返回 402" in text or "insufficient_quota" in text) \
        or "insufficient_quota" in text


def _only_ids(value: str) -> list[str]:
    ids = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in ids if item not in d7_set.SAMPLE_IDS]
    if unknown:
        raise SystemExit(f"未知样本 {unknown}")
    return ids


REAL_DB = PROJECT / ".runtime" / "d7-round-20261010" / "app.db"


def _cap_report(extra_cny: float) -> list[str]:
    """预检用：只读真实台账，确认本月全局/个人上限在加上本轮预估后不会拦截。"""
    problems = []
    if not REAL_DB.is_file():
        return ["找不到真实台账，无法核对月度上限"]
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    month = time.strftime("%Y-%m")
    row = conn.execute(
        "select coalesce(sum(case when status='completed' then charged_micros else 0 end),0), "
        "coalesce(sum(case when status in ('reserved','uncertain') then reserved_micros else 0 end),0) "
        "from model_cost_ledger where month=?", (month,)).fetchone()
    used = (row[0] + row[1]) / 1e6
    active = conn.execute("select count(*) from executions where status in ('pending','running') "
                          "and operation not in ('save','select')").fetchone()[0]
    caps = {"MONTHLY_USER_BUDGET_CNY": float(config.settings.monthly_user_budget_cny),
            "MONTHLY_BUDGET_CNY": float(config.settings.monthly_budget_cny)}
    print(f"月度台账 {month}：已记 ¥{used:.2f}（含未结预留 ¥{row[1]/1e6:.2f}），本轮预估 ¥{extra_cny:.2f}，"
          f"合计 ¥{used + extra_cny:.2f}")
    for name, cap in caps.items():
        ok = used + extra_cny <= cap
        print(f"  {name}=¥{cap:.0f}  余量 ¥{cap - used:.2f}  {'OK' if ok else '会被拦截'}")
        if not ok:
            problems.append(f"{name} 不够：需要 ¥{used + extra_cny:.2f} > ¥{cap:.0f}")
    print(f"  活跃执行 {active}（并发上限：全局 {config.settings.max_active_executions}，"
          f"每用户 {config.settings.max_active_executions_per_user}；样本串行，每次只占 1 个）")
    if active >= config.settings.max_active_executions_per_user:
        problems.append(f"已有 {active} 个活跃执行，会触发 USER_CONCURRENCY_LIMIT")
    return problems


def _check_prompts(sample: dict) -> list[str]:
    errs = []
    plan = d7_set.generation_plan(sample)
    assets = {a["id"] for a in sample["assets"]}
    for t in NEG_MARKERS:
        if t not in sample["negative_prompt_zh"]:
            errs.append(f"{sample['id']} 负面提示词缺「{t}」")
    for shot, planned in zip(sample["shots"], plan["shots"]):
        tag = f"{sample['id']}/{shot['shot_id']}"
        for t in REQUIRED_ZH:
            if t not in shot["prompt_zh"]:
                errs.append(f"{tag} 中文缺「{t}」")
        for t in REQUIRED_EN:
            if t not in shot["prompt_en"]:
                errs.append(f"{tag} 英文缺「{t}」")
        if "避免出现：" not in planned["prompt"] or "三分屏" not in planned["prompt"]:
            errs.append(f"{tag} 合成提示词未加载负面提示词")
        if any(r not in assets for r in shot["reference_ids"]):
            errs.append(f"{tag} reference_ids 无效 {shot['reference_ids']}")
    return errs


def _install_dry_network() -> list:
    """拦截全部 httpx 入口；任何真实出网都会失败。返回 captured 列表。"""
    captured: list = []

    def fake_request(method, url, **k):
        captured.append({"method": method, "url": str(url), "json": k.get("json")})
        raise httpx.ConnectError("dry-run: network disabled")

    def fake_post(url, **k):
        captured.append({"method": "POST", "url": str(url), "json": k.get("json")})
        raise httpx.ConnectError("dry-run: network disabled")

    def blocked(*a, **k):
        raise RuntimeError("dry-run: network disabled")

    httpx.request = fake_request
    httpx.post = fake_post
    httpx.get = blocked
    httpx.put = blocked
    httpx.patch = blocked
    httpx.delete = blocked
    httpx.Client = blocked
    httpx.AsyncClient = blocked
    return captured


def _spent_cny(base_rowid: int) -> float:
    db_path = Path(os.environ["DATA_DIR"]) / "app.db"
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "select coalesce(sum(charged_micros),0), "
        "coalesce(sum(case when status in ('reserved','uncertain') then reserved_micros else 0 end),0) "
        "from model_cost_ledger where rowid>?",
        (base_rowid,),
    ).fetchone()
    return (row[0] + row[1]) / 1e6


def _ledger_base() -> int:
    db_path = Path(os.environ["DATA_DIR"]) / "app.db"
    conn = sqlite3.connect(db_path)
    row = conn.execute("select coalesce(max(rowid),0) from model_cost_ledger").fetchone()
    return int(row[0])


def _review_factory(vlm_model: str):
    def _review(path: Path) -> None:
        # 临时覆盖审查模型，不改全局默认以外的用途
        old = config.settings.vlm_model
        config.settings.vlm_model = vlm_model
        try:
            reviewer = ContentReviewer()
            if not reviewer.enabled:
                return
            result = reviewer.review_image(str(path))
            if not result["safe"]:
                raise AppError("CONTENT_REVIEW_FAILED", f"内容安全拦截：{result['reason']}", 400)
        finally:
            config.settings.vlm_model = old
    return _review


def _ensure_public(models: dict) -> None:
    """运行时把 D7 需要的模型补进公开名单，避免预算/目录校验拦下。"""
    img = set(config.settings.public_image_models) | {models["image"]}
    vid = set(config.settings.public_video_reference_models) | {models["video"]}
    object.__setattr__(config.settings, "public_image_models", frozenset(img))
    object.__setattr__(config.settings, "public_video_reference_models", frozenset(vid))


async def _run_sample(sample: dict, models: dict, *, force: bool, budget_cny: float | None,
                      base_rowid: int, reuse: bool = False) -> dict:
    destination = d7_runner.OUTPUT / sample["id"]
    final = destination / "final.mp4"
    if final.exists() and not force:
        return {"id": sample["id"], "final": str(final), "skipped": True}

    # 不覆盖 S1：若目标是其他样本，路径天然隔离；若是 S1 且 force，由调用方确认
    orch = Orchestrator()
    meta = orch.create(SessionCreate(
        idea=sample["idea"], style=sample["style"], episodes=1,
        video_ratio=models["ratio"], resolution=models["resolution"],
        video_generation_mode="reference",
    ), owner_id=OWNER)
    execution_id, _created = execution_store.claim_session(
        meta.session_id, "video_generation", "generate",
        {"d7_sample": sample["id"], "fixture": "backend/tests/fixtures/d7_story_set.json",
         "image_model": models["image"], "video_model": models["video"],
         "resolution": models["resolution"]},
    )
    token = execution_store._current_execution.set(execution_id)
    per_vid = cost_control.estimate_cny(models["video"], "video",
                                        {"seconds": models["seconds"], "resolution": models["resolution"]})
    per_img = cost_control.estimate_cny(models["image"], "image", {"images": 1})

    reused: set[str] = set()

    def guard(label: str, next_cny: float) -> None:
        if budget_cny is None:
            return
        spent = _spent_cny(base_rowid)
        if spent + next_cny > budget_cny + 1e-9:
            raise SystemExit(
                f"BUDGET STOP before {label}: spent ¥{spent:.3f} + next ¥{next_cny:.3f} "
                f"> cap ¥{budget_cny:.2f}"
            )

    class GuardedImage(ImageClient):
        def text_to_image(self, prompt, out_path, model=None, *, size="1280x720"):
            if reuse and d7_post.reusable_sheet(Path(out_path)):
                reused.add(str(out_path))
                print(f"  复用定妆图 {Path(out_path).name}（不调用模型、不审查）", flush=True)
                return Path(out_path)
            guard(f"image {out_path.name}", per_img)
            last = None
            for attempt in (1, 2):
                try:
                    return super().text_to_image(prompt, out_path, model=model, size=size)
                except AppError as e:
                    last = e
                    if e.code not in RETRYABLE or attempt == 2 or _is_quota_error(e):
                        raise
                    print(f"  image retry after {e.code}", flush=True)
                    time.sleep(5)
            raise last

    class GuardedVideo(VideoClient):
        def image_to_video(self, *a, **k):
            out_path = Path(a[2] if len(a) > 2 else k["out_path"])
            if reuse and d7_post.reusable_clip(out_path, models["resolution"]):
                print(f"  复用镜头 {out_path.name}（1080P/24fps/帧数正常，不调用模型）", flush=True)
                return out_path
            guard(f"video {k.get('model') or models['video']}", per_vid)
            last = None
            for attempt in (1, 2):
                known = set()
                try:
                    # 重试前先用免费 GET 看任务是否其实已创建（仅真实模式有网络）
                    if attempt == 2:
                        try:
                            r = httpx.get(f"{config.settings.aihubmix_base}/ai/v1/videos",
                                          params={"limit": 20},
                                          headers={"Authorization": f"Bearer {config.settings.aihubmix_api_key}"},
                                          timeout=30)
                            if r.status_code < 400:
                                known = {t["id"] for t in r.json().get("data", [])}
                        except Exception:
                            known = set()
                    return super().image_to_video(*a, **k)
                except AppError as e:
                    last = e
                    if e.code not in RETRYABLE or attempt == 2 or _is_quota_error(e):
                        raise
                    print(f"  video error {e.code}; checking platform before retry", flush=True)
                    time.sleep(8)
                    try:
                        r = httpx.get(f"{config.settings.aihubmix_base}/ai/v1/videos",
                                      params={"limit": 20},
                                      headers={"Authorization": f"Bearer {config.settings.aihubmix_api_key}"},
                                      timeout=30)
                        if r.status_code < 400:
                            now = [t for t in r.json().get("data", []) if t["id"] not in known
                                   and t.get("model") == models["video"]]
                            if now:
                                raise SystemExit(
                                    f"platform already created {now[0]['id']} ({now[0].get('status')}); "
                                    "NOT resubmitting — wait for it, do not re-run the script"
                                )
                    except SystemExit:
                        raise
                    except Exception:
                        pass
                    print(f"  video retry 1 after {e.code}", flush=True)
            raise last

    try:
        rendered = d7_runner.render_sample(
            sample, GuardedImage(), GuardedVideo(), destination,
            image_model=models["image"], video_model=models["video"],
            review_image=(lambda path, _r=_review_factory(models["vlm"]): None
                          if str(path) in reused else _r(path)),
            resolution=models["resolution"], video_ratio=models["ratio"],
        )
        # 与 S1 一致的后期：本地原片 → final-master.mp4（CRF16）+ final.mp4（两遍编码，<15MB）
        post = await asyncio.to_thread(d7_post.finalize, [Path(path) for path in rendered["clips"]], destination)
        rendered["post"] = {"master": post["master"], "preview": post["preview"], "trims": post["trims"]}
        print(f"  后期：master {post['master_info']['size']/1e6:.1f}MB，预览 {post['preview_info']['size']/1e6:.1f}MB，"
              f"去帧 {[t['trim'] for t in post['trims']]}", flush=True)
        execution_store.finish(execution_id, {
            "type": "done", "stage": "video_generation",
            "message": f"{sample['id']} 已生成", "percent": 100,
        }, "completed")
        meta = orch.get(meta.session_id)
        meta.status = "session_completed"
        session_store.touch(meta)
        rendered["final"] = str(final)
        rendered["session_id"] = meta.session_id
        return rendered
    except SystemExit:
        execution_store.fail(execution_id, "BUDGET_STOP", "预算硬上限")
        raise
    except AppError as error:
        execution_store.fail(execution_id, error.code, error.message)
        raise
    except Exception:
        execution_store.fail(execution_id, "INTERNAL_ERROR", "D7 样本生成失败")
        raise
    finally:
        execution_store._current_execution.reset(token)


def _dry_run(samples: list[dict], models: dict, *, reuse: bool = False, budget_usd: float | None = None) -> int:
    assert config.settings.aihubmix_api_key == "dry-run-invalid-key", (
        "dry-run 必须用假密钥：AIHUBMIX_API_KEY=dry-run-invalid-key"
    )
    assert str(Path(os.environ["DATA_DIR"]).resolve()).startswith("/tmp/"), (
        f"dry-run DATA_DIR must be under /tmp, got {os.environ['DATA_DIR']}"
    )
    config.settings.ensure_dirs()
    db.init_db()

    captured = _install_dry_network()
    # 台账预留也走本地，允许；但禁止任何出网
    cost_control.reserve = lambda *a, **k: None  # type: ignore
    cost_control.settle = lambda *a, **k: None  # type: ignore
    provider_jobs.check_active = lambda *a, **k: None  # type: ignore

    print("=== D7 零费用预检 ===")
    print(f"image={models['image']}  video={models['video']}  vlm={models['vlm']}")
    print(f"resolution={models['resolution']}  ratio={models['ratio']}  seconds={models['seconds']}")
    print(f"output root={d7_runner.OUTPUT}  (S1 成片不会被本预检改写)")
    print(f"post: 本地原片 → final-master.mp4 (x264 {d7_post.MASTER_PRESET} CRF{d7_post.MASTER_CRF}, {d7_post.FPS}fps, 无音轨)"
          f" + final.mp4 (两遍 {d7_post.PREVIEW_KBPS}k/{d7_post.PREVIEW_MAXRATE_KBPS}k, <{d7_post.PREVIEW_LIMIT_BYTES/1e6:.0f}MB)；"
          "第0帧仅在判定为定妆图时去掉；不缩放、不锐化")

    all_errs = []
    totals = {"images": 0, "videos": 0, "reviews": 0, "total_cny": 0.0, "total_usd": 0.0}
    plans = {}
    for sample in samples:
        errs = _check_prompts(sample)
        all_errs.extend(errs)
        plans[sample["id"]] = (d7_post.reuse_plan(sample, d7_runner.OUTPUT / sample["id"], models["resolution"])
                               if reuse else None)
        est = _estimate_sample(sample, models, plans[sample["id"]])
        for k in ("images", "videos", "reviews", "total_cny", "total_usd"):
            totals[k] += est[k]
        out = d7_runner.OUTPUT / sample["id"]
        s1_safe = sample["id"] != "S1" or not (out / "final.mp4").exists()
        print(f"\n{sample['id']}: assets={est['images']} shots={est['videos']} "
              f"est ¥{est['total_cny']:.2f} (${est['total_usd']:.2f}) "
              f"out={out} exists_final={(out / 'final.mp4').exists()}")
        if plans[sample["id"]]:
            pl = plans[sample["id"]]
            print(f"  复用定妆图 {[k for k, v in pl['assets'].items() if v]}  需生成 {[k for k, v in pl['assets'].items() if not v]}")
            print(f"  复用镜头 {[k for k, v in pl['shots'].items() if v]}  需生成 {[k for k, v in pl['shots'].items() if not v]}")
        for shot in sample["shots"]:
            print(f"  {shot['shot_id']} refs={shot['reference_ids']} "
                  f"zh_len={len(shot['prompt_zh'])} en_len={len(shot['prompt_en'])}")
        if errs:
            print("  PROMPT ERRORS:")
            for e in errs:
                print(f"   - {e}")

    # 走真实代码路径，确认可灵 1080P pro / Seedream / 参考图
    import jsonschema
    schema_path = Path("/tmp/schema/kling-v3-omni.json")
    schema = None
    if schema_path.is_file():
        schema = json.loads(schema_path.read_text())["endpoints"][0]["request"]["schema"]

    ok_payloads = True
    for sample in samples:
        plan = d7_set.generation_plan(sample)
        # 假定妆图：1x1 png
        sheets = {}
        sheet_dir = Path(f"/tmp/d7-dry/{sample['id']}/sheets")
        sheet_dir.mkdir(parents=True, exist_ok=True)
        tiny = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        rp = plans[sample["id"]] or {"assets": {}, "shots": {}}
        img_submits = vid_submits = 0
        for asset in plan["assets"]:
            if rp["assets"].get(asset["id"]):
                sheets[asset["id"]] = d7_runner.OUTPUT / sample["id"] / "sheets" / f"{asset['id']}.png"
                continue
            p = sheet_dir / f"{asset['id']}.png"
            p.write_bytes(tiny)
            sheets[asset["id"]] = p
            img_submits += 1
            n0 = len(captured)
            try:
                ImageClient().text_to_image(asset["prompt"], Path(f"/tmp/d7-dry/{asset['id']}.png"),
                                            model=models["image"])
            except (AppError, httpx.ConnectError, RuntimeError):
                pass
            if len(captured) != n0 + 1:
                print(f"  WARN {sample['id']}/{asset['id']}: expected 1 image submit, got {len(captured)-n0}")
                ok_payloads = False
            else:
                pl = captured[-1]["json"] or {}
                print(f"  image payload {asset['id']}: model={pl.get('model')} "
                      f"watermark={((pl.get('extra') or {}).get('watermark'))} "
                      f"async={pl.get('async')}")
                if pl.get("model") != models["image"]:
                    ok_payloads = False
        for shot in plan["shots"]:
            if rp["shots"].get(shot["shot_id"]):
                continue
            vid_submits += 1
            refs = [str(sheets[r]) for r in shot["reference_ids"]]
            n0 = len(captured)
            try:
                VideoClient().image_to_video(
                    refs[0], shot["prompt"], Path(f"/tmp/d7-dry/{shot['shot_id']}.mp4"),
                    "reference", reference_paths=refs[1:], video_ratio=models["ratio"],
                    resolution=models["resolution"], model=models["video"],
                )
            except (AppError, httpx.ConnectError, RuntimeError):
                pass
            if len(captured) != n0 + 1:
                print(f"  WARN {sample['id']}/{shot['shot_id']}: expected 1 video submit")
                ok_payloads = False
                continue
            pl = captured[-1]["json"] or {}
            if schema:
                try:
                    jsonschema.validate(pl, schema)
                except Exception as e:
                    print(f"  schema fail {shot['shot_id']}: {e}")
                    ok_payloads = False
            checks = {
                "model": pl.get("model") == models["video"],
                "1080p": str(pl.get("resolution", "")).lower() == "1080p",
                "pro": (pl.get("extra") or {}).get("mode") == "pro",
                "5s": pl.get("duration") == models["seconds"],
                "silent": pl.get("generate_audio") is False,
                "refs": len(pl.get("input_references") or []) == len(shot["reference_ids"]),
            }
            print(f"  video payload {shot['shot_id']}: {checks}")
            ok_payloads &= all(checks.values())
        print(f"  {sample['id']} 将发出：图片 {img_submits} 次，可灵 {vid_submits} 次")

    # 后期串联自检：用本地合成的小测试片（ffmpeg testsrc，不出网）跑同一个 d7_post.finalize，输出只写 /tmp
    ok_post = True
    for sample in samples:
        real_out = d7_runner.OUTPUT / sample["id"]
        if sample["id"] != "S1" and real_out.resolve() == (d7_runner.OUTPUT / "S1").resolve():
            ok_post = False
        post_dir = Path(f"/tmp/d7-dry/{sample['id']}/post")
        post_dir.mkdir(parents=True, exist_ok=True)
        fakes = []
        for shot in sample["shots"]:
            fake = post_dir / f"{shot['shot_id']}.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc2=size=320x180:rate=24:duration=1", "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", str(fake)], check=True)
            fakes.append(fake)
        try:
            post = d7_post.finalize(fakes, post_dir / "out")
            mi, pi = post["master_info"], post["preview_info"]
            good = (mi["fps"] == pi["fps"] == 24.0 and not mi["has_audio"] and not pi["has_audio"]
                    and pi["size"] < d7_post.PREVIEW_LIMIT_BYTES)
            print(f"  post {sample['id']}: {Path(post['master']).name}+{Path(post['preview']).name} "
                  f"frames={mi['frames']}/{pi['frames']} 24fps={mi['fps'] == 24.0} 无音轨={not mi['has_audio']} "
                  f"真实输出目录={real_out} ok={good}")
            ok_post &= good
        except AppError as e:
            print(f"  post {sample['id']} FAILED: {e.code} {e.message}")
            ok_post = False

    print("\n=== 费用预估（台账价格，不是账单）===")
    print(f"合计：图片 {totals['images']} × Seedream + 视频 {totals['videos']} × 可灵 "
          f"+ 审查 {totals['reviews']} × VLM")
    print(f"总计约 ¥{totals['total_cny']:.2f} = ${totals['total_usd']:.2f}")
    print(f"可灵单价：${0.4225}/镜（平台实际）；Seedream：按价格表；审查：按价格表 token 估算")
    print(f"captured network attempts (all blocked): {len(captured)}")
    print(f"prompt errors: {len(all_errs)}  payloads_ok: {ok_payloads}  post_ok: {ok_post}")

    cap_problems = _cap_report(totals["total_cny"])
    if budget_usd is not None:
        fits = totals["total_usd"] <= budget_usd + 1e-9
        print(f"本轮硬上限 ${budget_usd:.2f}：预估 ${totals['total_usd']:.2f}  {'OK' if fits else '超出'}")
        if not fits:
            cap_problems.append("预估超过 --budget-usd")
    for item in cap_problems:
        print(f"  CAP PROBLEM: {item}")

    if all_errs or not ok_payloads or not ok_post or cap_problems:
        return 1
    print("DRY OK — 零费用，未发出真实请求")
    return 0


async def _main(args) -> int:
    models = _models(args)
    if args.post_only:
        shots_dir = Path(args.post_only)
        out_dir = Path(args.out_dir) if args.out_dir else shots_dir.parent
        if (out_dir / "final.mp4").exists() and not args.force:
            print(f"{out_dir} 已有成片，不覆盖；要覆盖请加 --force")
            return 2
        clips = sorted(shots_dir.glob("s[0-9]*.mp4"))
        post = d7_post.finalize(clips, out_dir)
        print(json.dumps({k: post[k] for k in ("master", "preview", "trims", "preview_kbps")}, ensure_ascii=False, indent=1))
        return 0
    if args.dry_run:
        samples = d7_set.load_samples()
        if args.only:
            samples = [s for s in samples if s["id"] in _only_ids(args.only)]
        elif args.skip_s1:
            samples = [s for s in samples if s["id"] != "S1"]
        if not samples:
            print("没有要预检的样本")
            return 2
        return _dry_run(samples, models, reuse=args.reuse_existing, budget_usd=args.budget_usd)

    blocked = d7_runner.credential_block()
    if blocked:
        d7_runner.write_status(blocked)
        print(blocked["reason"])
        for step in blocked["how_to_run"]:
            print(f"- {step}")
        return 2

    _ensure_public(models)
    # 审查模型
    config.settings.vlm_model = models["vlm"]
    config.settings.ensure_dirs()
    db.init_db()
    base = _ledger_base()
    budget_cny = None if args.budget_usd is None else args.budget_usd * CNY_PER_USD

    samples = d7_set.load_samples()
    if args.only:
        samples = [s for s in samples if s["id"] in _only_ids(args.only)]
        if not samples:
            print(f"未知样本 {args.only}")
            return 2
    elif args.skip_s1:
        samples = [s for s in samples if s["id"] != "S1"]

    print(f"models: {models}  budget_cny={budget_cny}  ledger_base={base}")
    results = []
    for sample in samples:
        errs = _check_prompts(sample)
        if errs:
            print(f"{sample['id']} 提示词检查失败，跳过生成：")
            for e in errs:
                print(f"  - {e}")
            results.append({"id": sample["id"], "error": "prompt_check_failed", "code": "PROMPT_CHECK"})
            continue
        final = d7_runner.OUTPUT / sample["id"] / "final.mp4"
        if final.exists() and not args.force:
            results.append({"id": sample["id"], "final": str(final), "skipped": True})
            print(f"{sample['id']} 已有成片，跳过。要重跑请加 --force。")
            continue
        print(f"生成 {sample['id']}：{sample['idea']}", flush=True)
        try:
            rendered = await _run_sample(sample, models, force=args.force,
                                         budget_cny=budget_cny, base_rowid=base,
                                         reuse=args.reuse_existing)
        except SystemExit as e:
            results.append({"id": sample["id"], "error": str(e), "code": "BUDGET_STOP"})
            print(f"  停止：{e}")
            break
        except AppError as error:
            results.append({"id": sample["id"], "error": error.message, "code": error.code})
            print(f"  失败 {error.code}: {error.message}")
            if _is_quota_error(error):
                print("  平台返回 402（余额不足/未到账）：终止整个运行，不再尝试后续样本。", flush=True)
                break
            continue
        results.append(rendered)
        print(f"  成片 {rendered['final']}  本轮台账 ¥{_spent_cny(base):.3f}", flush=True)

    generated = [item for item in results if item.get("final")]
    d7_runner.write_status({
        "generated": bool(generated) and all(item.get("final") for item in results),
        "scores": None,
        "note": "有成片也不代表通过。五维和合规红线仍为空，等待人工观看后填写待评分表。这里没有自动打分。",
        "image_model": models["image"],
        "video_model": models["video"],
        "vlm_model": models["vlm"],
        "resolution": models["resolution"],
        "video_mode": "reference",
        "ledger_spent_cny": _spent_cny(base),
        "samples": [
            {"id": item["id"], "final": item.get("final"), "skipped": item.get("skipped", False),
             "error": item.get("error"), "code": item.get("code")}
            for item in results
        ],
    })
    return 0 if generated and all(item.get("final") for item in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="D7 样片生成（默认 Seedream + 可灵 1080P）")
    parser.add_argument("--only", default=None, help="样本 ID，可逗号分隔，例如 S3,S4,S5")
    parser.add_argument("--reuse-existing", action="store_true",
                        help="output/<样本>/sheets 与 shots 里已有且合格的定妆图/镜头直接复用，只生成缺的")
    parser.add_argument("--skip-s1", action="store_true", help="跳过 S1（批量跑 S2–S5 时用）")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="零费用预检：假密钥 + 拦截全部网络")
    parser.add_argument("--budget-usd", type=float, default=None,
                        help="本轮新增花费硬上限（美元）；超出则停。不传则只靠月预算。")
    parser.add_argument("--image-model", default=None)
    parser.add_argument("--video-model", default=None)
    parser.add_argument("--vlm-model", default=None)
    parser.add_argument("--resolution", default=None, choices=["720P", "1080P"])
    parser.add_argument("--post-only", metavar="SHOTS_DIR", default=None,
                        help="只做后期（不调用模型）：用本地单镜目录产出 final-master.mp4 与 final.mp4")
    parser.add_argument("--out-dir", default=None, help="--post-only 的输出目录")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_main(args)))
    except AppError as error:
        d7_runner.write_status({
            "generated": False, "reason": error.message, "code": error.code,
            "scores": None, "how_to_run": d7_runner.HOW_TO_RUN,
        })
        print(f"{error.code}: {error.message}")
        sys.exit(1)
