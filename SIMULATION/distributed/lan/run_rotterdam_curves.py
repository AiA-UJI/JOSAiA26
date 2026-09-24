"""Campana Rotterdam por CURVAS COMPLETAS, horizonte fijo 14.400 steps.

Objetivo: aunque se corte, tener CURVAS enteras (speedup vs nº workers) para
cada (flota, protocolo, corte), en vez de trozos sueltos.

Regla: cada run (baseline y distribuido) se corta EXACTAMENTE en CAP=14400
steps (= ventana de salidas). TODAS IGUALES -> speedups validos.

Plan:
  0) deploy (main.py con node_occupancy.json).
  1) baselines de las 3 flotas (acc/noacc) DE UNA VEZ en paralelo (6 PCs, ~4h).
  2) distribuidos en orden que completa curvas:
       for flota in [20k, 15k, 30k]:
         for protocolo in [tcp, udp, http]:
           for corte in [y(2,3,4,6), x(2,3,4)]:
             for accidente in [no, si]:
               baseline (reusado) + 2 + 3 + 4 + 6 workers -> curva completa
Un hilo regenera INDEX_simulaciones.csv cada 15 min con datos parciales.
"""
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"

CAP = "14400"
FLOTAS = ["20000", "15000", "30000"]
PROTOCOLS = ["tcp", "udp", "http"]
CUTS = [
    "balanced-y-2,balanced-y-3,balanced-y-4,balanced-y-6",   # corte Y: 2,3,4,6
    "balanced-x-2,balanced-x-3,balanced-x-4",                 # corte X: 2,3,4
]

_stop = threading.Event()


def _newest_results_dir(before: set) -> Path | None:
    dirs = {p for p in RESULTS.iterdir() if p.is_dir() and p.name[0].isdigit()}
    new = sorted(dirs - before, key=lambda p: p.stat().st_mtime)
    return new[-1] if new else None


def _rebuild_index(tag: str = "") -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "SIMULATION.distributed.lan.build_index"],
            cwd=str(ROOT), check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if tag:
            print(f"[curves] indice actualizado tras {tag}", flush=True)
    except Exception as ex:
        print(f"[curves] fallo indice: {ex}", flush=True)


def _index_watcher():
    while not _stop.wait(900):
        _rebuild_index()
        print("[curves] (watcher) INDEX refrescado", flush=True)


def _run(cmd: list, tag: str) -> int:
    print(f"[curves] {tag}:\n  " + " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    print(f"[curves] {tag} rc={rc}", flush=True)
    return rc


def _sweep(extra: list, tag: str) -> int:
    base = [sys.executable, "-u", "-m",
            "SIMULATION.distributed.lan.run_lan_sweep",
            "--road", "rotterdam_arterial",
            "--max-steps", CAP,
            "--accident-start", "7200", "--accident-duration", "3600",
            "--per-run-timeout", "20000", "--baseline-timeout", "30000",
            "--max-groups", "6", "--no-plots"]
    return _run(base + extra, tag)


def main():
    threading.Thread(target=_index_watcher, daemon=True).start()

    # 0) deploy
    print("[curves] deploy...", flush=True)
    subprocess.run([sys.executable, "-m", "SIMULATION.distributed.lan.deploy"],
                   cwd=str(ROOT))

    # 1) baselines de las 3 flotas en paralelo (capados a 14400).
    before = {p for p in RESULTS.iterdir() if p.is_dir()}
    _sweep(["--vehicles", ",".join(FLOTAS),
            "--partitions", "balanced-y-2",  # dummy, se filtra
            "--accidents", "false,true",
            "--transports", "tcp",
            "--only-stage", "baseline"],
           "BASELINES 3 flotas (cap 14400, paralelo)")
    base_dir = _newest_results_dir(before)
    base_csv = str(base_dir / "benchmark.csv") if base_dir else ""
    print(f"[curves] baselines -> {base_csv}", flush=True)
    _rebuild_index("baselines")

    # 2) distribuidos en orden que completa curvas.
    for flota in FLOTAS:
        for proto in PROTOCOLS:
            for cut in CUTS:
                for acc in ["false", "true"]:
                    tag = (f"{flota} {proto} "
                           f"{'Y' if 'y-' in cut else 'X'} "
                           f"{'acc' if acc == 'true' else 'noacc'}")
                    extra = ["--vehicles", flota,
                             "--partitions", cut,
                             "--accidents", acc,
                             "--transports", proto,
                             "--no-baselines", "--baselines-from", base_csv]
                    _sweep(extra, "CURVA " + tag)
                    _rebuild_index(tag)

    _stop.set()
    _rebuild_index("FINAL")
    print("[curves] TODO TERMINADO", flush=True)


if __name__ == "__main__":
    main()
