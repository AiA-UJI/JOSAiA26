"""
CLI to submit a simulation (or a sweep) to a running master.

Examples
--------
Submit ONE simulation in spatial mode (one job per corridor group, 2 workers)::

    python -m SIMULATION.distributed submit \\
        --master 192.168.1.10:9000 \\
        --road modified_4ways_all --ratio 99 --workers 2 --mode spatial

Submit a 18-sim sweep in batch mode (1 job per sim, queued FIFO)::

    python -m SIMULATION.distributed submit \\
        --master 192.168.1.10:9000 --mode batch \\
        --sweep "ratio=10,50,99 sharing=none,v2v"

Hybrid (spatial + sweep) - each sim is split into W sub-jobs::

    python -m SIMULATION.distributed submit --mode spatial --workers 2 \\
        --sweep "ratio=10,50,99"

The ``--sweep`` mini-DSL accepts space-separated ``key=v1,v2,...`` pairs.
The cartesian product is enumerated and each combination becomes one
simulation submission.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.protocol import (  # noqa: E402
    DEFAULT_MASTER_PORT,
    EP_SUBMIT,
    SimMode,
)


def _normalise_master(url: str) -> str:
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    bare = url.split("://", 1)[1]
    if ":" not in bare:
        url = url + f":{DEFAULT_MASTER_PORT}"
    return url.rstrip("/")


def _post_submit(master_url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        master_url + EP_SUBMIT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=60.0) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as e:
        raise SystemExit(f"[submit] master returned {e.code}: {e.read().decode()}")
    except URLError as e:
        raise SystemExit(f"[submit] master unreachable: {e}")


def _parse_sweep(sweep: str) -> List[Dict[str, str]]:
    """Parse a ``key=v1,v2 key2=a,b`` string into a list of dicts (cartesian)."""
    if not sweep:
        return [{}]
    keys: List[str] = []
    values: List[List[str]] = []
    for token in sweep.split():
        if "=" not in token:
            raise SystemExit(f"[submit] bad sweep token: {token}")
        k, vs = token.split("=", 1)
        keys.append(k.strip())
        values.append([v.strip() for v in vs.split(",") if v.strip()])
    if not keys:
        return [{}]
    out: List[Dict[str, str]] = []
    for combo in itertools.product(*values):
        out.append(dict(zip(keys, combo)))
    return out


def _make_payload(args, sweep_overrides: Dict[str, str]) -> dict:
    sharing_raw = sweep_overrides.get("sharing", args.sharing)
    sharing_list = [s.strip() for s in str(sharing_raw).split(",") if s.strip()]

    payload = {
        "mode": args.mode,
        "road": sweep_overrides.get("road", args.road),
        "ratio": str(sweep_overrides.get("ratio", args.ratio)),
        "sharing": sharing_list or ["none"],
        "accident": args.accident or sweep_overrides.get("accident", "0") in ("1", "true", "True"),
        "accident_start": args.accident_start,
        "accident_duration": args.accident_duration,
        "force_cv": args.force_cv,
        "vehicles": args.vehicles,
        "save_environment": args.save_environment,
        "save_knowledge": args.save_knowledge,
        "save_tripinfo": args.save_tripinfo,
        "max_steps": args.max_steps,
        "workers": args.workers,
        "trips": sweep_overrides.get("trips", args.trips),
        "label": args.label or f"{args.road}/{sweep_overrides.get('ratio', args.ratio)}",
    }
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Submit simulation(s) to a master")
    parser.add_argument("--master", required=True, help="master host:port")
    parser.add_argument("--mode",
                        choices=[SimMode.BATCH.value,
                                 SimMode.SPATIAL.value,
                                 SimMode.SYNCED_SPATIAL.value],
                        default=SimMode.BATCH.value)
    parser.add_argument("--road", default="modified_4ways_all")
    parser.add_argument("--ratio", default="99")
    parser.add_argument("--sharing", default="none",
                        help="Comma-separated sharing modes (e.g. none or none,v2v)")
    parser.add_argument("--accident", action="store_true")
    parser.add_argument("--accident-start", type=int, default=None)
    parser.add_argument("--accident-duration", type=int, default=None)
    parser.add_argument("--force-cv", action="store_true")
    parser.add_argument("--vehicles", type=int, default=None)
    parser.add_argument("--save-environment", action="store_true", default=True)
    parser.add_argument("--no-save-environment", action="store_false", dest="save_environment")
    parser.add_argument("--save-knowledge", action="store_true", default=False)
    parser.add_argument("--save-tripinfo", action="store_true", default=True)
    parser.add_argument("--no-save-tripinfo", action="store_false", dest="save_tripinfo")
    parser.add_argument("--max-steps", type=int, default=0)
    parser.add_argument("--workers", type=int, default=2,
                        help="Number of workers for spatial split (2/3/4)")
    parser.add_argument("--label", default=None)
    parser.add_argument("--trips", default=None,
                        help="Trips filename resolved locally on each worker "
                             "(main.py --trips). Batch mode; overrides ratio-based "
                             "resolution for the OD/departures while the smart %% "
                             "is still applied at runtime by veh-id hash.")
    parser.add_argument("--sweep", default="",
                        help="Sweep DSL: 'ratio=10,50,99 sharing=none,v2v'")
    args = parser.parse_args(argv)

    master_url = _normalise_master(args.master)
    combos = _parse_sweep(args.sweep)

    print(f"[submit] {len(combos)} simulation(s) to submit -> {master_url}")
    for i, ov in enumerate(combos, start=1):
        payload = _make_payload(args, ov)
        resp = _post_submit(master_url, payload)
        print(
            f"[submit] {i}/{len(combos)} mode={payload['mode']} "
            f"road={payload['road']} ratio={payload['ratio']} "
            f"-> jobs={resp.get('count')} parent={resp.get('parent_sim_id')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
