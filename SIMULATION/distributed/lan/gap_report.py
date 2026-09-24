"""Informe de huecos de la campana Rotterdam (solo lectura).

Escanea todos los results/*/benchmark.csv, cuenta cuantas REPLICAS 'aggregated'
con speedup valido hay por combinacion:
    (vehicles, accident, partition, transport)
y reporta cuanto falta para alcanzar --reps replicas en la matriz COMPLETA
    flotas x divisiones x accidente x protocolos.

    python -m SIMULATION.distributed.lan.gap_report --reps 3
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"

FLOTAS = ["15000", "20000", "30000"]
DIVS = ["balanced-y-2", "balanced-y-3", "balanced-y-4", "balanced-y-6",
        "balanced-x-2", "balanced-x-3", "balanced-x-4", "balanced-x-6"]
PROTOCOLS = ["tcp", "udp", "http", "zmq", "grpc"]


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3, help="replicas objetivo")
    ap.add_argument("--protocols", default=",".join(PROTOCOLS))
    ap.add_argument("--flotas", default=",".join(FLOTAS))
    ap.add_argument("--full", action="store_true", help="lista combo a combo")
    args = ap.parse_args(argv)

    protos = [p.strip() for p in args.protocols.split(",") if p.strip()]
    flotas = [v.strip() for v in args.flotas.split(",") if v.strip()]
    counts = scan_counts()

    print(f"Matriz: {len(flotas)} flotas x {len(DIVS)} divisiones x 2 acc x "
          f"{len(protos)} protocolos = "
          f"{len(flotas)*len(DIVS)*2*len(protos)} combos; objetivo {args.reps} reps\n")

    # resumen por protocolo
    total_runs_missing = 0
    print(f"{'proto':6} {'combos':>7} {'full':>6} {'part':>6} {'empty':>6} "
          f"{'runs_faltan':>12}")
    per_proto_missing = {}
    for proto in protos:
        full = part = empty = missing = 0
        for flota in flotas:
            for acc in (False, True):
                for d in DIVS:
                    c = counts.get((flota, acc, d, proto), 0)
                    if c >= args.reps:
                        full += 1
                    elif c > 0:
                        part += 1
                    else:
                        empty += 1
                    missing += max(0, args.reps - c)
        per_proto_missing[proto] = missing
        total_runs_missing += missing
        combos = len(flotas) * len(DIVS) * 2
        print(f"{proto:6} {combos:>7} {full:>6} {part:>6} {empty:>6} {missing:>12}")

    print(f"\nTOTAL runs de simulacion que faltan para {args.reps}x: "
          f"{total_runs_missing}")

    if args.full:
        print("\n--- detalle combos incompletos ---")
        for flota in flotas:
            for acc in (False, True):
                for proto in protos:
                    for d in DIVS:
                        c = counts.get((flota, acc, d, proto), 0)
                        if c < args.reps:
                            print(f"  {flota:6} {'acc' if acc else 'noacc':5} "
                                  f"{proto:5} {d:15} reps={c} "
                                  f"faltan={args.reps - c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
