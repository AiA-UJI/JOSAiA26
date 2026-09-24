"""Campana Rotterdam de una semana: TODOS los protocolos, TODA la matriz.

Reparte el cluster en varios grupos INDEPENDIENTES que trabajan en paralelo,
cada uno con su propio master y su propio subconjunto de maquinas. El tamano de
cada grupo se elige para que encaje con el numero de particiones de los trabajos
que ejecuta (una particion = una maquina), de modo que no se desperdicie
hardware: un trabajo de 6 particiones va a un grupo de 6 maquinas, uno de 2 a un
grupo de 2.

Matriz objetivo
    flotas       {15000, 20000, 30000}
    divisiones   {y,x} x {2,3,4,6}
    accidente    {no, si}
    protocolos   {tcp, udp, http, zmq, grpc}
    horizonte    14400 pasos, accidente en 7200 + 3600  (igual que la campana
                 de julio, para que los speedups sean comparables)

Un "trabajo" es (flota, accidente, protocolo, num_grupos) e incluye las dos
orientaciones (balanced-y-N y balanced-x-N) que sigan faltando, porque
run_lan_sweep las ejecuta seguidas reutilizando el mismo cluster y asi se
amortiza el arranque.

Prioridad: primero completar 1 replica de TODA la matriz, luego la 2a, luego la
3a. Dentro de cada nivel se van cogiendo primero los trabajos baratos, de forma
que si el plazo se agota lo que falte sea lo menos posible.

Plazo: no se lanza ningun trabajo cuya duracion estimada se pase de --deadline.

Es reanudable: el progreso se deduce releyendo los benchmark.csv, asi que se
puede matar y relanzar sin perder nada.

    python -m SIMULATION.distributed.lan.run_campaign_week \
        --deadline "2026-08-28 18:00" --reps 3
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
WORK = RESULTS / "_week"

FLEETS = ["15000", "20000", "30000"]
NGROUPS = [2, 3, 4, 6]
PROTOCOLS = ["tcp", "udp", "http", "zmq", "grpc"]

# Mediana medida en la campana de julio (segundos por run, horizonte 14400).
# Se usa solo para estimar duraciones y respetar el plazo.
MEDIAN_SEC = {
    ("15000", 2): 10630, ("15000", 3): 8100, ("15000", 4): 6605, ("15000", 6): 3905,
    ("20000", 2): 12350, ("20000", 3): 10385, ("20000", 4): 7060, ("20000", 6): 4770,
    ("30000", 2): 18280, ("30000", 3): 15170, ("30000", 4): 10375, ("30000", 6): 7030,
}
STARTUP_SEC = 300           # arranque/parada del cluster por invocacion
SAFETY = 1.35               # margen sobre la mediana (hay colas larguisimas)

_lock = threading.Lock()
_claimed: set = set()       # trabajos en vuelo (no estan aun en los CSV)
_attempts: dict = defaultdict(int)
_done_jobs: list = []
_stop = threading.Event()


# ------------------------------------------------------------------ inventario
def scan_counts() -> dict:
    """Replicas validas por (flota, accidente, particion, protocolo)."""
    counts = defaultdict(int)
    for f in RESULTS.rglob("benchmark.csv"):
        try:
            with f.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if ("rotterdam" in str(r.get("road", ""))
                            and r.get("status") == "aggregated"
                            and r.get("speedup") not in (None, "")
                            and r.get("partition")):
                        counts[(str(r.get("vehicles")),
                                str(r.get("accident")).strip().lower() == "true",
                                r.get("partition"),
                                r.get("transport"))] += 1
        except Exception:
            continue
    return counts


def job_key(fleet: str, acc: bool, proto: str, ng: int) -> tuple:
    return (fleet, acc, proto, ng)


def build_queue(counts: dict, reps: int, protos: list, fleets: list) -> list:
    """Trabajos pendientes ordenados por prioridad (replica, coste)."""
    jobs = []
    for target in range(1, reps + 1):
        level = []
        for fleet in fleets:
            for ng in sorted(NGROUPS, reverse=True):     # los baratos primero
                for proto in protos:
                    for acc in (False, True):
                        divs = [d for d in (f"balanced-y-{ng}",
                                            f"balanced-x-{ng}")
                                if counts.get((fleet, acc, d, proto), 0) < target]
                        if not divs:
                            continue
                        est = len(divs) * MEDIAN_SEC[(fleet, ng)] * SAFETY
                        level.append({
                            "fleet": fleet, "acc": acc, "proto": proto,
                            "ng": ng, "divs": divs, "target": target,
                            "est_sec": est + STARTUP_SEC,
                        })
        level.sort(key=lambda j: j["est_sec"])
        jobs.extend(level)
    return jobs


# ---------------------------------------------------------------------- sweeps
def run_job(job: dict, hosts: list, gname: str, baselines: str,
            cap: str, log: Path) -> int:
    env = dict(os.environ)
    env["VK_LAN_HOSTS"] = ",".join(hosts)
    env["VK_LAN_MASTER"] = hosts[0]
    env["PYTHONIOENCODING"] = "utf-8"
    tag = (f"{gname}_{job['proto']}_{job['fleet']}_"
           f"{'acc' if job['acc'] else 'noacc'}_n{job['ng']}")
    cmd = [sys.executable, "-u", "-m", "SIMULATION.distributed.lan.run_lan_sweep",
           "--road", "rotterdam_arterial",
           "--max-steps", cap,
           "--accident-start", "7200", "--accident-duration", "3600",
           "--vehicles", job["fleet"],
           "--partitions", ",".join(job["divs"]),
           "--accidents", "true" if job["acc"] else "false",
           "--transports", job["proto"],
           "--max-groups", str(job["ng"]),
           "--per-run-timeout", "39600",
           "--no-baselines", "--baselines-from", baselines,
           "--no-plots", "--no-report",
           "--run-tag", tag]
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{'='*78}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {tag}\n"
                 f"hosts={hosts}\n{' '.join(cmd)}\n{'='*78}\n")
        fh.flush()
        return subprocess.run(cmd, cwd=str(ROOT), env=env,
                              stdout=fh, stderr=subprocess.STDOUT).returncode


def group_worker(gname: str, hosts: list, deadline: float, reps: int,
                 protos: list, fleets: list, baselines: str, cap: str) -> None:
    size = len(hosts)
    log = WORK / f"{gname}.log"
    print(f"[{gname}] arranca con {size} maquinas: "
          f"{','.join(h.split('.')[0] for h in hosts)}", flush=True)

    while not _stop.is_set():
        now = time.time()
        if now >= deadline:
            print(f"[{gname}] plazo agotado, paro", flush=True)
            return

        with _lock:
            counts = scan_counts()
            queue = build_queue(counts, reps, protos, fleets)
            pick = None
            # Preferimos el trabajo mas grande que quepa en el grupo (asi un
            # grupo de 6 no malgasta maquinas en un trabajo de 2), y que ademas
            # entre en el plazo.
            for want_ng in sorted([n for n in NGROUPS if n <= size],
                                  reverse=True):
                for j in queue:
                    if j["ng"] != want_ng:
                        continue
                    k = job_key(j["fleet"], j["acc"], j["proto"], j["ng"])
                    if k in _claimed or _attempts[k] >= 2:
                        continue
                    if now + j["est_sec"] > deadline:
                        continue
                    pick = j
                    break
                if pick:
                    break
            if pick:
                _claimed.add(job_key(pick["fleet"], pick["acc"],
                                     pick["proto"], pick["ng"]))

        if not pick:
            # o no queda trabajo, o lo que queda no cabe en el plazo
            remaining = [j for j in queue
                         if job_key(j["fleet"], j["acc"], j["proto"],
                                    j["ng"]) not in _claimed]
            if not remaining:
                print(f"[{gname}] matriz completa, paro", flush=True)
                return
            if _stop.wait(300):
                return
            continue

        k = job_key(pick["fleet"], pick["acc"], pick["proto"], pick["ng"])
        eta = datetime.fromtimestamp(now + pick["est_sec"])
        print(f"[{gname}] -> rep{pick['target']} {pick['proto']} "
              f"{pick['fleet']} {'acc' if pick['acc'] else 'noacc'} "
              f"{pick['divs']} (est {pick['est_sec']/3600:.1f} h, "
              f"fin ~{eta:%d/%m %H:%M})", flush=True)
        t0 = time.time()
        try:
            rc = run_job(pick, hosts, gname, baselines, cap, log)
        except Exception as ex:
            rc = -1
            print(f"[{gname}] EXCEPCION: {ex}", flush=True)
        dt = (time.time() - t0) / 3600

        with _lock:
            _claimed.discard(k)
            after = scan_counts()
            ok = all(after.get((pick["fleet"], pick["acc"], d,
                                pick["proto"]), 0) >= pick["target"]
                     for d in pick["divs"])
            if not ok:
                _attempts[k] += 1
            _done_jobs.append({
                "job": f"{pick['proto']} {pick['fleet']} "
                       f"{'acc' if pick['acc'] else 'noacc'} n{pick['ng']}",
                "group": gname, "rc": rc, "horas": round(dt, 2),
                "ok": ok, "fin": datetime.now().isoformat(timespec="seconds"),
            })
        print(f"[{gname}] <- rc={rc} en {dt:.2f} h "
              f"{'OK' if ok else 'INCOMPLETO'}", flush=True)


# ------------------------------------------------------------------ vigilancia
def rebuild_index() -> None:
    subprocess.run([sys.executable, "-m", "SIMULATION.distributed.lan.build_index"],
                   cwd=str(ROOT), check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def progress_snapshot(reps: int, protos: list, fleets: list,
                      deadline: float) -> None:
    counts = scan_counts()
    lines = [f"actualizado: {datetime.now():%Y-%m-%d %H:%M:%S}",
             f"plazo      : {datetime.fromtimestamp(deadline):%Y-%m-%d %H:%M}",
             ""]
    total_cells = len(fleets) * len(NGROUPS) * 2 * 2 * len(protos)
    for target in (1, 2, 3):
        if target > reps:
            break
        done = 0
        for fleet in fleets:
            for ng in NGROUPS:
                for orient in ("y", "x"):
                    for acc in (False, True):
                        for proto in protos:
                            if counts.get((fleet, acc,
                                           f"balanced-{orient}-{ng}",
                                           proto), 0) >= target:
                                done += 1
        lines.append(f"replica {target}: {done}/{total_cells} celdas "
                     f"({100*done/total_cells:.1f} %)")
    lines.append("")
    lines.append(f"{'proto':6} {'1x':>6} {'2x':>6} {'3x':>6}")
    for proto in protos:
        row = []
        for target in (1, 2, 3):
            d = sum(1 for fleet in fleets for ng in NGROUPS
                    for orient in ("y", "x") for acc in (False, True)
                    if counts.get((fleet, acc, f"balanced-{orient}-{ng}",
                                   proto), 0) >= target)
            row.append(d)
        lines.append(f"{proto:6} {row[0]:>6} {row[1]:>6} {row[2]:>6}")
    lines.append("")
    lines.append("trabajos terminados en esta sesion:")
    for d in _done_jobs[-40:]:
        lines.append(f"  {d['fin']}  {d['group']:4} {d['job']:34} "
                     f"{d['horas']:>6.2f} h  {'OK' if d['ok'] else 'FALLO'}")
    (WORK / "PROGRESO.txt").write_text("\n".join(lines), encoding="utf-8")
    (WORK / "estado.json").write_text(
        json.dumps({"done": _done_jobs,
                    "claimed": [list(k) for k in _claimed],
                    "ts": datetime.now().isoformat()}, indent=1),
        encoding="utf-8")


def maintainer(reps: int, protos: list, fleets: list, deadline: float,
               every: int) -> None:
    while not _stop.wait(every):
        try:
            rebuild_index()
            progress_snapshot(reps, protos, fleets, deadline)
            for mod in ("build_rotterdam_tables", "build_speedup_table"):
                subprocess.run([sys.executable, "-m",
                                f"SIMULATION.distributed.lan.{mod}"],
                               cwd=str(ROOT), check=False,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        except Exception as ex:
            print(f"[mant] {ex}", flush=True)


# ---------------------------------------------------------------------- grupos
def make_groups(hosts: list, sizes: list) -> list:
    out, i = [], 0
    for n, size in enumerate(sizes, 1):
        if i + size > len(hosts):
            break
        out.append((f"G{n}x{size}", hosts[i:i + size]))
        i += size
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hosts", default=",".join(
        f"labrob{n:02d}.act.uji.es" for n in range(8, 30)),
        help="labrob07 esta cedida y labrob30 tiene 12 nucleos: fuera")
    ap.add_argument("--sizes", default="6,4,3,3,2,2,2",
                    help="tamano de cada grupo concurrente")
    ap.add_argument("--deadline", default="2026-08-28 18:00")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--protocols", default=",".join(PROTOCOLS))
    ap.add_argument("--fleets", default=",".join(FLEETS))
    ap.add_argument("--cap", default="14400")
    ap.add_argument("--baselines-from", default=str(
        RESULTS / "20260713_123725" / "benchmark.csv"))
    ap.add_argument("--maintain-every", type=int, default=1200)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    protos = [p.strip() for p in args.protocols.split(",") if p.strip()]
    fleets = [f.strip() for f in args.fleets.split(",") if f.strip()]
    deadline = datetime.strptime(args.deadline, "%Y-%m-%d %H:%M").timestamp()
    if not Path(args.baselines_from).is_file():
        print(f"FATAL: no encuentro baselines en {args.baselines_from}")
        return 1

    WORK.mkdir(parents=True, exist_ok=True)
    groups = make_groups(hosts, sizes)

    counts = scan_counts()
    queue = build_queue(counts, args.reps, protos, fleets)
    total_h = sum(j["est_sec"] for j in queue) / 3600
    horas = (deadline - time.time()) / 3600
    capacidad = sum(1 for _ in groups) * horas

    print(f"[semana] {len(hosts)} maquinas en {len(groups)} grupos "
          f"{[g[0] for g in groups]}")
    print(f"[semana] plazo {args.deadline} -> {horas:.1f} h por delante")
    print(f"[semana] pendiente hasta {args.reps}x: {len(queue)} trabajos, "
          f"{total_h:.0f} h de computo secuencial")
    print(f"[semana] capacidad aprox: {capacidad:.0f} h de grupo")
    for target in range(1, args.reps + 1):
        h = sum(j["est_sec"] for j in queue if j["target"] == target) / 3600
        n = sum(1 for j in queue if j["target"] == target)
        print(f"[semana]   replica {target}: {n:>3} trabajos, {h:>6.0f} h")
    if args.dry_run:
        for j in queue[:40]:
            print(f"   rep{j['target']} {j['proto']:5} {j['fleet']:>6} "
                  f"{'acc' if j['acc'] else 'noacc':5} n{j['ng']} "
                  f"{j['est_sec']/3600:>5.1f} h  {j['divs']}")
        return 0

    threading.Thread(target=maintainer,
                     args=(args.reps, protos, fleets, deadline,
                           args.maintain_every), daemon=True).start()

    threads = []
    for gname, ghosts in groups:
        t = threading.Thread(target=group_worker,
                             args=(gname, ghosts, deadline, args.reps, protos,
                                   fleets, args.baselines_from, args.cap),
                             daemon=False)
        t.start()
        threads.append(t)
        time.sleep(20)      # arranques escalonados: no saturar el SSH

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        _stop.set()
        print("[semana] interrumpido por el usuario")

    _stop.set()
    rebuild_index()
    progress_snapshot(args.reps, protos, fleets, deadline)
    counts = scan_counts()
    left = build_queue(counts, args.reps, protos, fleets)
    print(f"[semana] TERMINADO. Trabajos que siguen faltando para "
          f"{args.reps}x: {len(left)}")
    print(f"[semana] progreso en {WORK / 'PROGRESO.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
