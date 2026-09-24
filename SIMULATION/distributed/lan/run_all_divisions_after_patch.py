"""Wait for the running 2-worker patch to finish, then launch the full
division-comparison sweep: ALL partitions x ALL vehicles x sin/con accidente,
TCP only, full horizon. Single self-consistent run for the "speedup por tipo de
division" figure.
"""

from __future__ import annotations
import csv, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PATCH_CSV = (ROOT / "SIMULATION" / "distributed" / "lan" / "results" /
             "20260630_180959" / "benchmark.csv")
PATCH_TARGET = 16  # distributed runs expected in the patch


def done_count() -> int:
    if not PATCH_CSV.exists():
        return 0
    return sum(1 for x in csv.DictReader(open(PATCH_CSV, encoding="utf-8"))
               if x["kind"] == "distributed" and x["status"] == "aggregated")


def main():
    print("[wait] esperando a que termine el parche de 2 workers...", flush=True)
    while done_count() < PATCH_TARGET:
        time.sleep(120)
    print(f"[wait] parche completo ({done_count()}/{PATCH_TARGET}). "
          "Dejando que termine teardown+report...", flush=True)
    time.sleep(300)  # let the patch tear down the cluster + build its report

    ALL_DIV = ("corridor-2,corridor-3,corridor-4,highway-2,"
               "balanced-x-2,balanced-x-3,balanced-x-4,"
               "balanced-y-2,balanced-y-3,balanced-y-4,balanced-y-6")
    common = ["--accidents", "false,true", "--transports", "tcp",
              "--max-steps", "0", "--accident-start", "7200",
              "--accident-duration", "3600", "--per-run-timeout", "7200",
              "--baseline-timeout", "9000", "--max-groups", "6"]

    # PHASE A (priority): all divisions at 10k -> a COMPLETE, homogeneous
    # "speedup por tipo de division" figure ready by tomorrow morning.
    phase_a = [sys.executable, "-u", "-m",
               "SIMULATION.distributed.lan.run_lan_sweep",
               "--partitions", ALL_DIV, "--vehicles", "10000"] + common
    # PHASE B: remaining loads for all divisions.
    phase_b = [sys.executable, "-u", "-m",
               "SIMULATION.distributed.lan.run_lan_sweep",
               "--partitions", ALL_DIV, "--vehicles", "8000,12000,15000"] + common

    print("[wait] PHASE A (divisiones @10k, completa):\n  " + " ".join(phase_a),
          flush=True)
    rc = subprocess.run(phase_a, cwd=str(ROOT)).returncode
    print(f"[wait] PHASE A terminada rc={rc}", flush=True)

    print("[wait] PHASE B (divisiones @8k/12k/15k):\n  " + " ".join(phase_b),
          flush=True)
    rc = subprocess.run(phase_b, cwd=str(ROOT)).returncode
    print(f"[wait] PHASE B terminada rc={rc}", flush=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
