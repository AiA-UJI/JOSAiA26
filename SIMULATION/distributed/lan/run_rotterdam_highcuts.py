"""Campana Rotterdam CORTES ALTOS (8/10/12) para el paper. RESUMIBLE.

Pensada para el 3er cluster (labrob19-30, 12 maquinas, SUMO via pip) EN PARALELO
a la campana TCP (labrob07-12) y a la no-TCP (labrob13-18). Selecciona cluster
por entorno:

    set VK_LAN_HOSTS=labrob19..30   set VK_LAN_MASTER=labrob19

Barrido: flotas {15k,20k,30k} x divisiones {x-8,x-10,x-12, y-8,y-10,y-12}
x accidente {no,si}, protocolo TCP, horizonte fijo 14400 steps, 12 workers.

Paso 0: calcula sus PROPIOS baselines cap-14400 en ESTE cluster (6 = 3 flotas x
2 acc, en paralelo) para que los speedups sean validos en las mismas maquinas.
Luego reutiliza esos baselines y SALTA cualquier (flota,division,acc) ya
'aggregated' con speedup. Un hilo regenera INDEX_simulaciones.csv cada 15 min.

Paso final (--equiv): validacion de equivalencia (mismos vehiculos por segmento
que el baseline) para x-12 e y-12 a 20k sin accidente, con edgeData activado.

    python -m SIMULATION.distributed.lan.run_rotterdam_highcuts
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"

CAP = "14400"
MAXG = "12"
FLOTAS = ["20000", "15000", "30000"]   # 20k primero para cerrar una flota rapido
DIVS = ["balanced-x-8", "balanced-x-10", "balanced-x-12",
        "balanced-y-8", "balanced-y-10", "balanced-y-12"]
PROTOCOL = "tcp"

# Baselines cap-14400 ya calculados en maquinas IDENTICAS (mismo modelo labrob).
# El baseline de 1 nodo no depende del cluster, asi que se reutilizan (igual que
# la campana TCP del cluster A) en vez de recalcular 6 runs de horas.
DEFAULT_BASE_CSV = RESULTS / "20260708_100934" / "benchmark.csv"

STATE_BASELINE = RESULTS / "_highcuts_baseline.txt"

_stop = threading.Event()


def _done_set() -> set:
    """(vehicles, accident_bool, partition) TCP ya agregados con speedup."""
    done = set()
    for f in RESULTS.rglob("benchmark.csv"):
        try:
            with f.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if (str(r.get("road", "")).find("rotterdam") >= 0
                            and r.get("transport") == PROTOCOL
                            and r.get("status") == "aggregated"
                            and r.get("speedup") not in (None, "")):
                        done.add((str(r.get("vehicles")),
                                  str(r.get("accident")).strip().lower() == "true",
                                  r.get("partition")))
        except Exception:
            continue
    return done


def _newest_results_dir(before: set) -> Path | None:
    dirs = {p for p in RESULTS.iterdir() if p.is_dir() and p.name[0].isdigit()}
    new = sorted(dirs - before, key=lambda p: p.stat().st_mtime)
    return new[-1] if new else None


def _rebuild_index(tag: str = "") -> None:
    try:
        subprocess.run([sys.executable, "-m",
                        "SIMULATION.distributed.lan.build_index"],
                       cwd=str(ROOT), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if tag:
            print(f"[highcuts] indice actualizado tras {tag}", flush=True)
    except Exception as ex:
        print(f"[highcuts] fallo indice: {ex}", flush=True)


def _index_watcher():
    while not _stop.wait(900):
        _rebuild_index()


def _sweep(extra: list, tag: str, maxg: str = MAXG) -> int:
    base = [sys.executable, "-u", "-m",
            "SIMULATION.distributed.lan.run_lan_sweep",
            "--road", "rotterdam_arterial",
            "--max-steps", CAP,
            "--accident-start", "7200", "--accident-duration", "3600",
            "--per-run-timeout", "36000", "--baseline-timeout", "40000",
            "--max-groups", maxg, "--no-plots"]
    print(f"[highcuts] {tag}:\n  " + " ".join(base + extra), flush=True)
    rc = subprocess.run(base + extra, cwd=str(ROOT)).returncode
    print(f"[highcuts] {tag} rc={rc}", flush=True)
    return rc


def _baselines_ok(csv_path: Path) -> bool:
    """True si el csv contiene los 6 baselines cap-14400 (3 flotas x 2 acc)."""
    try:
        flotas_ok = set()
        with csv_path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if ("rotterdam" in str(r.get("road", ""))
                        and not r.get("partition")
                        and r.get("status") == "ok"
                        and str(r.get("max_steps")) in ("14400", "14400.0")):
                    flotas_ok.add((str(r.get("vehicles")),
                                   str(r.get("accident")).strip().lower() == "true"))
        need = {(v, a) for v in FLOTAS for a in (False, True)}
        return need.issubset(flotas_ok)
    except Exception:
        return False


def _ensure_baselines() -> str:
    """Baselines PROPIOS de ESTE cluster (mismas maquinas -> speedups validos)."""
    if STATE_BASELINE.is_file():
        p = Path(STATE_BASELINE.read_text(encoding="utf-8").strip())
        if p.is_file() and _baselines_ok(p):
            print(f"[highcuts] baselines de este cluster reutilizados: {p}",
                  flush=True)
            return str(p)

    print("[highcuts] calculando 6 baselines cap-14400 en ESTE cluster "
          "(paralelo)...", flush=True)
    before = {p for p in RESULTS.iterdir() if p.is_dir()}
    _sweep(["--vehicles", ",".join(FLOTAS),
            "--partitions", "balanced-x-8",   # dummy, --only-stage baseline lo ignora
            "--accidents", "false,true",
            "--transports", PROTOCOL,
            "--only-stage", "baseline"],
           "BASELINES 6 (cap 14400)")
    d = _newest_results_dir(before)
    if d:
        csv_path = d / "benchmark.csv"
        try:
            STATE_BASELINE.write_text(str(csv_path), encoding="utf-8")
        except Exception:
            pass
        return str(csv_path)
    return ""


def _run_equivalence(base_csv: str) -> None:
    """Justificacion 'mismos vehiculos por segmento que el baseline' para los
    cortes altos: x-12 e y-12 a 20k sin accidente, con edgeData."""
    for part in ("balanced-x-12", "balanced-y-12"):
        tag = f"EQUIV {part} 20k noacc"
        print(f"[highcuts] {tag}", flush=True)
        cmd = [sys.executable, "-u", "-m",
               "SIMULATION.distributed.lan.run_rotterdam_equivalence",
               "--vehicles", "20000", "--max-steps", CAP,
               "--partition", part, "--workers", "12",
               "--road", "rotterdam_arterial"]
        rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
        print(f"[highcuts] {tag} rc={rc}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reverse", action="store_true",
                    help="recorre la matriz en orden inverso")
    ap.add_argument("--no-equiv", action="store_true",
                    help="omite la fase final de equivalencia")
    ap.add_argument("--baselines-from", default=str(DEFAULT_BASE_CSV),
                    help="csv de baselines cap-14400 a reutilizar (maquinas "
                         "identicas). '' fuerza recalcularlos en este cluster.")
    ap.add_argument("--divs", default=",".join(DIVS),
                    help="cortes a barrer (coma). Por defecto los 6 altos.")
    ap.add_argument("--flotas", default=",".join(FLOTAS),
                    help="flotas a barrer (coma).")
    ap.add_argument("--max-groups", default=MAXG,
                    help="tope de workers (10 si el cluster tiene 11 nodos).")
    args = ap.parse_args(argv)

    maxg = str(args.max_groups)
    flotas = [x for x in args.flotas.split(",") if x]
    divs = [x for x in args.divs.split(",") if x]
    if args.reverse:
        flotas = flotas[::-1]
        divs = divs[::-1]

    threading.Thread(target=_index_watcher, daemon=True).start()
    print(f"[highcuts] Rotterdam TCP cortes altos {divs} (resumible). "
          f"Flotas={flotas} 12 workers cap={CAP}", flush=True)

    if args.baselines_from and Path(args.baselines_from).is_file():
        base_csv = args.baselines_from
        print(f"[highcuts] baselines REUTILIZADOS (maquinas identicas): "
              f"{base_csv}", flush=True)
    else:
        base_csv = _ensure_baselines()
    if not base_csv or not Path(base_csv).is_file():
        print(f"[highcuts] FATAL: sin baselines ({base_csv})", flush=True)
        return 1
    print(f"[highcuts] baselines -> {base_csv}", flush=True)
    _rebuild_index("baselines")

    acc_order = (True, False) if args.reverse else (False, True)
    # Varias pasadas: si un glitch SSH deja un bloque incompleto, la siguiente
    # pasada solo reintenta lo que falta (los completos se saltan via _done_set).
    MAX_PASSES = 4
    for pass_i in range(1, MAX_PASSES + 1):
        pending_any = False
        for flota in flotas:
            for acc in acc_order:
                done = _done_set()
                todo = [d for d in divs if (flota, acc, d) not in done]
                tag = f"P{pass_i} {flota} {'acc' if acc else 'noacc'} tcp"
                if not todo:
                    print(f"[highcuts] {tag}: ya completo, salto", flush=True)
                    continue
                pending_any = True
                print(f"[highcuts] {tag}: faltan {todo}", flush=True)
                _sweep(["--vehicles", flota,
                        "--partitions", ",".join(todo),
                        "--accidents", ("true" if acc else "false"),
                        "--transports", PROTOCOL,
                        "--no-baselines", "--baselines-from", base_csv],
                       tag, maxg=maxg)
                _rebuild_index(tag)
        if not pending_any:
            print(f"[highcuts] todo completo tras pasada {pass_i}", flush=True)
            break
        print(f"[highcuts] fin pasada {pass_i}; reviso huecos...", flush=True)

    if not args.no_equiv:
        _run_equivalence(base_csv)

    _stop.set()
    _rebuild_index("FINAL")
    print("[highcuts] TERMINADO barrido cortes altos Rotterdam", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
