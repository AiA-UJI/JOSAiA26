"""Refresca indice y tablas cada N minutos mientras la campana esta corriendo.

Independiente del planificador: se puede arrancar y parar sin tocar la campana.

    python -m SIMULATION.distributed.lan.tables_refresher --every 20
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULES = ["build_index", "build_rotterdam_tables", "build_speedup_table"]


def once() -> None:
    for mod in MODULES:
        try:
            subprocess.run([sys.executable, "-m",
                            f"SIMULATION.distributed.lan.{mod}"],
                           cwd=str(ROOT), check=False, timeout=900,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception as ex:
            print(f"[tablas] {mod}: {ex}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=20, help="minutos")
    ap.add_argument("--until", default="2026-08-28 20:00")
    args = ap.parse_args(argv)
    end = datetime.strptime(args.until, "%Y-%m-%d %H:%M").timestamp()

    while time.time() < end:
        once()
        print(f"[tablas] actualizadas {datetime.now():%Y-%m-%d %H:%M:%S}",
              flush=True)
        time.sleep(max(60, args.every * 60))
    once()
    print("[tablas] fin", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
