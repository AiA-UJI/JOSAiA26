"""Auto-completa la equivalencia de los 12 cortes SIN intervencion:
cada ciclo -> cosecha edgedata de los cortes ya cerrados (harvest_edgedata) y
regenera las graficas por nexo (plot_multicut_equivalence). Repite hasta tener
los 12 cortes objetivo o agotar el tiempo maximo.

    set VK_LAN_HOSTS=labrob19..30 ; set VK_LAN_MASTER=labrob19
    python -m SIMULATION.distributed.lan.auto_equiv [--interval 900] [--max-hours 12]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EQUIV = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"

TARGET = {f"balanced-{ax}-{n}" for ax in ("x", "y") for n in (2, 4, 6, 8, 10, 12)}


def _cuts_with_edgedata() -> set:
    done = set()
    for d in EQUIV.iterdir():
        if not d.is_dir():
            continue
        mp = d / "meta.json"
        wdir = d / "distributed" / "workers"
        if mp.is_file() and wdir.is_dir() and list(wdir.glob("*.edgedata.xml")):
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
                if m.get("_multicut"):
                    done.add(m.get("partition"))
            except Exception:
                pass
    return done


def _run(mod: str, *extra: str) -> int:
    return subprocess.run([sys.executable, "-u", "-m", mod, *extra],
                          cwd=str(PROJECT_ROOT)).returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=900, help="segundos entre ciclos")
    ap.add_argument("--max-hours", type=float, default=12.0)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args(argv)

    t_end = time.time() + args.max_hours * 3600
    cycle = 0
    while True:
        cycle += 1
        print(f"\n[auto-equiv] ===== ciclo {cycle} "
              f"{time.strftime('%H:%M:%S')} =====", flush=True)
        _run("SIMULATION.distributed.lan.harvest_edgedata")
        _run("SIMULATION.distributed.lan.plot_multicut_equivalence",
             "--top", str(args.top))
        done = _cuts_with_edgedata()
        falt = sorted(TARGET - done)
        print(f"[auto-equiv] cortes con edgedata {len(done & TARGET)}/12 "
              f"-> faltan: {falt}", flush=True)
        if TARGET.issubset(done):
            print("[auto-equiv] COMPLETO: los 12 cortes con edgedata.", flush=True)
            return 0
        if time.time() >= t_end:
            print("[auto-equiv] tiempo maximo agotado; salgo.", flush=True)
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
