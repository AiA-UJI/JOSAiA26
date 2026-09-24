"""Orquestador autonomo: espera a que termine la validacion de equivalencia y
luego lanza la campana TCP completa del paper (Rotterdam).

Colonia (TAPAS) NO se auto-lanza: falta preparar la red (ver CAMPAIGN_STATE.md).

    python -m SIMULATION.distributed.lan.run_paper_pipeline --equiv-run <ts>
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EQUIV = ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equiv-run", required=True,
                    help="carpeta de la validacion a esperar (bajo equivalence/)")
    ap.add_argument("--poll", type=int, default=120)
    args = ap.parse_args(argv)

    summary = EQUIV / args.equiv_run / "nexus_flow_summary.json"
    print(f"[pipeline] esperando validacion: {summary}", flush=True)
    while not summary.is_file():
        time.sleep(args.poll)
    print("[pipeline] validacion COMPLETA. Lanzando campana TCP del paper...",
          flush=True)

    rc = subprocess.run(
        [sys.executable, "-u", "-m",
         "SIMULATION.distributed.lan.run_rotterdam_paper_tcp"],
        cwd=str(ROOT)).returncode
    print(f"[pipeline] campana TCP paper rc={rc}. "
          f"Siguiente manual: Colonia (ver CAMPAIGN_STATE.md).", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
