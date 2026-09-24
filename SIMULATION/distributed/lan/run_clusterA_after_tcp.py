"""Cluster A (labrob07-12): cuando termine la campana TCP, unirse al backlog
no-TCP (udp+http) EN ORDEN INVERSO para complementar al cluster B (13-18) sin
colisionar. Usa los baselines cap-14400 propios de A (los del barrido TCP).

Se ejecuta en la maquina de control con el ENTORNO POR DEFECTO (VK_LAN_HOSTS sin
definir -> labrob07-12).

    python -m SIMULATION.distributed.lan.run_clusterA_after_tcp
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
BASE_CSV_A = RESULTS / "20260708_100934" / "benchmark.csv"  # baselines cap-14400 cluster A


def _tcp_orch_running() -> bool:
    """True si el orquestador TCP sigue vivo (proceso python con ese modulo)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
             "| Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=30).stdout or ""
        return "run_rotterdam_paper_tcp" in out
    except Exception:
        return True  # ante la duda, esperar


def main() -> int:
    print("[A-after-tcp] esperando a que termine la campana TCP (07-12)...",
          flush=True)
    # margen: exigir 3 comprobaciones seguidas sin el proceso TCP
    gone = 0
    while gone < 3:
        if _tcp_orch_running():
            gone = 0
        else:
            gone += 1
        time.sleep(300)
    print("[A-after-tcp] TCP terminado. Cluster A se une al backlog no-TCP "
          "(orden inverso).", flush=True)

    if not BASE_CSV_A.is_file():
        print(f"[A-after-tcp] AVISO: no encuentro baselines A {BASE_CSV_A}; "
              f"el sub-orquestador calculara los suyos.", flush=True)
        baseflag = []
    else:
        baseflag = ["--baselines-from", str(BASE_CSV_A)]

    cmd = [sys.executable, "-u", "-m",
           "SIMULATION.distributed.lan.run_rotterdam_nontcp",
           "--reverse", "--tag", "nontcp-A"] + baseflag
    print("[A-after-tcp] lanzo: " + " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    print(f"[A-after-tcp] terminado rc={rc}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
