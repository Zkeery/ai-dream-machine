"""One-command recording reset/start; manages only its own local processes."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / ".runtime/release-demo"
FRONT = ROOT / "frontend"
ENV = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "TMPDIR", "LANG") if k in os.environ}
ENV.update(NEXT_PUBLIC_RECORDING_DEMO="1", RECORDING_DEMO_DIST="1", NEXT_TELEMETRY_DISABLED="1")
ENV.update(BACKEND_URL="http://127.0.0.1:8030", NEXT_PUBLIC_API_URL="", NEXT_PUBLIC_BASE_PATH="")


def stop_owned(name):
    path = RUN / f"{name}.pid"
    if not path.exists():
        return
    pid = int(path.read_text())
    try:
        command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()
    except subprocess.CalledProcessError:
        path.unlink(missing_ok=True)
        return
    expected = "serve_mock_demo.py" if name == "backend" else "npm run start"
    cwd = subprocess.check_output(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], text=True)
    expected_cwd = ROOT if name == "backend" else FRONT
    if expected not in command or f"n{expected_cwd}\n" not in cwd or os.getpgid(pid) != pid:
        raise RuntimeError(f"{name} 进程标记不匹配，已停止操作")
    os.killpg(pid, signal.SIGTERM)
    path.unlink(missing_ok=True)


def assert_free(port):
    for _ in range(30):
        found = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
        if not found.stdout.strip():
            return
        time.sleep(0.1)
    raise RuntimeError(f"端口 {port} 被其他服务占用；未停止或修改该服务。")


def fingerprint():
    result = hashlib.sha256()
    result.update(json.dumps({k: ENV[k] for k in ("NEXT_PUBLIC_RECORDING_DEMO", "RECORDING_DEMO_DIST", "BACKEND_URL", "NEXT_PUBLIC_API_URL", "NEXT_PUBLIC_BASE_PATH")}, sort_keys=True).encode())
    for directory, dirs, files in os.walk(FRONT):
        dirs[:] = sorted(d for d in dirs if d not in {"node_modules", ".next", ".next-recording", ".git"})
        for name in sorted(files):
            if name.endswith((".tsx", ".ts", ".css", ".json", ".mjs")) and name != "next-env.d.ts":
                p = Path(directory) / name
                result.update(str(p.relative_to(FRONT)).encode())
                result.update(p.read_bytes())
    return result.hexdigest()


def start(name, args, cwd):
    with (RUN / f"{name}.log").open("a") as log:
        process = subprocess.Popen(args, cwd=cwd, env=ENV, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
    (RUN / f"{name}.pid").write_text(str(process.pid))
    return process


def wait_ready(url, process):
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError("演示服务未启动，请查看 .runtime/release-demo 中的日志")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("演示服务启动超时，请查看 .runtime/release-demo 中的日志")


def main():
    if sys.argv[1:] != ["reset"]:
        raise SystemExit("用法：./demo.sh reset")
    RUN.mkdir(parents=True, exist_ok=True)
    for name in ("backend", "frontend"):
        stop_owned(name)
    for port in (8030, 3030):
        assert_free(port)
    print("正在准备本地录屏作品；正式数据和评审记录不受影响。", flush=True)
    python = str(ROOT / "backend/.venv/bin/python")
    seeded = subprocess.check_output([python, "-B", str(ROOT / "backend/scripts/reset_mock_demo.py")], cwd=ROOT, env=ENV, text=True)
    pair = json.loads(seeded.strip().splitlines()[-1])
    signature = fingerprint()
    marker = RUN / "frontend-build.sha256"
    if not (FRONT / ".next-recording/BUILD_ID").exists() or not marker.exists() or marker.read_text() != signature:
        print("首次使用或页面有更新，正在构建录屏界面…", flush=True)
        subprocess.run(["npm", "run", "build"], cwd=FRONT, env=ENV, check=True)
        marker.write_text(fingerprint())
    backend = start("backend", [python, "-B", str(ROOT / "backend/scripts/serve_mock_demo.py")], ROOT)
    wait_ready("http://127.0.0.1:8030/api/health", backend)
    frontend = start("frontend", ["npm", "run", "start", "--", "--hostname", "127.0.0.1"], FRONT)
    wait_ready("http://127.0.0.1:3030", frontend)
    print("已重置：雨夜归途（全部素材）＋雨夜归途（继续制作）。")
    print("打开：" + pair["url"])
    print("本地演示邀请码：DEM23456。旧演示作品已从列表移除，备份保留。")
    print("运行中仅使用本地素材，没有模型调用或费用。")


if __name__ == "__main__":
    main()
