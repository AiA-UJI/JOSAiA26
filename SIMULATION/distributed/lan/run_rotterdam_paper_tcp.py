"""Campana Rotterdam TCP COMPLETA para el paper. RESUMIBLE.

Barrido: flotas {15k,20k,30k} x divisiones {y-2,y-3,y-4,y-6, x-2,x-3,x-4,x-6}
x accidente {no,si}, protocolo TCP, horizonte fijo 14400 steps.

Reutiliza los baselines cap-14400 ya hechos y SALTA cualquier (flota,division,
accidente) TCP que ya este 'aggregated' con speedup valido en cualquier
results/*/benchmark.csv. Solo ejecuta lo que falta (p.ej. x-6 en 20k y todo
15k/30k).

Un hilo regenera INDEX_simulaciones.csv cada 15 min.

    python -m SIMULATION.distributed.lan.run_rotterdam_paper_tcp
"""
from __future__ import annotations

import csv
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
BASE_CSV = RESULTS / "20260708_100934" / "benchmark.csv"   # 6 baselines cap-14400

CAP = "14400"
FLOTAS = ["20000", "15000", "30000"]   # 20k casi hecho -> primero para cerrar rapido
DIVS = ["balanced-y-2", "balanced-y-3", "balanced-y-4", "balanced-y-6",
        "balanced-x-2", "balanced-x-3", "balanced-x-4", "balanced-x-6"]

_stop = threading.Event()


def _done_set() -> set:
    """(vehicles:str, accident_true:bool, partition) ya agregados en TCP."""
    done = set()
    for f in RESULTS.rglob("benchmark.csv"):
        try:
            with f.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if (str(r.get("road", "")).find("rotterdam") >= 0
                            and r.get("transport") == "tcp"
                            and r.get("status") == "aggregated"
                            and r.get("speedup") not in (None, "")):
                        done.add((str(r.get("vehicles")),
                                  str(r.get("accident")).strip().lower() == "true",
                                  r.get("partition")))
        except Exception:
            continue
    return done


def _rebuild_index(tag: str = "") -> None:
    try:
        subprocess.run([sys.executable, "-m",
                        "SIMULATION.distributed.lan.build_index"],
                       cwd=str(ROOT), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if tag:
            print(f"[paper-tcp] indice actualizado tras {tag}", flush=True)
    except Exception as ex:
        print(f"[paper-tcp] fallo indice: {ex}", flush=True)


def _index_watcher():
    while not _stop.wait(900):
        _rebuild_index()


def _sweep(flota: str, parts: list, acc: bool, tag: str) -> int:
    cmd = [sys.executable, "-u", "-m",
           "SIMULATION.distributed.lan.run_lan_sweep",
           "--road", "rotterdam_arterial",
           "--max-steps", CAP,
           "--accident-start", "7200", "--accident-duration", "3600",
           "--per-run-timeout", "30000", "--baseline-timeout", "40000",
           "--max-groups", "6", "--no-plots",
           "--vehicles", flota,
           "--partitions", ",".join(parts),
           "--accidents", ("true" if acc else "false"),
           "--transports", "tcp",
           "--no-baselines", "--baselines-from", str(BASE_CSV)]
    print(f"[paper-tcp] {tag}:\n  " + " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    print(f"[paper-tcp] {tag} rc={rc}", flush=True)
    return rc


def main() -> int:
    if not BASE_CSV.is_file():
        print(f"[paper-tcp] FATAL: no encuentro baselines {BASE_CSV}", flush=True)
        return 1
    threading.Thread(target=_index_watcher, daemon=True).start()
    print("[paper-tcp] Rotterdam TCP completo (resumible). Flotas="
          f"{FLOTAS} divisiones={DIVS}", flush=True)

    for flota in FLOTAS:
        for acc in (False, True):
            done = _done_set()
            todo = [d for d in DIVS if (flota, acc, d) not in done]
            tag = f"{flota} {'acc' if acc else 'noacc'}"
            if not todo:
                print(f"[paper-tcp] {tag}: ya completo, salto", flush=True)
                continue
            print(f"[paper-tcp] {tag}: faltan {todo}", flush=True)
            _sweep(flota, todo, acc, "TCP " + tag)
            _rebuild_index(tag)

    _stop.set()
    _rebuild_index("FINAL")
    print("[paper-tcp] TERMINADO barrido TCP Rotterdam", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
