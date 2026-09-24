"""Cosecha los environment_traffic.edgedata.xml POR WORKER de cada corte multicut
ya completado, recuperandolos de worker_runs/<job_id>/ en los nodos (no se borran
entre cortes) usando el mapeo label->sub_job_ids que conserva el master.

No re-simula nada y no interfiere con la campana en marcha. Es re-ejecutable: salta
los cortes que ya tienen distributed/workers/*.edgedata.xml.

    set VK_LAN_HOSTS=labrob19..30 ; set VK_LAN_MASTER=labrob19
    python -m SIMULATION.distributed.lan.harvest_edgedata
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.cluster import Cluster  # noqa: E402
from SIMULATION.distributed.lan.hosts import load_config  # noqa: E402
from SIMULATION.distributed.lan.ssh import Ssh  # noqa: E402

EQUIV = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"
KEY = str(Path.home() / ".ssh" / "id_ed25519")
EDGE_NAME = "environment_traffic.edgedata.xml"


def _partition_from_label(label: str) -> str:
    # "balanced-x-12_tcp_20000_noacc_mc" -> "balanced-x-12"
    return label.split("_tcp")[0]


def _run_dir_for(partition: str) -> Path | None:
    """Equivalence dir de este corte que tiene agregado pero le faltan edgedata."""
    cands = []
    for d in sorted(EQUIV.iterdir()):
        if not d.is_dir():
            continue
        mp = d / "meta.json"
        if not mp.is_file():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("partition") != partition or not m.get("_multicut"):
            continue
        agg = d / "distributed" / "aggregate_summary.json"
        if agg.is_file():
            cands.append(d)
    if not cands:
        return None
    return cands[-1]  # el mas reciente


def _index_edgedata(cfg, retries: int = 2) -> dict:
    """job_id -> (host, remote_path) buscando en worker_runs de todos los nodos.
    Reintenta hosts que fallen (p.ej. timeout por estar simulando)."""
    idx = {}
    rd = cfg.remote_dir
    pending = list(cfg.hosts)
    for attempt in range(retries + 1):
        failed = []
        for h in pending:
            try:
                with Ssh(h, cfg.user, key_filename=KEY, timeout=40) as s:
                    out = s.run(
                        f"find $HOME/{rd}/SIMULATION/distributed/worker_runs/ "
                        f"-name '{EDGE_NAME}' 2>/dev/null").out
                    for line in out.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        jid = Path(line).parent.name
                        idx.setdefault(jid, (h, line))
            except Exception as e:
                print(f"[harvest] {h} ERR indexando (intento {attempt + 1}): {e}",
                      flush=True)
                failed.append(h)
        if not failed:
            break
        pending = failed
    return idx


def main() -> int:
    cfg = load_config()
    cl = Cluster(cfg)
    st = cl.status()
    parents = st.get("parent_sims", {})

    print(f"[harvest] indexando edgedata en {len(cfg.hosts)} nodos...", flush=True)
    idx = _index_edgedata(cfg)
    print(f"[harvest] {len(idx)} edgedata.xml localizados en worker_runs", flush=True)

    n_ok = 0
    for pid, p in parents.items():
        label = (p.get("meta", {}) or {}).get("label", "")
        if not label.endswith("_mc"):
            continue
        if p.get("status") != "aggregated":
            print(f"[harvest] {label}: status={p.get('status')} -> aun no listo",
                  flush=True)
            continue
        part = _partition_from_label(label)
        run_dir = _run_dir_for(part)
        if run_dir is None:
            print(f"[harvest] {part}: sin run_dir con agregado -> salto", flush=True)
            continue
        wdir = run_dir / "distributed" / "workers"
        have = list(wdir.glob("*.edgedata.xml")) if wdir.is_dir() else []
        if len(have) >= len(p.get("sub_job_ids", [])) and have:
            print(f"[harvest] {part}: ya tiene {len(have)} edgedata -> salto",
                  flush=True)
            continue
        wdir.mkdir(parents=True, exist_ok=True)
        got = 0
        for jid in p.get("sub_job_ids", []):
            hit = idx.get(jid)
            if not hit:
                print(f"[harvest] {part}/{jid[:8]}: NO encontrado en nodos",
                      flush=True)
                continue
            host, remote = hit
            local = wdir / f"{host.split('.')[0]}_{jid}.edgedata.xml"
            if local.is_file() and local.stat().st_size > 0:
                got += 1
                continue
            try:
                with Ssh(host, cfg.user, key_filename=KEY, timeout=30) as s:
                    s.get(remote, str(local))
                got += 1
            except Exception as e:
                print(f"[harvest] {part}/{jid[:8]} fallo bajando de {host}: {e}",
                      flush=True)
        print(f"[harvest] {part}: {got}/{len(p.get('sub_job_ids', []))} edgedata "
              f"bajados -> {wdir}", flush=True)
        if got:
            n_ok += 1
    print(f"[harvest] TERMINADO. cortes con edgedata: {n_ok}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
