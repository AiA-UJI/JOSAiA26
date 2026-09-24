"""Completa la validacion de equivalencia: espera a que termine el distribuido
(meta.json escrito por run_rotterdam_equivalence), luego lanza el BASELINE
(1 nodo, main.py ya arreglado, con edgeData) en la MISMA carpeta del run y
finalmente ejecuta la comparacion por nexos.

Uso:
    python -m SIMULATION.distributed.lan.finish_equivalence --run <ts>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.cluster import (  # noqa: E402
    Cluster, LAN_DIR, _ssh)
from SIMULATION.distributed.lan.hosts import load_config  # noqa: E402
from SIMULATION.distributed.lan.matrix import trips_for  # noqa: E402

EQUIV = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--baseline-timeout", type=float, default=32000.0)
    args = ap.parse_args(argv)

    run_dir = EQUIV / args.run
    meta_path = run_dir / "meta.json"

    # 1) esperar a que el distribuido termine (meta.json con distributed.status)
    print(f"[finish] esperando fin del distribuido en {run_dir.name}...",
          flush=True)
    while True:
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("distributed", {}).get("status"):
                    break
            except Exception:
                pass
        time.sleep(30)
    dist_env = run_dir / "distributed" / "environment_traffic.json"
    dist_ok = dist_env.is_file() and dist_env.stat().st_size > 2
    print(f"[finish] distribuido status={meta['distributed'].get('status')} "
          f"env_ok={dist_ok} "
          f"({dist_env.stat().st_size if dist_env.is_file() else 0} bytes)",
          flush=True)

    cfg = load_config()
    cl = Cluster(cfg)

    # 2) liberar maquinas (parar master/workers que dejo el run distribuido)
    print("[finish] parando master/workers para liberar CPU del baseline...",
          flush=True)
    cl.stop_all()
    time.sleep(3)

    # 3) baseline (1 nodo) con edgeData, main.py ya arreglado
    veh = int(meta["vehicles"])
    acc = bool(meta.get("accident"))
    acc_tag = "acc" if acc else "noacc"
    road = meta["road"]
    ms = int(meta["max_steps"])
    trips = trips_for(road, veh)
    base_sub = f"equiv_baseline_{veh}_{acc_tag}_fix"
    host0 = cfg.hosts[0]
    print(f"[finish] baseline (1 nodo) en {host0}, {veh} veh, cap {ms}...",
          flush=True)
    b = cl.run_baseline(host0, trips, veh, acc, out_subdir=base_sub,
                        max_steps=ms, timeout=args.baseline_timeout,
                        road=road, save_environment=True)
    print(f"[finish] baseline rc={b.get('rc')} runtime={b.get('runtime_sec')}s "
          f"wall={b.get('wall_sec')}s", flush=True)
    (run_dir / "baseline").mkdir(parents=True, exist_ok=True)
    try:
        s = _ssh(host0, cfg)
        base_remote = f"{cfg.remote_dir}/{LAN_DIR}/{base_sub}"
        s.get(f"{base_remote}/environment_traffic.json",
              str(run_dir / "baseline" / "environment_traffic.json"))
        for extra in ("simulation_info.json", "trip_data.json"):
            try:
                s.get(f"{base_remote}/{extra}",
                      str(run_dir / "baseline" / extra))
            except Exception:
                pass
        s.close()
        sz = (run_dir / "baseline" / "environment_traffic.json").stat().st_size
        print(f"[finish] baseline environment bajado ({sz} bytes)", flush=True)
    except Exception as ex:
        print(f"[finish] ERROR bajando baseline env: {ex}", flush=True)
        return 1

    meta["baseline_fix"] = {"runtime_sec": b.get("runtime_sec"),
                            "wall_sec": b.get("wall_sec"), "rc": b.get("rc"),
                            "remote_subdir": base_sub}
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # 4) comparacion por nexos
    print("[finish] comparando flujos por nexo...", flush=True)
    rc = subprocess.run(
        [sys.executable, "-m", "SIMULATION.distributed.lan.compare_nexus_flows",
         "--run", args.run], cwd=str(PROJECT_ROOT)).returncode
    print(f"[finish] compare rc={rc}. TERMINADO.", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
