"""Campana Rotterdam COMPLETA y con N REPLICAS. RESUMIBLE + rep-aware.

Objetivo: que CADA combinacion
    (flota, division, accidente, protocolo)
tenga al menos --reps ejecuciones 'aggregated' con speedup valido.

Barrido:
    flotas       {15k, 20k, 30k}
    divisiones   {y-2,y-3,y-4,y-6, x-2,x-3,x-4,x-6}
    accidente    {no, si}
    protocolos   --protocols (def: tcp,udp,http,zmq,grpc)
    horizonte    --cap steps (def 14400, comparable, reutiliza baselines)

Cuenta las replicas ya existentes en TODOS los results/*/benchmark.csv y, en
sucesivas PASADAS, solo lanza los combos a los que aun les faltan replicas. Cada
pasada anade como maximo 1 replica por combo (run_lan_sweep ejecuta cada
particion una vez). Se detiene cuando no falta ninguna o al llegar a --max-passes.

Baselines: se reutilizan via --baselines-from; si no se pasa, se calculan 6
baselines cap-CAP en ESTE cluster (mismas maquinas -> speedups validos) y se
cachean en results/_allreps_baseline.txt.

Un hilo regenera INDEX_simulaciones.csv cada 15 min.

    python -m SIMULATION.distributed.lan.run_rotterdam_all_reps --reps 3 \
        --protocols tcp,udp,http
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import threading
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
STATE_BASELINE = RESULTS / "_allreps_baseline.txt"

FLOTAS = ["15000", "20000", "30000"]
DIVS = ["balanced-y-2", "balanced-y-3", "balanced-y-4", "balanced-y-6",
        "balanced-x-2", "balanced-x-3", "balanced-x-4", "balanced-x-6"]
PROTOCOLS = ["tcp", "udp", "http", "zmq", "grpc"]

_stop = threading.Event()


# --------------------------------------------------------------- conteo replicas
def scan_counts() -> dict:
    counts = defaultdict(int)
    for f in RESULTS.rglob("benchmark.csv"):
        try:
            with f.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if (str(r.get("road", "")).find("rotterdam") >= 0
                            and r.get("status") == "aggregated"
                            and r.get("speedup") not in (None, "")
                            and r.get("partition")):
                        key = (str(r.get("vehicles")),
                               str(r.get("accident")).strip().lower() == "true",
                               r.get("partition"), r.get("transport"))
                        counts[key] += 1
        except Exception:
            continue
    return counts


# ------------------------------------------------------------------ index / logs
def _rebuild_index(tag: str = "") -> None:
    try:
        subprocess.run([sys.executable, "-m",
                        "SIMULATION.distributed.lan.build_index"],
                       cwd=str(ROOT), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if tag:
            print(f"[allreps] indice actualizado tras {tag}", flush=True)
    except Exception as ex:
        print(f"[allreps] fallo indice: {ex}", flush=True)


def _index_watcher():
    while not _stop.wait(900):
        _rebuild_index()


# ------------------------------------------------------------------- baselines
def _newest_results_dir(before: set) -> Path | None:
    dirs = {p for p in RESULTS.iterdir() if p.is_dir() and p.name[0].isdigit()}
    new = sorted(dirs - before, key=lambda p: p.stat().st_mtime)
    return new[-1] if new else None


def _baselines_ok(csv_path: Path, cap: str, flotas: list) -> bool:
    try:
        ok = set()
        with csv_path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if ("rotterdam" in str(r.get("road", ""))
                        and not r.get("partition")
                        and r.get("status") == "ok"
                        and str(r.get("max_steps")) in (cap, cap + ".0")):
                    ok.add((str(r.get("vehicles")),
                            str(r.get("accident")).strip().lower() == "true"))
        need = {(v, a) for v in flotas for a in (False, True)}
        return need.issubset(ok)
    except Exception:
        return False


def _ensure_baselines(cap: str, flotas: list) -> str:
    if STATE_BASELINE.is_file():
        p = Path(STATE_BASELINE.read_text(encoding="utf-8").strip())
        if p.is_file() and _baselines_ok(p, cap, flotas):
            print(f"[allreps] baselines de este cluster reutilizados: {p}",
                  flush=True)
            return str(p)
    print(f"[allreps] calculando {2*len(flotas)} baselines cap-{cap} en ESTE "
          "cluster (paralelo)...", flush=True)
    before = {p for p in RESULTS.iterdir() if p.is_dir()}
    _sweep(["--vehicles", ",".join(flotas),
            "--partitions", "balanced-y-2",
            "--accidents", "false,true",
            "--transports", "tcp",
            "--only-stage", "baseline"], cap, "BASELINES")
    d = _newest_results_dir(before)
    if d:
        csv_path = d / "benchmark.csv"
        try:
            STATE_BASELINE.write_text(str(csv_path), encoding="utf-8")
        except Exception:
            pass
        return str(csv_path)
    return ""


# --------------------------------------------------------------------- sweep
def _sweep(extra: list, cap: str, tag: str) -> int:
    base = [sys.executable, "-u", "-m",
            "SIMULATION.distributed.lan.run_lan_sweep",
            "--road", "rotterdam_arterial",
            "--max-steps", cap,
            "--accident-start", "7200", "--accident-duration", "3600",
            "--per-run-timeout", "30000", "--baseline-timeout", "40000",
            "--max-groups", "6", "--no-plots"]
    print(f"[allreps] {tag}:\n  " + " ".join(base + extra), flush=True)
    rc = subprocess.run(base + extra, cwd=str(ROOT)).returncode
    print(f"[allreps] {tag} rc={rc}", flush=True)
    return rc


def _remaining(counts: dict, reps: int, flotas: list, protos: list) -> int:
    miss = 0
    for flota in flotas:
        for acc in (False, True):
            for proto in protos:
                for d in DIVS:
                    miss += max(0, reps - counts.get((flota, acc, d, proto), 0))
    return miss


# ----------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--protocols", default=",".join(PROTOCOLS))
    ap.add_argument("--flotas", default=",".join(FLOTAS))
    ap.add_argument("--cap", default="14400")
    ap.add_argument("--baselines-from", default="")
    ap.add_argument("--max-passes", type=int, default=0,
                    help="0 = reps + 3 pasadas de gracia")
    ap.add_argument("--reverse", action="store_true")
    args = ap.parse_args(argv)

    protos = [p.strip() for p in args.protocols.split(",") if p.strip()]
    flotas = [v.strip() for v in args.flotas.split(",") if v.strip()]
    divs = list(DIVS)
    acc_order = [False, True]
    if args.reverse:
        flotas = flotas[::-1]
        divs = divs[::-1]
        protos = protos[::-1]
        acc_order = [True, False]
    max_passes = args.max_passes or (args.reps + 3)

    threading.Thread(target=_index_watcher, daemon=True).start()
    print(f"[allreps] Rotterdam {args.reps}x protos={protos} flotas={flotas} "
          f"cap={args.cap} max_passes={max_passes}", flush=True)

    if args.baselines_from and Path(args.baselines_from).is_file():
        base_csv = args.baselines_from
        print(f"[allreps] baselines dados: {base_csv}", flush=True)
    else:
        base_csv = _ensure_baselines(args.cap, flotas)
    if not base_csv or not Path(base_csv).is_file():
        print(f"[allreps] FATAL: sin baselines ({base_csv})", flush=True)
        return 1
    _rebuild_index("baselines")

    for p in range(1, max_passes + 1):
        counts = scan_counts()
        rem = _remaining(counts, args.reps, flotas, protos)
        print(f"\n[allreps] === PASADA {p}/{max_passes} — faltan {rem} runs ===",
              flush=True)
        if rem == 0:
            print("[allreps] matriz completa, nada que hacer", flush=True)
            break
        did_something = False
        for flota in flotas:
            for acc in acc_order:
                for proto in protos:
                    counts = scan_counts()
                    todo = [d for d in divs
                            if counts.get((flota, acc, d, proto), 0) < args.reps]
                    tag = f"p{p} {flota} {'acc' if acc else 'noacc'} {proto}"
                    if not todo:
                        continue
                    did_something = True
                    print(f"[allreps] {tag}: faltan replicas en {todo}",
                          flush=True)
                    _sweep(["--vehicles", flota,
                            "--partitions", ",".join(todo),
                            "--accidents", ("true" if acc else "false"),
                            "--transports", proto,
                            "--no-baselines", "--baselines-from", base_csv],
                           args.cap, tag)
                    _rebuild_index(tag)
        if not did_something:
            print("[allreps] nada pendiente en esta pasada", flush=True)
            break

    _stop.set()
    _rebuild_index("FINAL")
    counts = scan_counts()
    rem = _remaining(counts, args.reps, flotas, protos)
    print(f"[allreps] TERMINADO. Runs que aun faltan para {args.reps}x: {rem}",
          flush=True)
    return 0 if rem == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
