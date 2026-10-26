"""Package a prebuilt demo and explicitly exported public media, never a DB."""
import argparse
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[2]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination")
    parser.add_argument("--snapshot", default=str(ROOT / ".runtime/public-demo/snapshot"))
    args = parser.parse_args()
    dest = Path(args.destination).resolve()
    dest.mkdir(parents=True, exist_ok=False)
    build = ROOT / "frontend/.next-public-demo"
    shutil.copytree(build / "standalone", dest / "frontend", ignore=shutil.ignore_patterns(".env*", "*.map"))
    shutil.copytree(build / "static", dest / "frontend/.next-public-demo/static", dirs_exist_ok=True)
    shutil.copytree(Path(args.snapshot), dest / "snapshot")
    (dest / "deployment").mkdir()
    for name in ("server.py", "dream-public.locations.conf", "dream-public-api.service", "dream-public-web.service", "activate.py", "update_release.py"):
        shutil.copyfile(Path(__file__).with_name(name), dest / "deployment" / name)
    forbidden = [p for p in dest.rglob("*") if p.is_file() and (p.name.startswith(".env") or p.suffix in {".db", ".sqlite", ".pem", ".key"})]
    if forbidden: raise RuntimeError("Private file in release package")
    with tarfile.open(str(dest) + ".tar.gz", "w:gz") as archive:
        archive.add(dest, arcname="release")
    print(f"Release: {dest}.tar.gz")
