"""Campana Rotterdam reordenada para ventana ~10 dias (jue 9 -> dom 19 jul).

Objetivo: dado que la matriz completa (~15-16 dias) NO cabe, priorizar para que
lo mas valioso este garantizado aunque falte tiempo al final:

  ORDEN NUEVO:  for protocolo in [tcp, http, udp]:      # udp (poco fiable) el ultimo
                  for flota in [20k, 15k, 30k]:          # incluye 30k pronto
                    for corte in [Y(2,3,4,6), X(2,3,4)]:
                      for accidente in [no, si]:
                        baseline (REUSADO) + 2/3/4/6 workers -> curva completa

Asi TCP cubre TODAS las flotas primero -> escalado por workers + por flota +
efecto accidente, con el transporte fiable. Luego HTTP. UDP al final.

Todo capado a CAP=14400 steps (comparable). Reusa baselines ya calculados.
Salta curvas ya completadas (20k TCP Y sin/con accidente).
Un hilo regenera INDEX_simulaciones.csv cada 15 min.
"""
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"

CAP = "14400"
# baselines ya calculados y capados a 14400 (3 flotas x acc/noacc).
BASE_CSV = str(RESULTS / "20260708_100934" / "benchmark.csv")

PROTOCOLS = ["tcp", "http", "udp"]   # udp el ultimo (poco fiable)
FLOTAS = ["20000", "15000", "30000"]
CUTS = [
    ("Y", "balanced-y-2,balanced-y-3,balanced-y-4,balanced-y-6"),
    ("X", "balanced-x-2,balanced-x-3,balanced-x-4"),
]

# Curvas ya completadas en campanas previas -> no repetir.
DONE = {
    ("20000", "tcp", "Y", "false"),
    ("20000", "tcp", "Y", "true"),
}

_stop = threading.Event()


def _rebuild_index(tag: str = "") -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "SIMULATION.distributed.lan.build_index"],
            cwd=str(ROOT), check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if tag:
            print(f"[week] indice actualizado tras {tag}", flush=True)
    except Exception as ex:
        print(f"[week] fallo indice: {ex}", flush=True)


def _index_watcher():
    while not _stop.wait(900):
        _rebuild_index()
        print("[week] (watcher) INDEX refrescado", flush=True)


def _sweep(extra: list, tag: str) -> int:
    base = [sys.executable, "-u", "-m",
            "SIMULATION.distributed.lan.run_lan_sweep",
            "--road", "rotterdam_arterial",
            "--max-steps", CAP,
            "--accident-start", "7200", "--accident-duration", "3600",
            "--per-run-timeout", "20000", "--baseline-timeout", "30000",
            "--max-groups", "6", "--no-plots"]
    print(f"[week] {tag}:\n  " + " ".join(base + extra), flush=True)
    rc = subprocess.run(base + extra, cwd=str(ROOT)).returncode
    print(f"[week] {tag} rc={rc}", flush=True)
    return rc


def main():
    threading.Thread(target=_index_watcher, daemon=True).start()

    print("[week] deploy...", flush=True)
    subprocess.run([sys.executable, "-m", "SIMULATION.distributed.lan.deploy"],
                   cwd=str(ROOT))

    print(f"[week] reusando baselines -> {BASE_CSV}", flush=True)

    for proto in PROTOCOLS:
        for flota in FLOTAS:
            for cutlabel, cut in CUTS:
                for acc in ["false", "true"]:
                    if (flota, proto, cutlabel, acc) in DONE:
                        print(f"[week] SKIP (ya hecho) {flota} {proto} "
                              f"{cutlabel} {acc}", flush=True)
                        continue
                    tag = (f"{flota} {proto} {cutlabel} "
                           f"{'acc' if acc == 'true' else 'noacc'}")
                    extra = ["--vehicles", flota,
                             "--partitions", cut,
                             "--accidents", acc,
                             "--transports", proto,
                             "--no-baselines", "--baselines-from", BASE_CSV]
                    _sweep(extra, "CURVA " + tag)
                    _rebuild_index(tag)

    _stop.set()
    _rebuild_index("FINAL")
    print("[week] TODO TERMINADO", flush=True)


if __name__ == "__main__":
    main()
