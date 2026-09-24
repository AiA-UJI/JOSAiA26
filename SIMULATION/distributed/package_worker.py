"""
Build a self-contained zip with everything a worker PC needs to run a
distributed simulation. The zip is intentionally minimal: it excludes
results, caches, .git and other dev/debug artefacts.

Usage::

    python -m SIMULATION.distributed.package_worker
    python -m SIMULATION.distributed.package_worker --output /path/to/dist.zip

The resulting archive can be unzipped on any worker PC with Python 3.8+
and SUMO installed; then the worker is started with::

    pip install -r requirements.txt
    python -m SIMULATION.distributed.worker --master http://<master-ip>:9000
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ---- What goes into the zip ------------------------------------------------

# Top-level individual files (absolute paths inside the project)
INCLUDE_FILES: List[str] = [
    "requirements.txt",
    "README.md",
    ".gitignore",
]

# Whole directories to copy recursively. Each tuple is (relative_dir, glob_filter).
# glob_filter is matched against POSIX-style relative paths inside the dir.
INCLUDE_DIRS: List[Tuple[str, str]] = [
    ("CLASS",                              "*.py"),
    ("SIMULATION",                         "*.py"),                # main.py, CONSTANTS.py, etc. (top-level only)
    ("SIMULATION/distributed",             "*.py"),
    ("SIMULATION/data",                    "*.json"),
    ("SIMULATION/roads",                   "*"),                   # nets + sumocfg + .bat
    ("SIMULATION/trips",                   "*.xml"),               # network + trip XMLs
    ("SIMULATION/trips",                   "*.py"),                # generate_*.py, check_*.py
]

# Patterns to EXCLUDE everywhere (POSIX-style relative paths)
EXCLUDE_PATTERNS = [
    "*.pyc",
    "__pycache__/*",
    "*/__pycache__/*",
    ".git/*",
    "**/.DS_Store",
    "**/Thumbs.db",
    # SUMO temp files
    "*.alt.xml",
    "**/*.alt.xml",
    # Outputs and caches that the worker generates locally
    "SIMULATION/knowledge_output/*",
    "SIMULATION/old_results/*",
    "SIMULATION/distributed/master_work/*",
    "SIMULATION/distributed/trips_cache/*",
    "SIMULATION/distributed/worker_runs/*",
    "SIMULATION/fast_tripinfo/results/*",
    "SIMULATION/fast_tripinfo/temp/*",
    "SIMULATION/PYTHON_ETL/**/results/*",
    "SIMULATION/PYTHON_ETL/**/results_*/*",
    "SIMULATION/PYTHON_ETL/results/*",
    "SIMULATION/PYTHON_ETL/full_percentages/*",
    "SIMULATION/PYTHON_ETL/105099oK/*",
    "resultados/*",
]


def _is_excluded(rel_posix: str) -> bool:
    for pat in EXCLUDE_PATTERNS:
        if fnmatch.fnmatch(rel_posix, pat):
            return True
    return False


def _iter_dir(root: Path, sub_rel: str, glob_filter: str) -> Iterable[Path]:
    """Yield files in <root>/<sub_rel> matching glob_filter (top-level by default).

    If glob_filter is "*", recurse into subdirs too. Otherwise, top-level only.
    """
    base = root / sub_rel
    if not base.is_dir():
        return []
    if glob_filter == "*":
        return (p for p in base.rglob("*") if p.is_file())
    return (p for p in base.glob(glob_filter) if p.is_file())


def collect_files(root: Path) -> List[Path]:
    files: List[Path] = []
    seen = set()

    for rel in INCLUDE_FILES:
        p = root / rel
        if p.is_file() and rel not in seen:
            files.append(p)
            seen.add(rel)

    for sub_rel, glob_filter in INCLUDE_DIRS:
        for p in _iter_dir(root, sub_rel, glob_filter):
            rel = p.relative_to(root).as_posix()
            if rel in seen:
                continue
            if _is_excluded(rel):
                continue
            files.append(p)
            seen.add(rel)

    return files


def build_zip(output: Path, root: Path = PROJECT_ROOT) -> Tuple[Path, int, int]:
    files = collect_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in files:
            arcname = p.relative_to(root).as_posix()
            zf.write(p, arcname=arcname)
            total_bytes += p.stat().st_size

        # Embed a small manifest with build metadata
        manifest = {
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "project_root": str(root),
            "file_count": len(files),
            "raw_size_bytes": total_bytes,
            "includes": [str(p.relative_to(root).as_posix()) for p in files],
        }
        import json as _json
        zf.writestr("DIST_MANIFEST.json", _json.dumps(manifest, indent=2))

    return output, len(files), total_bytes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Package the project for a worker PC")
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output zip path (default: dist/VehicleKnowledge_worker_<timestamp>.zip)",
    )
    args = parser.parse_args(argv)

    if args.output:
        out = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = PROJECT_ROOT / "dist" / f"VehicleKnowledge_worker_{ts}.zip"

    out, n, raw = build_zip(out)
    print(f"[OK] Wrote {out}")
    print(f"     Files     : {n}")
    print(f"     Raw size  : {raw / (1024*1024):.1f} MB")
    print(f"     Zip size  : {out.stat().st_size / (1024*1024):.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
