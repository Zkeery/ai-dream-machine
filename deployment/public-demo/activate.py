"""Install isolated demo, then add only its route to the shared HTTPS entry.

Requires root. Backs up both route configs before any config mutation, checks
nginx -t before reload (never restart), verifies Partner and rolls back on error.
"""
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import urlopen

ROOT = Path("/opt/dream-public")
RELEASE = Path(__file__).resolve().parents[1]
HTTPS = Path("/etc/nginx/conf.d/wanwu-partner-https.conf")
HTTP = Path("/etc/nginx/conf.d/dream.conf")
SNIPPET = Path("/etc/nginx/snippets/dream-public.locations.conf")
INCLUDE = "    include /etc/nginx/snippets/dream-public.locations.conf;\n"


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def fetch(url):
    with urlopen(url, timeout=15) as response:
        assert response.status == 200
        return response.read()


def partner_state():
    paths = [Path("/etc/nginx/conf.d/00-wanwu-partner-upgrade.conf"), Path("/etc/nginx/snippets/wanwu-partner.locations.conf"), Path("/etc/nginx/snippets/wanwu-partner.proxy.conf")]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    hashes["services"] = run("systemctl", "show", "wanwu-partner-backend", "wanwu-partner-frontend", "--property=MainPID,ActiveState,ExecMainStartTimestamp")
    hashes["https_page"] = hashlib.sha256(fetch("https://101.96.210.187/partner")).hexdigest()
    return hashes


def activate():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = ROOT / "backups" / stamp
    backup.mkdir(parents=True, exist_ok=False)
    before = partner_state()
    (backup / "partner-before.json").write_text(json.dumps(before, indent=2))
    for path in (HTTPS, HTTP): shutil.copy2(path, backup / path.name)
    if SNIPPET.exists(): shutil.copy2(SNIPPET, backup / SNIPPET.name)
    (backup / "release.txt").write_text(str(RELEASE))
    original_https = HTTPS.read_text()
    original_http = HTTP.read_text()
    assert INCLUDE not in original_https, "Demo already active; review existing deployment before changing"
    assert "    include /etc/nginx/snippets/wanwu-partner.locations.conf;" in original_https
    marker = "    include /etc/nginx/snippets/wanwu-partner.locations.conf;"
    start = original_http.index("    location = / { return 302 /dream/; }")
    end = original_http.index(marker)
    new_http = original_http[:start] + "    location = / { return 302 /dream/; }\n" + INCLUDE + "\n" + original_http[end:]
    new_https = original_https.replace(marker, INCLUDE + marker, 1)
    assert new_https.replace(INCLUDE, "", 1) == original_https
    current = ROOT / "current"
    if current.exists():
        assert current.is_symlink() and current.resolve() == RELEASE, "Existing public release must be reviewed"
    else:
        current.symlink_to(RELEASE, target_is_directory=True)
    for name in ("dream-public-api.service", "dream-public-web.service"):
        shutil.copyfile(RELEASE / "deployment" / name, Path("/etc/systemd/system") / name)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "dream-public-api", "dream-public-web")
    ready = False
    for _ in range(30):
        try:
            assert json.loads(fetch("http://127.0.0.1:8130/api/health"))["read_only"]
            assert "AI造梦机" in fetch("http://127.0.0.1:3130/dream/").decode()
            ready = True; break
        except Exception: time.sleep(0.5)
    assert ready, "Independent demo services did not start; Nginx untouched"
    try:
        shutil.copyfile(RELEASE / "deployment/dream-public.locations.conf", SNIPPET)
        HTTP.write_text(new_http)
        HTTPS.write_text(new_https)
        print(run("nginx", "-t"), flush=True)
        run("systemctl", "reload", "nginx")
        # systemctl reload returns once HUP is delivered. Existing Nginx workers
        # can still answer with the old routing table during graceful handover.
        for attempt in range(20):
            try:
                assert len(json.loads(fetch("https://101.96.210.187/dream/api/sessions"))) == 2
                assert "AI造梦机" in fetch("https://101.96.210.187/dream/").decode()
                break
            except Exception:
                if attempt == 19: raise
                time.sleep(0.25)
        after = partner_state()
        assert after == before, "Partner verification changed"
    except BaseException:
        for path in (HTTPS, HTTP): shutil.copy2(backup / path.name, path)
        if (backup / SNIPPET.name).exists(): shutil.copy2(backup / SNIPPET.name, SNIPPET)
        else: SNIPPET.unlink(missing_ok=True)
        print(run("nginx", "-t"), flush=True)
        run("systemctl", "reload", "nginx")
        raise
    result = {"status": "active", "backup": str(backup), "partner_unchanged": before == after, "nginx_action": "reload", "url": "https://101.96.210.187/dream"}
    (backup / "activation.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__": activate()
