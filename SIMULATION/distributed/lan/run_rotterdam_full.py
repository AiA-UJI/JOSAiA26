"""Fully-autonomous Rotterdam campaign.

1. SMOKE: short baseline (500 steps, 15k trips) on the last lab host to verify
   the net loads and the pipeline works end-to-end on the cluster.
2. PHASE A: every balanced partition (x-2/3/4, y-2/3/4/6) @ 20k vehicles,
   con/sin accidente, TCP, horizonte completo  -> complete division figure.
3. PHASE B: same partitions @ 15k and 30k vehicles.

    python -u -m SIMULATION.distributed.lan.run_rotterdam_full
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ROAD = "rotterdam_arterial"
PARTS = ("balanced-y-2,balanced-y-3,balanced-y-4,balanced-y-6,"
         "balanced-x-2,balanced-x-3,balanced-x-4")
COMMON = ["--road", ROAD, "--partitions", PARTS,
          "--accidents", "false,true", "--transports", "tcp",
          "--max-steps", "0", "--accident-start", "7200",
          "--accident-duration", "3600", "--per-run-timeout", "14400",
          "--baseline-timeout", "21600", "--max-groups", "6"]


def smoke() -> bool:
    sys.path.insert(0, str(ROOT))
    from SIMULATION.distributed.lan.hosts import load_config
    from SIMULATION.distributed.lan.cluster import Cluster

    cfg = load_config()
    cl = Cluster(cfg)
    host = cfg.hosts[-1]
    print(f"[rot] SMOKE en {host} (500 pasos, 15k trips)...", flush=True)
    b = cl.run_baseline(host, "rotterdam_trips_intelligent_99_15000.xml",
                        15000, False, out_subdir="smoke_rot",
                        max_steps=500, timeout=3600, road=ROAD)
    print(f"[rot] SMOKE rc={b.get('rc')} wall={b.get('wall_sec')}s "
          f"tail:\n{b.get('tail', '')}", flush=True)
    return b.get("rc") == 0


def sweep(vehicles: str, tag: str) -> int:
    cmd = [sys.executable, "-u", "-m",
           "SIMULATION.distributed.lan.run_lan_sweep",
           "--vehicles", vehicles] + COMMON
    print(f"[rot] {tag}:\n  " + " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    print(f"[rot] {tag} terminada rc={rc}", flush=True)
    return rc


def main():
    if "--skip-smoke" not in sys.argv and not smoke():
        print("[rot] SMOKE FALLIDO - abortando campana. Revisa el log.",
              flush=True)
        sys.exit(2)

    rc_a = sweep("20000", "PHASE A (7 divisiones @20k)")
    rc_b = sweep("15000,30000", "PHASE B (7 divisiones @15k/30k)")
    sys.exit(rc_a or rc_b)


if __name__ == "__main__":
    main()
