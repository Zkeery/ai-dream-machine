"""Switch only the two demo services to a prevalidated release; rollback on fail.

Nginx and Partner services/configuration are not changed by this operation.
"""
import datetime
import json
from pathlib import Path
import time
from activate import ROOT, fetch, partner_state, run


def point_to(release):
    pending = ROOT / "current.next"
    assert not pending.exists() and not pending.is_symlink()
    pending.symlink_to(release, target_is_directory=True)
    pending.replace(ROOT / "current")


def verify():
    for attempt in range(30):
        try:
            assert len(json.loads(fetch("https://101.96.210.187/dream/api/sessions"))) == 2
            assert "AI造梦机" in fetch("https://101.96.210.187/dream/").decode()
            return
        except Exception:
            if attempt == 29: raise
            time.sleep(0.25)


if __name__ == "__main__":
    release = Path(__file__).resolve().parents[1]
    assert release.is_relative_to(ROOT / "releases")
    current = ROOT / "current"
    assert current.is_symlink()
    old = current.resolve()
    assert old != release
    before = partner_state()
    backup = ROOT / "backups" / datetime.datetime.now(datetime.timezone.utc).strftime("update-%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, exist_ok=False)
    (backup / "previous-release.txt").write_text(str(old))
    (backup / "partner-before.json").write_text(json.dumps(before, indent=2))
    point_to(release)
    try:
        run("systemctl", "restart", "dream-public-api", "dream-public-web")
        verify()
        assert partner_state() == before, "Partner state changed"
    except BaseException:
        point_to(old)
        run("systemctl", "restart", "dream-public-api", "dream-public-web")
        verify()
        raise
    print(json.dumps({"status": "updated", "release": str(release), "previous": str(old), "partner_unchanged": True, "nginx_changed": False}))
