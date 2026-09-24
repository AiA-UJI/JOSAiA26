"""
Synced-spatial runtime helpers (consumed by ``SIMULATION.main``).

Glue layer between a running TraCI session and the master's HTTP barrier.
Encapsulates the per-step ``handoff`` cycle so ``main.py`` only has to call
two methods inside its main loop.

Wire format of one handoff (sent in ``outgoing`` and received in ``incoming``):

    {
        "vid":        str,    # SUMO vehicle id
        "vtype":      str,    # type id (e.g. "intelligent" or "normal")
        "edges":      [str],  # remaining route edges, starting at the current edge
        "lane_id":    str,    # current lane id (e.g. "467787295_0")
        "lane_pos":   float,  # position along the lane (m)
        "speed":      float,  # current speed (m/s)
        "depart":     float,  # original depart time (kept for diagnostics)
        "color":      [r,g,b,a] | None,
        "src_group":  str,    # group that sent the handoff
        "dest_group": str,    # group that should receive it (None = broadcast)
    }

The receiving worker materialises the vehicle via
``traci.route.add`` + ``traci.vehicle.add`` + ``traci.vehicle.moveTo``.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError


# ==================== Master URL helpers ====================

def _normalise_master_url(url: str) -> str:
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    return url.rstrip("/")


def _http_post_json(url: str, payload: dict, timeout: float) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))


def _http_get_json(url: str, timeout: float) -> Any:
    with urllib_request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ==================== Stats ====================

@dataclass
class SyncStats:
    handoffs_out: int = 0
    handoffs_in: int = 0
    barrier_calls: int = 0
    barrier_timeouts: int = 0
    barrier_timeout_total_sec: float = 0.0
    barrier_total_sec: float = 0.0
    handoff_failures: int = 0
    self_disabled_at_step: int = -1
    transport: str = "http"
    boot_at: float = field(default_factory=time.time)


# ==================== SyncBridge ====================

class SyncBridge:
    """Per-worker bridge to the master's sync barrier.

    Lifecycle::

        bridge = SyncBridge(master_url, parent_sim_id, group, peers)
        bridge.boot()        # downloads edge_owner.json
        # ... start TraCI ...
        for step in loop:
            traci.simulationStep()
            bridge.exchange(traci, step)   # handoff + barrier
        bridge.finish()
    """

    SHARED_LABEL = "SHARED"

    def __init__(self,
                 master_url: str,
                 parent_sim_id: str,
                 group: str,
                 peers: List[str],
                 barrier_timeout: float = 120.0,
                 verbose: bool = True):
        self.master_url = _normalise_master_url(master_url)
        self.parent_sim_id = parent_sim_id
        self.group = group
        self.peers: Set[str] = set(peers or [])
        self.barrier_timeout = barrier_timeout
        self.verbose = verbose

        self.edge_owner: Dict[str, str] = {}
        self.stats = SyncStats()
        self._finished = False
        # Pluggable barrier transport client (tcp/udp/zmq/grpc/http). Falls
        # back to an HTTP client when the preferred transport can't connect.
        self._client = None
        self._transport = "http"
        self._barrier_host: Optional[str] = None
        self._barrier_port: Optional[int] = None
        # vid -> last step we shipped this vid out, to suppress immediate
        # re-handoff churn when a vehicle straddles the boundary.
        self._recent_outgoing: Dict[str, int] = {}
        # vid -> step at which we created it via incoming handoff. We refuse
        # to re-handoff such vids for a few steps to avoid ping-pong.
        self._recent_incoming: Dict[str, int] = {}
        # Cooldown (in steps) before a vid we just handled can be re-routed.
        self.handoff_cooldown_steps = 5
        # If too many consecutive barriers time out we assume all peers are
        # dead and degrade to a non-synced local sim so we don't waste hours
        # waiting on every step. The vehicles that should have crossed are
        # simply removed (vaporized=traci) which is also what would happen
        # if the peer never came back in real life.
        self._consecutive_timeouts = 0
        self._max_consecutive_timeouts = 3
        self._disabled = False

    # ---------- helpers ----------

    def _master_host(self) -> str:
        url = self.master_url.split("://", 1)[-1]
        return url.split(":", 1)[0]

    def _master_port(self) -> int:
        url = self.master_url.split("://", 1)[-1]
        try:
            return int(url.split(":", 1)[1])
        except (IndexError, ValueError):
            return 80

    def _make_http_client(self):
        from SIMULATION.distributed.transports import make_client
        c = make_client("http", self._master_host(), self._master_port(),
                        self.parent_sim_id, self.group,
                        timeout=self.barrier_timeout)
        c.connect()
        return c

    def _connect_transport(self, transport: str) -> None:
        """Connect ``transport``; fall back to HTTP on any failure."""
        from SIMULATION.distributed.transports import make_client
        if transport in ("http", None) or not self._barrier_port:
            self._client = self._make_http_client()
            self._transport = "http"
        else:
            try:
                c = make_client(
                    transport, self._barrier_host, int(self._barrier_port),
                    self.parent_sim_id, self.group, timeout=30.0)
                c.connect()
                self._client = c
                self._transport = transport
                if self.verbose:
                    print(f"[sync] barrier transport '{transport}' connected "
                          f"-> {self._barrier_host}:{self._barrier_port}",
                          flush=True)
            except Exception as e:
                print(f"[sync] WARN: transport '{transport}' connect failed "
                      f"({e}); falling back to HTTP barrier", flush=True)
                self._client = self._make_http_client()
                self._transport = "http"
        self.stats.transport = self._transport

    # ---------- boot ----------

    def boot(self) -> None:
        """Download the edge ownership map from the master."""
        url = f"{self.master_url}/api/sync/edges/{self.parent_sim_id}"
        try:
            data = _http_get_json(url, timeout=30.0)
        except (HTTPError, URLError, OSError) as e:
            raise RuntimeError(
                f"[sync] failed to fetch edge_owner from {url}: {e}"
            ) from e
        self.edge_owner = data.get("edge_owner") or {}
        if not self.edge_owner:
            raise RuntimeError(
                f"[sync] empty edge_owner for parent {self.parent_sim_id}"
            )

        # Select the barrier transport advertised by the master (default tcp);
        # connect, falling back to an HTTP client if it can't be opened.
        self._barrier_port = data.get("barrier_port")
        self._barrier_host = data.get("barrier_host") or self._master_host()
        transport = (data.get("barrier_transport") or "tcp").lower()
        self._connect_transport(transport)

        if self.verbose:
            counts: Dict[str, int] = {}
            for owner in self.edge_owner.values():
                counts[owner] = counts.get(owner, 0) + 1
            summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"[sync] booted as group={self.group} peers={sorted(self.peers)} "
                  f"edges={len(self.edge_owner)} ({summary})", flush=True)

    # ---------- per-step ----------

    def exchange(self, traci_module, step: int) -> Dict[str, int]:
        """Run one barrier round: send outgoing, receive + apply incoming.

        Returns counters for diagnostics: {"out": X, "in": Y}.
        """
        if self._finished or self._disabled:
            return {"out": 0, "in": 0}

        outgoing = self._collect_and_remove_outgoing(traci_module, step)
        try:
            t0 = time.perf_counter()
            resp = self._client.step(step, outgoing)
            if resp is None:
                raise OSError("barrier returned no response")
            self.stats.barrier_calls += 1
            self.stats.barrier_total_sec += (time.perf_counter() - t0)
        except (HTTPError, URLError, OSError) as e:
            print(f"[sync] WARN: barrier exchange failed at step {step}: {e}",
                  flush=True)
            # If a fast transport died, degrade to HTTP for the rest of the run.
            if self._transport != "http":
                try:
                    self._client = self._make_http_client()
                    self._transport = "http"
                    self.stats.transport = "http"
                    print("[sync] switched to HTTP barrier after transport "
                          "failure", flush=True)
                except Exception:
                    pass
            self._account_for_failed_barrier(step)
            return {"out": len(outgoing), "in": 0}

        if not resp.get("ok"):
            print(f"[sync] WARN: barrier rejected at step {step}: "
                  f"{resp.get('error')}", flush=True)
            self.stats.barrier_timeouts += 1
            self._account_for_failed_barrier(step)
            return {"out": len(outgoing), "in": 0}

        # Successful barrier: reset the dead-peer counter.
        self._consecutive_timeouts = 0

        incoming = resp.get("incoming") or []
        applied = self._apply_incoming(traci_module, step, incoming)

        self.stats.handoffs_out += len(outgoing)
        self.stats.handoffs_in += applied

        if self.verbose and (outgoing or incoming) and (step % 50 == 0 or len(outgoing) + len(incoming) >= 5):
            print(f"[sync] step={step} out={len(outgoing)} in={applied}",
                  flush=True)

        return {"out": len(outgoing), "in": applied}

    # ---------- failure handling ----------

    def _account_for_failed_barrier(self, step: int) -> None:
        """Track consecutive barrier failures and disable the bridge if all
        peers seem dead. After disable we keep simulating locally without
        any further handoffs, which avoids the worst-case "wait 120 s every
        step for hours" failure mode."""
        self._consecutive_timeouts += 1
        if self._consecutive_timeouts >= self._max_consecutive_timeouts:
            self._disabled = True
            self.stats.self_disabled_at_step = step
            print(
                f"[sync] DISABLED at step={step}: {self._consecutive_timeouts} "
                f"consecutive barrier failures. Treating peers as dead and "
                f"continuing as a local sim (no further handoffs).",
                flush=True,
            )

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            if self._client is not None:
                self._client.finish()
                self._client.close()
                self._client = None
        except Exception as e:
            print(f"[sync] WARN: finish notify failed: {e}", flush=True)
        if self.verbose:
            elapsed = time.time() - self.stats.boot_at
            print(f"[sync] DONE group={self.group} elapsed={elapsed:.1f}s "
                  f"barriers={self.stats.barrier_calls} "
                  f"handoffs_out={self.stats.handoffs_out} "
                  f"handoffs_in={self.stats.handoffs_in} "
                  f"failures={self.stats.handoff_failures}", flush=True)

    # ---------- private: outgoing ----------

    def _collect_and_remove_outgoing(self, traci, step: int
                                     ) -> List[Dict[str, Any]]:
        outgoing: List[Dict[str, Any]] = []
        try:
            current_ids = list(traci.vehicle.getIDList())
        except Exception:
            return outgoing

        for vid in current_ids:
            try:
                edge = traci.vehicle.getRoadID(vid)
            except Exception:
                continue
            if not edge or edge.startswith(":"):
                # On internal junction edge: defer decision to next step
                continue
            owner = self.edge_owner.get(edge)
            if not owner or owner == self.SHARED_LABEL or owner == self.group:
                continue
            if owner not in self.peers:
                # Owner is a group not present in this sim; ignore
                continue

            # Cooldown to avoid ping-pong right after we received it
            recent_in = self._recent_incoming.get(vid, -10**9)
            if step - recent_in < self.handoff_cooldown_steps:
                continue

            try:
                vtype = traci.vehicle.getTypeID(vid)
                lane_id = traci.vehicle.getLaneID(vid)
                lane_pos = traci.vehicle.getLanePosition(vid)
                speed = traci.vehicle.getSpeed(vid)
                route_idx = traci.vehicle.getRouteIndex(vid)
                route_full = traci.vehicle.getRoute(vid)
                # Remaining route from the current edge onwards
                remaining = list(route_full[route_idx:]) if route_idx >= 0 \
                    else list(route_full)
                if not remaining or remaining[0] != edge:
                    # Be defensive: ensure the first edge is the current one
                    remaining = [edge] + [
                        e for e in remaining if e != edge
                    ]
                color = None
                try:
                    color = list(traci.vehicle.getColor(vid))
                except Exception:
                    color = None
                depart = 0.0
                try:
                    depart = float(traci.vehicle.getDeparture(vid))
                except Exception:
                    depart = 0.0
            except Exception as e:
                print(f"[sync] WARN: could not capture {vid}: {e}", flush=True)
                continue

            outgoing.append({
                "vid": vid,
                "vtype": vtype,
                "edges": remaining,
                "lane_id": lane_id,
                "lane_pos": float(lane_pos),
                "speed": float(speed),
                "depart": depart,
                "color": color,
                "src_group": self.group,
                "dest_group": owner,
            })

            try:
                traci.vehicle.remove(vid)
                self._recent_outgoing[vid] = step
            except Exception as e:
                print(f"[sync] WARN: traci.vehicle.remove({vid}) failed: {e}",
                      flush=True)
                # Pop the last queued handoff so we don't double-create it
                outgoing.pop()
                self.stats.handoff_failures += 1

        return outgoing

    # ---------- private: incoming ----------

    def _apply_incoming(self, traci, step: int,
                        incoming: List[Dict[str, Any]]) -> int:
        if not incoming:
            return 0

        try:
            existing = set(traci.vehicle.getIDList())
        except Exception:
            existing = set()

        applied = 0
        for h in incoming:
            vid = h.get("vid")
            edges = h.get("edges") or []
            if not vid or not edges:
                continue
            if vid in existing:
                # Should not happen, but be defensive
                print(f"[sync] WARN: duplicate vid {vid} arriving via "
                      f"handoff at step {step}; skipping", flush=True)
                self.stats.handoff_failures += 1
                continue

            route_id = f"hf_{vid}_{step}"
            try:
                traci.route.add(route_id, edges)
            except Exception as e:
                # If the same id was somehow used previously, append a
                # disambiguator and try again.
                try:
                    route_id = f"{route_id}_{int(time.time()*1000) % 100000}"
                    traci.route.add(route_id, edges)
                except Exception as e2:
                    print(f"[sync] WARN: route.add failed for {vid}: {e2}",
                          flush=True)
                    self.stats.handoff_failures += 1
                    continue

            # Add the vehicle. depart="now" puts it in the sim immediately.
            try:
                traci.vehicle.add(
                    vehID=vid,
                    routeID=route_id,
                    typeID=h.get("vtype") or "DEFAULT_VEHTYPE",
                    depart="now",
                    departLane="best",
                    departPos="0",
                    departSpeed=str(max(0.0, float(h.get("speed") or 0.0))),
                )
            except Exception as e:
                print(f"[sync] WARN: vehicle.add({vid}) failed: {e}",
                      flush=True)
                self.stats.handoff_failures += 1
                continue

            # Move to the exact lane / position / speed if possible.
            lane_id = h.get("lane_id")
            lane_pos = float(h.get("lane_pos") or 0.0)
            speed = float(h.get("speed") or 0.0)
            try:
                if lane_id:
                    traci.vehicle.moveTo(vid, lane_id, lane_pos)
            except Exception:
                # Non-fatal: the vehicle stays where SUMO inserted it.
                pass
            try:
                traci.vehicle.setSpeed(vid, speed)
            except Exception:
                pass

            color = h.get("color")
            if color and len(color) >= 3:
                try:
                    traci.vehicle.setColor(
                        vid,
                        (int(color[0]), int(color[1]), int(color[2]),
                         int(color[3]) if len(color) >= 4 else 255),
                    )
                except Exception:
                    pass

            self._recent_incoming[vid] = step
            applied += 1

        return applied
