"""Explicit TEST-only, one-run paid acceptance; disabled by default."""
import os
import re
import time
from decimal import Decimal, ROUND_CEILING
from app.core import config
from app.core.errors import AppError

MODELS = {'qwen3.5-plus': ('text', 2), 'qwen-image-2.0': ('image', 5),
          'qwen3-vl-plus': ('text', 8), 'kling-v3-omni': ('video', 3)}


def enabled() -> bool:
    return os.getenv('F1_REAL_ENABLED') == '1' and not config.settings.mode_mock


def allowed(user_id: str) -> bool:
    configured = os.getenv('F1_TEST_USER_ID', '')
    return enabled() and bool(configured) and user_id == configured


def require_user(user_id: str) -> None:
    if not allowed(user_id):
        raise AppError('F1_ACCOUNT_DISABLED', '当前只开放作者的TEST单次自测', 403)


def cap_micros() -> int:
    try:
        value = Decimal(os.getenv('F1_HARD_CAP_CNY', '12.5'))
        if not value.is_finite() or not 0 < value <= Decimal('12.5'):
            raise ValueError
        return int((value * 1000000).to_integral_value(rounding=ROUND_CEILING))
    except Exception:
        raise AppError('F1_BUDGET_INVALID', 'F1预算配置无效，已阻止付费调用', 503) from None


def authorization_id() -> str:
    value = os.getenv('F1_AUTHORIZATION_ID', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', value):
        raise AppError('F1_AUTHORIZATION_MISSING', '本次单次授权未配置，已阻止付费调用', 503)
    return value


def _ensure_run(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS f1_approved_runs (
        authorization_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        task_id TEXT UNIQUE NOT NULL, hard_cap_micros INTEGER NOT NULL,
        resolution TEXT NOT NULL, created_at REAL NOT NULL)""")


def approved_count(conn, user_id: str) -> int:
    _ensure_run(conn)
    return conn.execute('SELECT COUNT(*) FROM f1_approved_runs WHERE authorization_id=? AND owner_id=?',
                        (authorization_id(), user_id)).fetchone()[0]


def claim_task(conn, user_id: str, task_id: str) -> None:
    require_user(user_id)
    _ensure_run(conn)
    row = conn.execute('SELECT * FROM f1_approved_runs WHERE authorization_id=?',
                       (authorization_id(),)).fetchone()
    if row:
        if row['owner_id'] != user_id or row['task_id'] != task_id:
            raise AppError('TRIAL_QUOTA_EXHAUSTED', '名额已满，本次授权不能再次生成', 403)
        return
    conn.execute('INSERT INTO f1_approved_runs VALUES(?,?,?,?,?,?)',
                 (authorization_id(), user_id, task_id, cap_micros(), '720P', time.time()))


def validate_scope(meta, stage: str | None = None, *, owner_id: str | None = None) -> None:
    if not enabled():
        return
    require_user(owner_id or getattr(meta, 'owner_id', None))
    if (meta.project_type != 'story' or meta.episodes != 1 or meta.resolution.upper() != '720P'
        or meta.video_ratio != '16:9' or meta.video_generation_mode != 'reference'
        or meta.orchestration_mode != 'workflow' or meta.expand_idea or meta.knowledge_library_ids):
        raise AppError('F1_SCOPE_DISABLED', '本次仅允许720P、3镜短视频、参考图模式和固定工作流', 403)
    if stage == 'character_design':
        script = meta.artifacts.get('script_generation') or {}
        if len(script.get('characters', [])) != 1 or len(script.get('settings', [])) != 1:
            raise AppError('F1_SCRIPT_SCOPE', '剧本须恰好1名人物和1个场景，已停止后续付费调用', 409)
    if stage in {'reference_generation', 'video_generation', 'post_production'}:
        shots = (meta.artifacts.get('storyboard') or {}).get('shots', [])
        if len(shots) != 3:
            raise AppError('F1_SHOT_SCOPE', '本次分镜须恰好3镜，已停止后续付费调用', 409)


def enforce_call(conn, owner_id, execution_id, model, kind, estimate):
    if not enabled():
        return
    require_user(owner_id)
    if not execution_id or model not in MODELS or MODELS[model][0] != kind:
        raise AppError('F1_CALL_DISABLED', '付费请求未绑定本次任务或模型不在许可范围', 403)
    row = conn.execute("SELECT g.task_id,g.status,g.mode FROM executions e JOIN trial_generations g "
                       "ON g.task_id=e.entity_id WHERE e.execution_id=? AND g.owner_id=?",
                       (execution_id, owner_id)).fetchone()
    if not row or row['mode'] != 'real' or row['status'] != 'reserved':
        raise AppError('F1_TASK_STOPPED', '本次任务未开放或已停止，不能提交新的付费请求', 409)
    _ensure_run(conn)
    approved = conn.execute('SELECT task_id FROM f1_approved_runs WHERE authorization_id=? AND owner_id=?',
                            (authorization_id(), owner_id)).fetchone()
    if not approved or approved['task_id'] != row['task_id']:
        raise AppError('F1_TASK_STOPPED', '付费调用未绑定本次单次授权', 409)
    rows = conn.execute("SELECT l.model,l.status,l.charged_micros,l.reserved_micros "
                        "FROM model_cost_ledger l JOIN executions e ON e.execution_id=l.execution_id "
                        "WHERE e.entity_id=?", (row['task_id'],)).fetchall()
    if any(r['status'] in {'uncertain', 'rejected'} for r in rows):
        raise AppError('F1_PREVIOUS_CALL_FAILED', '已有失败或未知请求，已停止整条任务', 409)
    if sum(r['model'] == model for r in rows) >= MODELS[model][1]:
        raise AppError('F1_CALL_LIMIT', '已达到本次模型请求次数上限，不能再提交', 429)
    used = sum(r['charged_micros'] if r['status'] == 'completed' else r['reserved_micros'] for r in rows)
    if used + estimate > cap_micros():
        raise AppError('F1_HARD_CAP_EXCEEDED', '本次12.5元硬上限余额不足，已阻止付费请求', 429)


def accounting_micros(conn, task_id):
    rows = conn.execute("SELECT l.status,l.charged_micros FROM model_cost_ledger l "
                        "JOIN executions e ON e.execution_id=l.execution_id WHERE e.entity_id=?",
                        (task_id,)).fetchall()
    if any(r['status'] != 'completed' for r in rows):
        raise AppError('TRIAL_PENDING_RECONCILIATION', '请求费用尚未确定，任务进入待对账', 409)
    return sum(r['charged_micros'] for r in rows)
