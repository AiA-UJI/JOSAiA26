"""Validacion de EQUIVALENCIA de flujo: secuencial (1 nodo) vs distribuido
(balanced-y-6, 6 nodos), MISMA flota / horizonte / mapa, con edgeData
(environment_traffic) ACTIVADO en ambos.

Objetivo: comprobar que el particionado no altera el trafico, comparando el
flujo por segmento en las aristas-NEXO (frontera/handoff) entre 1 y 6.

Uso:
    python -m SIMULATION.distributed.lan.run_rotterdam_equivalence
        [--vehicles 20000] [--max-steps 14400] [--partition balanced-y-6]
        [--accident]

Salida en SIMULATION/distributed/lan/equivalence/<ts>/:
    baseline/environment_traffic.json      (1 nodo)
    distributed/  (dir agregado del run de 6 cortes, con environment_traffic.json)
    meta.json
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.cluster import (  # noqa: E402
    Cluster, MASTER_PORT, LAN_DIR, _ssh)
from SIMULATION.distributed.lan.hosts import load_config  # noqa: E402
from SIMULATION.distributed.lan.matrix import trips_for  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicles", type=int, default=20000)
    ap.add_argument("--max-steps", type=int, default=14400)
    ap.add_argument("--partition", default="balanced-y-6")
    ap.add_argument("--road", default="rotterdam_arterial")
    ap.add_argument("--accident", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--baseline-timeout", type=float, default=32000.0)
    ap.add_argument("--dist-timeout", type=float, default=20000.0)
    args = ap.parse_args(argv)

    cfg = load_config()
    cl = Cluster(cfg)
    acc_tag = "acc" if args.accident else "noacc"
    trips = trips_for(args.road, args.vehicles)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = (PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" /
                "equivalence" / ts)
    (out_root / "baseline").mkdir(parents=True, exist_ok=True)
    print(f"[equiv] salida -> {out_root}", flush=True)
    print(f"[equiv] {args.road} {args.vehicles} veh, cap {args.max_steps} steps, "
          f"{acc_tag}, particion {args.partition}", flush=True)

    print("[equiv] limpiando procesos remotos...", flush=True)
    cl.stop_all()
    time.sleep(2)

    meta = {
        "ts": ts, "road": args.road, "vehicles": args.vehicles,
        "max_steps": args.max_steps, "accident": args.accident,
        "partition": args.partition, "workers": args.workers, "trips": trips,
    }

    # ---------- 1) BASELINE (1 nodo) con environment ----------
    base_sub = f"equiv_baseline_{args.vehicles}_{acc_tag}"
    host0 = cfg.hosts[0]
    print(f"[equiv] baseline (1 nodo) en {host0} con edgeData...", flush=True)
    t0 = time.time()
    b = cl.run_baseline(
        host0, trips, args.vehicles, args.accident, out_subdir=base_sub,
        max_steps=args.max_steps, timeout=args.baseline_timeout,
        road=args.road, save_environment=True)
    meta["baseline"] = {
        "host": host0, "runtime_sec": b.get("runtime_sec"),
        "wall_sec": b.get("wall_sec"), "rc": b.get("rc"),
        "remote_subdir": base_sub,
    }
    print(f"[equiv] baseline rc={b.get('rc')} runtime={b.get('runtime_sec')}s "
          f"wall={b.get('wall_sec')}s", flush=True)

    # traer environment_traffic.json del baseline
    try:
        s = _ssh(host0, cfg)
        remote_env = f"{cfg.remote_dir}/{LAN_DIR}/{base_sub}/environment_traffic.json"
        s.get(remote_env, str(out_root / "baseline" / "environment_traffic.json"))
        for extra in ("simulation_info.json", "trip_data.json"):
            try:
                s.get(f"{cfg.remote_dir}/{LAN_DIR}/{base_sub}/{extra}",
                      str(out_root / "baseline" / extra))
            except Exception:
                pass
        s.close()
        sz = (out_root / "baseline" / "environment_traffic.json").stat().st_size
        print(f"[equiv] baseline environment_traffic.json bajado ({sz} bytes)",
              flush=True)
    except Exception as ex:
        print(f"[equiv] AVISO: no pude bajar environment del baseline: {ex}",
              flush=True)

    # ---------- 2) DISTRIBUIDO (6 cortes) con environment ----------
    print("[equiv] arrancando master + workers...", flush=True)
    cl.start_master()
    if not cl.wait_master(40):
        print("[equiv] FATAL: master no arranco", flush=True)
        return 1
    cl.start_workers()
    alive = cl.wait_workers(min(args.workers, len(cfg.hosts)), 90)
    print(f"[equiv] workers vivos={alive}", flush=True)

    label = f"{args.partition}_tcp_{args.vehicles}_{acc_tag}_equiv"
    payload = {
        "mode": "synced-spatial",
        "road": args.road,
        "ratio": "99",
        "vehicles": args.vehicles,
        "workers": args.workers,
        "partition": args.partition,
        "barrier_transport": "tcp",
        "accident": args.accident,
        "max_steps": args.max_steps,
        "save_environment": True,      # <-- clave: guardar flujo por edge
        "save_knowledge": False,
        "save_tripinfo": True,
        "sync_master_url": f"{cfg.master}:{MASTER_PORT}",
        "label": label,
    }
    if args.accident:
        payload["accident_start"] = 7200
        payload["accident_duration"] = 3600

    print(f"[equiv] submit distribuido {label}...", flush=True)
    td = time.time()
    res = cl.submit(payload)
    pid = res.get("parent_sim_id")
    if not pid:
        print(f"[equiv] FATAL submit: {res}", flush=True)
        return 1
    p = cl.wait_parent(pid, timeout=args.dist_timeout, poll=10)
    wall = round(time.time() - td, 1)
    status = p.get("status", "timeout")
    agg_remote = p.get("aggregated_dir")
    meta["distributed"] = {
        "label": label, "status": status, "wall_sec": wall,
        "aggregated_dir_remote": agg_remote,
    }
    print(f"[equiv] distribuido status={status} wall={wall}s", flush=True)

    if status == "aggregated" and agg_remote:
        try:
            cl.fetch_aggregated(agg_remote, str(out_root / "distributed"))
            envp = out_root / "distributed" / "environment_traffic.json"
            sz = envp.stat().st_size if envp.is_file() else 0
            print(f"[equiv] distribuido agregado bajado; "
                  f"environment_traffic.json = {sz} bytes", flush=True)
        except Exception as ex:
            print(f"[equiv] AVISO fetch agregado: {ex}", flush=True)

    (out_root / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[equiv] meta -> {out_root / 'meta.json'}", flush=True)
    print(f"[equiv] TERMINADO. Baseline wall={meta['baseline']['wall_sec']}s, "
          f"dist wall={meta['distributed']['wall_sec']}s", flush=True)
    print(f"[equiv] Compara con: python -m SIMULATION.distributed.lan."
          f"compare_nexus_flows --run {out_root.name}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
