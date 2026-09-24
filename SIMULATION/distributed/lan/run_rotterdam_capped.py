"""Campana Rotterdam FULL, ABANICO COMPLETO, horizonte FIJO de steps.

Regla: cada run (baseline y distribuido) se corta EXACTAMENTE en
CAP_STEPS = 26244 steps (= drenaje del baseline 20k). Ningun escenario con
congestion es infinito y baseline vs distribuido comparan el MISMO trabajo.

Abanico COMPLETO por flota:
  particiones (7) x accidente (si/no) x protocolos (tcp/udp/http)
para 20k, 15k y 30k. La CSV se llena fila a fila y un hilo regenera el
INDEX_simulaciones.csv cada 15 min con los datos parciales.

Orden (front-load de lo mas importante):
  0) deploy del codigo actualizado (main.py con node_occupancy.json)
  1) 20k  abanico (tcp/udp/http)  -> reusa baseline 20k (ya ~26244)
  2) 15k  abanico (tcp/udp/http)  -> reusa baseline 15k (drena <26244)
  3) 30k  abanico (tcp/udp/http)  -> RE-EJECUTA baseline 30k capado a 26244
"""
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
PHASE_A_CSV = RESULTS / "20260706_165232" / "benchmark.csv"   # baseline 20k
PHASE_B_CSV = RESULTS / "20260707_001925" / "benchmark.csv"   # baseline 15k/30k

CAP_STEPS = "26244"
PROTOCOLS = "tcp,udp,http"
PARTS = ("balanced-y-2,balanced-y-3,balanced-y-4,balanced-y-6,"
         "balanced-x-2,balanced-x-3,balanced-x-4")

_stop = threading.Event()


def _rebuild_index(tag: str = "") -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "SIMULATION.distributed.lan.build_index"],
            cwd=str(ROOT), check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if tag:
            print(f"[cap] indice actualizado tras {tag}", flush=True)
    except Exception as ex:
        print(f"[cap] no se pudo actualizar el indice: {ex}", flush=True)


def _index_watcher():
    """Regenera el INDEX cada 15 min con los datos parciales."""
    while not _stop.wait(900):
        _rebuild_index()
        print("[cap] (watcher) INDEX_simulaciones.csv refrescado", flush=True)


def _sweep(vehicles: str, tag: str, baselines: str | None) -> int:
    cmd = [sys.executable, "-u", "-m",
           "SIMULATION.distributed.lan.run_lan_sweep",
           "--road", "rotterdam_arterial",
           "--vehicles", vehicles,
           "--partitions", PARTS,
           "--accidents", "false,true",
           "--transports", PROTOCOLS,
           "--max-steps", CAP_STEPS,
           "--accident-start", "7200", "--accident-duration", "3600",
           "--per-run-timeout", "40000", "--baseline-timeout", "40000",
           "--max-groups", "6"]
    if baselines:
        cmd += ["--no-baselines", "--baselines-from", baselines]
    print(f"[cap] {tag}:\n  " + " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    print(f"[cap] {tag} terminada rc={rc}", flush=True)
    _rebuild_index(tag)
    return rc


def main():
    watcher = threading.Thread(target=_index_watcher, daemon=True)
    watcher.start()

    # 0) deploy (todo parado, sin contencion).
    print("[cap] desplegando codigo actualizado (node_occupancy) a workers...",
          flush=True)
    rc_dep = subprocess.run(
        [sys.executable, "-m", "SIMULATION.distributed.lan.deploy"],
        cwd=str(ROOT)).returncode
    print(f"[cap] deploy rc={rc_dep}", flush=True)

    baselines = ",".join(str(p) for p in (PHASE_A_CSV, PHASE_B_CSV)
                         if p.is_file())

    # 1) 20k abanico completo (reusa baseline 20k ~26244).
    rc1 = _sweep("20000", "20k ABANICO tcp/udp/http (cap 26244)", baselines)

    # 2) 15k abanico completo (reusa baseline 15k, drena <26244).
    rc2 = _sweep("15000", "15k ABANICO tcp/udp/http (cap 26244)", baselines)

    # 3) 30k abanico completo. Baseline 30k viejo drena a ~37951 (> cap): se
    #    re-ejecuta capado a 26244 (baselines=None -> el sweep lo calcula).
    rc3 = _sweep("30000",
                 "30k ABANICO tcp/udp/http + baseline capado (cap 26244)", None)

    _stop.set()
    _rebuild_index("FINAL")
    print(f"[cap] TODO TERMINADO rc=({rc1},{rc2},{rc3})", flush=True)
    sys.exit(rc1 or rc2 or rc3)


if __name__ == "__main__":
    main()
