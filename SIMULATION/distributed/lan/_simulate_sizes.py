"""Simula el planificador con distintos repartos de grupos y mide que se acaba.

Replica la logica de run_campaign_week.group_worker (coger el trabajo mas grande
que quepa en el grupo, respetando prioridad de replica y el plazo) sobre las
duraciones estimadas, y reporta cuantas celdas de la matriz quedan cubiertas.
"""
from __future__ import annotations

import heapq
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from SIMULATION.distributed.lan.run_campaign_week import (  # noqa: E402
    NGROUPS, build_queue, scan_counts,
)

PROTOS = ["tcp", "udp", "http", "zmq", "grpc"]
FLEETS = ["15000", "20000", "30000"]
HORIZON_H = 103.5

CANDIDATES = [
    "6,4,3,3,2,2,2",
    "6,4,4,3,3,2",
    "6,6,4,3,3",
    "6,4,4,4,2,2",
    "6,4,4,3,2,2",
    "6,6,4,4,2",
    "4,4,4,3,3,2,2",
    "6,4,4,2,2,2,2",
    "6,6,3,3,2,2",
]


def simulate(sizes: list, queue: list, horizon_h: float):
    """Devuelve (celdas_hechas, horas_maquina_utiles, detalle por replica)."""
    jobs = [dict(j) for j in queue]
    for j in jobs:
        j["taken"] = False
    # heap de grupos: (hora_libre, idx)
    free = [(0.0, i) for i in range(len(sizes))]
    heapq.heapify(free)
    done = []
    busy_mh = 0.0
    while free:
        t, gi = heapq.heappop(free)
        if t >= horizon_h:
            continue
        size = sizes[gi]
        pick = None
        for want in sorted([n for n in NGROUPS if n <= size], reverse=True):
            for j in jobs:
                if j["taken"] or j["ng"] != want:
                    continue
                dur = j["est_sec"] / 3600
                if t + dur > horizon_h:
                    continue
                pick = j
                break
            if pick:
                break
        if not pick:
            continue
        pick["taken"] = True
        dur = pick["est_sec"] / 3600
        busy_mh += dur * pick["ng"]
        done.append(pick)
        heapq.heappush(free, (t + dur, gi))
    return done, busy_mh, jobs


def main() -> int:
    counts = scan_counts()
    queue = build_queue(counts, 3, PROTOS, FLEETS)
    rep1 = [j for j in queue if j["target"] == 1]
    print(f"pendiente: {len(queue)} trabajos "
          f"({len(rep1)} de la replica 1)\n")
    print(f"{'reparto':22} {'grupos':>7} {'maq':>4} {'rep1':>10} {'rep2':>10} "
          f"{'rep3':>7} {'uso':>6}")
    best = None
    for spec in CANDIDATES:
        sizes = [int(s) for s in spec.split(",")]
        maq = sum(sizes)
        if maq > 22:
            continue
        done, busy, allj = simulate(sizes, queue, HORIZON_H)
        n1 = sum(1 for j in done if j["target"] == 1)
        n2 = sum(1 for j in done if j["target"] == 2)
        n3 = sum(1 for j in done if j["target"] == 3)
        uso = busy / (22 * HORIZON_H) * 100
        score = (n1 * 1000 + n2 * 10 + n3)
        print(f"{spec:22} {len(sizes):>7} {maq:>4} "
              f"{n1:>4}/{len(rep1):<5} {n2:>10} {n3:>7} {uso:>5.0f}%")
        if best is None or score > best[0]:
            best = (score, spec, allj, n1, n2, n3)

    score, spec, allj, n1, n2, n3 = best
    print(f"\nMEJOR REPARTO: {spec}")
    print(f"  replica 1 completada: {n1}/{len(rep1)} trabajos")
    faltan = [j for j in allj if j["target"] == 1 and not j["taken"]]
    if faltan:
        print("  quedaria fuera de la replica 1:")
        for j in faltan:
            print(f"    {j['proto']:5} {j['fleet']:>6} "
                  f"{'acc' if j['acc'] else 'noacc':5} n{j['ng']} "
                  f"{j['est_sec']/3600:>5.1f} h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
