"""Recopila edgeData por segmento (save_environment) de VARIOS cortes REUSANDO
el baseline con edgeData ya existente (equivalence/20260710_163101, 20k sin
accidente, cap 14400). NO recalcula baseline.

Arranca master+workers UNA vez en el cluster actual (VK_LAN_HOSTS) y lanza cada
corte como run distribuido con edgeData, bajando los edgedata de los workers a
  equivalence/<ts>_<part>/distributed/workers/*.edgedata.xml  + meta.json

Al final genera la multigrafica (baseline vs todos los cortes).

    set VK_LAN_HOSTS=labrob19..30 ; set VK_LAN_MASTER=labrob19
    python -m SIMULATION.distributed.lan.run_equiv_multicut
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.cluster import Cluster, MASTER_PORT  # noqa: E402
from SIMULATION.distributed.lan.hosts import load_config  # noqa: E402
from SIMULATION.distributed.lan.matrix import trips_for  # noqa: E402

EQUIV = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"

ROAD = "rotterdam_arterial"
VEH = 20000          # debe casar con el baseline canonico existente
ACC = False
CAP = 14400
DEFAULT_CUTS = ["balanced-x-2", "balanced-x-4", "balanced-x-6",
                "balanced-x-8", "balanced-x-10", "balanced-x-12",
                "balanced-y-2", "balanced-y-4", "balanced-y-6",
                "balanced-y-8", "balanced-y-10", "balanced-y-12"]


def _done_parts() -> set:
    """Cortes que ya tienen distributed/workers/*.edgedata.xml en equivalence."""
    done = set()
    for d in EQUIV.iterdir():
        if not d.is_dir():
            continue
        mp = d / "meta.json"
        wdir = d / "distributed" / "workers"
        if mp.is_file() and wdir.is_dir() and list(wdir.glob("*.edgedata.xml")):
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
                if (str(m.get("vehicles")) == str(VEH)
                        and not m.get("accident")
                        and m.get("_multicut")):
                    done.add(m.get("partition"))
            except Exception:
                pass
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cuts", default=",".join(DEFAULT_CUTS))
    ap.add_argument("--dist-timeout", type=float, default=20000.0)
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args(argv)

    cuts = [c.strip() for c in args.cuts.split(",") if c.strip()]
    cfg = load_config()
    cl = Cluster(cfg)
    trips = trips_for(ROAD, VEH)

    already = _done_parts()
    todo = [c for c in cuts if c not in already]
    print(f"[equiv-mc] cluster={cfg.master} hosts={len(cfg.hosts)}", flush=True)
    print(f"[equiv-mc] ya hechos: {sorted(already)}", flush=True)
    print(f"[equiv-mc] a ejecutar: {todo}", flush=True)
    if not todo:
        print("[equiv-mc] nada que hacer", flush=True)
    else:
        print("[equiv-mc] limpiando remotos y arrancando master+workers...",
              flush=True)
        cl.stop_all()
        time.sleep(2)
        cl.start_master()
        if not cl.wait_master(40):
            print("[equiv-mc] FATAL: master no arranco", flush=True)
            return 1
        cl.start_workers()
        alive = cl.wait_workers(min(12, len(cfg.hosts)), 120)
        print(f"[equiv-mc] workers vivos={alive}", flush=True)

        for part in todo:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = EQUIV / f"{ts}_{part}"
            (out / "distributed").mkdir(parents=True, exist_ok=True)
            label = f"{part}_tcp_{VEH}_noacc_mc"
            payload = {
                "mode": "synced-spatial", "road": ROAD, "ratio": "99",
                "vehicles": VEH, "workers": int(part.split("-")[-1]),
                "partition": part, "barrier_transport": "tcp",
                "accident": ACC, "max_steps": CAP,
                "save_environment": True, "save_knowledge": False,
                "save_tripinfo": True,
                "sync_master_url": f"{cfg.master}:{MASTER_PORT}",
                "label": label,
            }
            print(f"[equiv-mc] === {part} submit ===", flush=True)
            td = time.time()
            res = cl.submit(payload)
            pid = res.get("parent_sim_id")
            if not pid:
                print(f"[equiv-mc] {part} FATAL submit: {res}", flush=True)
                continue
            p = cl.wait_parent(pid, timeout=args.dist_timeout, poll=10)
            wall = round(time.time() - td, 1)
            status = p.get("status", "timeout")
            agg = p.get("aggregated_dir")
            print(f"[equiv-mc] {part} status={status} wall={wall}s", flush=True)
            meta = {
                "ts": ts, "road": ROAD, "vehicles": VEH, "max_steps": CAP,
                "accident": ACC, "partition": part, "trips": trips,
                "workers": int(part.split("-")[-1]), "_multicut": True,
                "distributed": {"label": label, "status": status,
                                "wall_sec": wall,
                                "aggregated_dir_remote": agg},
            }
            if status == "aggregated" and agg:
                try:
                    cl.fetch_aggregated(agg, str(out / "distributed"))
                    nfiles = len(list((out / "distributed" / "workers")
                                      .glob("*.edgedata.xml")))
                    print(f"[equiv-mc] {part} edgedata workers={nfiles}",
                          flush=True)
                except Exception as ex:
                    print(f"[equiv-mc] {part} AVISO fetch: {ex}", flush=True)
            (out / "meta.json").write_text(json.dumps(meta, indent=2),
                                           encoding="utf-8")

        try:
            cl.stop_all()
        except Exception:
            pass

    if not args.no_plot:
        print("[equiv-mc] generando multigrafica...", flush=True)
        subprocess.run([sys.executable, "-u", "-m",
                        "SIMULATION.distributed.lan.plot_multicut_equivalence",
                        "--top", "8"], cwd=str(PROJECT_ROOT))
    print("[equiv-mc] TERMINADO", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
