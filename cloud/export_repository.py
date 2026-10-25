"""Export only reviewed cloud source/workflows; never copy local data or secrets."""
import argparse
import shutil
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("destination", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
if args.destination.exists():
    raise SystemExit("目标目录已存在；请使用新的目录名。")
allowed = [root / ".gitignore", root / "CLOUD_START.md"]
allowed += list((root / ".github/workflows").glob("*.yml"))
for p in (root / "cloud").rglob("*"):
    if not p.is_file() or any(part.startswith(".") for part in p.relative_to(root / "cloud").parts):
        continue
    if "__pycache__" in p.parts:
        continue
    if p.suffix in (".py", ".md", ".json", ".txt", ".lock", ".service", ".timer", ".css", ".js"):
        allowed.append(p)
allowed.append(root / "cloud/.env.example")
for p in allowed:
    dst = args.destination / p.relative_to(root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dst)
shutil.copy2(root / "CLOUD_START.md", args.destination / "README.md")
print(f"已导出 {len(allowed)} 个文件到 {args.destination.resolve()}，未复制历史数据库和密钥。")
