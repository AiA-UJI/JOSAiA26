"""Recovery pass for the Rotterdam campaign.

Phase A/B distributed runs failed with submit_error because /api/submit
computes the balanced partition synchronously on the master and the HTTP
read timeout was 30s (a cold Rotterdam cut takes minutes). cluster.py now
waits up to 900s, and this script re-runs ALL 42 distributed runs reusing
the baseline runtimes already recorded in the phase A/B benchmark CSVs.

It first waits for the currently-running campaign process (phase B) to
exit so the cluster is free.

    python -u -m SIMULATION.distributed.lan.run_recovery_rotterdam
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
PHASE_A_CSV = RESULTS / "20260706_165232" / "benchmark.csv"
PHASE_B_CSV = RESULTS / "20260707_001925" / "benchmark.csv"

PARTS = ("balanced-y-2,balanced-y-3,balanced-y-4,balanced-y-6,"
         "balanced-x-2,balanced-x-3,balanced-x-4")


def campaign_running() -> bool:
    """True while any run_rotterdam_full / run_lan_sweep process is alive."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
         "| Where-Object { $_.CommandLine -match "
         "'run_rotterdam_full|run_lan_sweep' -and $_.ProcessId -ne "
         f"{subprocess.os.getpid()} }}).Count"],
        capture_output=True, text=True).stdout.strip()
    try:
        return int(out or 0) > 0
    except ValueError:
        return False


def _sweep(vehicles: str, tag: str, baselines: str) -> int:
    cmd = [sys.executable, "-u", "-m",
           "SIMULATION.distributed.lan.run_lan_sweep",
           "--road", "rotterdam_arterial",
           "--vehicles", vehicles,
           "--partitions", PARTS,
           "--accidents", "false,true",
           "--transports", "tcp",
           "--max-steps", "0",
           "--accident-start", "7200", "--accident-duration", "3600",
           "--per-run-timeout", "28800",
           "--no-baselines", "--baselines-from", baselines,
           "--max-groups", "6"]
    print(f"[recovery] {tag}:\n  " + " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    print(f"[recovery] {tag} terminada rc={rc}", flush=True)
    _rebuild_index(tag)
    return rc


def _rebuild_index(tag: str) -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "SIMULATION.distributed.lan.build_index"],
            cwd=str(ROOT), check=False)
        print(f"[recovery] indice actualizado tras {tag}", flush=True)
    except Exception as ex:
        print(f"[recovery] no se pudo actualizar el indice: {ex}", flush=True)


def main():
    print("[recovery] esperando a que termine la campana en curso "
          "(baselines 30k + fase B fallida)...", flush=True)
    while campaign_running():
        time.sleep(120)
    print("[recovery] campana anterior terminada; lanzando recuperacion",
          flush=True)

    baselines = ",".join(str(p) for p in (PHASE_A_CSV, PHASE_B_CSV)
                         if p.is_file())

    # Fase A primero (20k, 14 runs), luego Fase B (15k+30k, 28 runs),
    # en ese orden, reutilizando todos los baselines ya calculados.
    rc_a = _sweep("20000", "FASE A distribuida (20k, 14 runs)", baselines)
    rc_b = _sweep("15000,30000", "FASE B distribuida (15k+30k, 28 runs)",
                  baselines)
    print(f"[recovery] TODO TERMINADO rc_a={rc_a} rc_b={rc_b}", flush=True)
    sys.exit(rc_a or rc_b)


if __name__ == "__main__":
    main()
