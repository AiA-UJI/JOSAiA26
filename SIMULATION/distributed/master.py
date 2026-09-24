"""
Distributed master: HTTP server orchestrating workers.

Responsibilities
----------------
* Accept worker registrations and heartbeats
* Hand out jobs from a FIFO queue (priority breaks ties)
* Receive job-completion notifications and uploaded result archives
* Track parent simulations (groups of sub-jobs in spatial mode) and trigger
  the aggregator once all sub-jobs of a parent are complete
* Expose a JSON status endpoint and a minimal HTML dashboard for the GUI

The implementation uses only the Python standard library (``http.server``)
to avoid extra dependencies on workers. It is intended for LAN clusters of
2-8 PCs; not for production-scale deployments.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.protocol import (  # noqa: E402
    BARRIER_PORT_OFFSET,
    BARRIER_PORT_OFFSETS,
    DEFAULT_MASTER_PORT,
    DEFAULT_SYNC_BARRIER_TIMEOUT,
    DEFAULT_WORKER_TIMEOUT,
    EP_COMPLETE,
    EP_DASHBOARD,
    EP_HEARTBEAT,
    EP_PULL_JOB,
    EP_REGISTER,
    EP_STATUS,
    EP_SUBMIT,
    EP_SYNC_BARRIER,
    EP_SYNC_EDGES_PREFIX,
    EP_SYNC_FINISH,
    EP_TRIPS_PREFIX,
    EP_UPLOAD,
    HeartbeatPayload,
    JobConfig,
    JobResult,
    JobStatus,
    SimMode,
    WorkerInfo,
    WorkerStatus,
)
from SIMULATION.distributed.aggregator import aggregate as aggregate_results  # noqa: E402
from SIMULATION.distributed import corridors  # noqa: E402
from SIMULATION.distributed.trip_splitter import split_trips  # noqa: E402
from SIMULATION.distributed.edge_classifier import build_edge_owner_map  # noqa: E402
from SIMULATION.CONSTANTS import (  # noqa: E402
    SIMULATION_DIR,
    get_network_file,
    get_trips_file,
)


# ==================== Sync session (synced-spatial mode) ====================

class SyncSession:
    """Per-parent_sim coordination state for step-by-step handoffs.

    Each step proceeds as follows:

        1. Every participating group POSTs ``/api/sync/barrier`` with its
           outgoing handoffs (vehicles that crossed into a foreign-owned
           edge during this step).
        2. The master buffers the handoffs into ``step_buckets[step]``.
        3. When all participants have arrived for a given step, the master
           atomically distributes the handoffs (each group receives the
           handoffs from the OTHER groups) and signals the barrier event.
        4. Each waiting POST returns its incoming handoffs.

    A group can also call ``finish()`` (POST /api/sync/finish) to declare
    end-of-simulation; remaining peers can then leave the barrier without
    waiting for it.
    """

    # If a peer never shows up at the barrier we consider it dead after this
    # many consecutive timeouts experienced by other peers and we evict it
    # from ``participants`` so the simulation can still progress.
    PEER_DEAD_AFTER_CONSECUTIVE_TIMEOUTS = 2

    def __init__(self, parent_sim_id: str, participants: List[str],
                 edge_owner_path: Path):
        self.parent_sim_id = parent_sim_id
        self.participants = set(participants)
        self.finished: set = set()
        self.auto_evicted: set = set()
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        # step -> {group -> [handoff dicts]}
        self.step_buckets: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}
        # step -> {group -> True} once that group has fetched its incoming
        self.step_consumed: Dict[int, set] = {}
        # group -> consecutive barrier timeouts experienced by *other* peers
        # while waiting for ``group``. When this hits the threshold we mark
        # ``group`` as auto-evicted so the rest can keep going.
        self.peer_timeouts: Dict[str, int] = {}
        self.edge_owner_path = edge_owner_path
        self.created_at = time.time()
        self.last_step_seen: Dict[str, int] = {}

    def submit_handoffs(self, step: int, group: str,
                        outgoing: List[Dict[str, Any]],
                        timeout: float = DEFAULT_SYNC_BARRIER_TIMEOUT
                        ) -> Dict[str, Any]:
        """Submit ``outgoing`` for ``step`` from ``group`` and block until
        the barrier opens (or peers finish). Returns the incoming handoffs
        for ``group``.
        """
        if group not in self.participants:
            return {"ok": False, "error": f"unknown group {group}"}

        with self.cond:
            bucket = self.step_buckets.setdefault(step, {})
            if group in bucket:
                return {"ok": False, "error": f"duplicate submission "
                                              f"for {group}@step{step}"}
            bucket[group] = outgoing
            self.last_step_seen[group] = step
            # As soon as we hear from a group it cannot be considered dead
            self.peer_timeouts[group] = 0

            deadline = time.monotonic() + timeout
            barrier_opened = False
            while not barrier_opened:
                expected = (self.participants
                            - self.finished
                            - self.auto_evicted)
                if expected.issubset(set(bucket.keys())):
                    barrier_opened = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = sorted(expected - set(bucket.keys()))
                    # Penalise the absent peers; if they keep no-showing we
                    # auto-evict them so the live peers can stop blocking.
                    evicted_now = []
                    for absent in missing:
                        self.peer_timeouts[absent] = (
                            self.peer_timeouts.get(absent, 0) + 1
                        )
                        if (self.peer_timeouts[absent]
                                >= self.PEER_DEAD_AFTER_CONSECUTIVE_TIMEOUTS):
                            self.auto_evicted.add(absent)
                            evicted_now.append(absent)
                    if evicted_now:
                        print(
                            f"[master] sync session {self.parent_sim_id}: "
                            f"auto-evicted peers {evicted_now} after "
                            f"{self.PEER_DEAD_AFTER_CONSECUTIVE_TIMEOUTS} "
                            f"consecutive timeouts (probably crashed or never "
                            f"sync-aware). Live peers will continue without them.",
                            flush=True,
                        )
                        self.cond.notify_all()
                        # Re-evaluate the barrier with the new evicted set;
                        # if the only missing peers were the evicted ones we
                        # can open right now without a hard error.
                        new_expected = (self.participants
                                        - self.finished
                                        - self.auto_evicted)
                        if new_expected.issubset(set(bucket.keys())):
                            barrier_opened = True
                            break
                    return {
                        "ok": False,
                        "error": f"barrier timeout at step {step}; "
                                 f"missing={missing}",
                        "incoming": [],
                        "auto_evicted": sorted(self.auto_evicted),
                    }
                self.cond.wait(timeout=remaining)
                # Re-check after wakeup (someone arrived or finished)

            # Build incoming = union of every other group's outgoing
            # whose 'dest_group' (computed by the sender) matches us. If
            # the sender did not annotate dest_group we route to all peers.
            incoming: List[Dict[str, Any]] = []
            for src_group, items in bucket.items():
                if src_group == group:
                    continue
                for h in items:
                    dest = h.get("dest_group")
                    if dest is None or dest == group:
                        incoming.append(h)

            self.step_consumed.setdefault(step, set()).add(group)

            # Notify peers (some may still be waiting their wake-up)
            self.cond.notify_all()

            # Garbage collect step buckets older than current
            self._gc_buckets(step)

            return {"ok": True, "incoming": incoming, "step": step}

    def finish(self, group: str) -> Dict[str, Any]:
        with self.cond:
            self.finished.add(group)
            # Wake everyone so they recompute the expected set
            self.cond.notify_all()
            return {"ok": True, "finished_groups": sorted(self.finished)}

    def _gc_buckets(self, current_step: int) -> None:
        # When all live participants have consumed step N, drop the bucket
        live = self.participants - self.finished - self.auto_evicted
        if not live:
            self.step_buckets.clear()
            self.step_consumed.clear()
            return
        to_drop = []
        for s, consumed in self.step_consumed.items():
            if live.issubset(consumed):
                to_drop.append(s)
        for s in to_drop:
            self.step_buckets.pop(s, None)
            self.step_consumed.pop(s, None)


# ==================== Master state ====================

class MasterState:
    """Thread-safe state shared between HTTP handler threads."""

    def __init__(self, work_dir: Path):
        self.lock = threading.RLock()
        self.workers: Dict[str, WorkerInfo] = {}
        self.pending_jobs: Deque[JobConfig] = deque()
        self.running_jobs: Dict[str, Tuple[JobConfig, str, float]] = {}   # job_id -> (cfg, worker_id, started_at)
        self.completed_jobs: Dict[str, JobResult] = {}
        self.failed_jobs: Dict[str, JobResult] = {}
        self.parent_sims: Dict[str, dict] = {}     # parent_sim_id -> metadata + sub-job ids
        # job_id -> absolute path of the trips XML the worker should run with.
        # Used to serve /api/trips/<job_id> so workers do NOT need shared disk.
        self.job_trips_paths: Dict[str, str] = {}
        # parent_sim_id -> SyncSession (only for synced-spatial mode)
        self.sync_sessions: Dict[str, SyncSession] = {}
        # parent_sim_id -> path to edge_owner.json (served via GET)
        self.sync_edge_owner_files: Dict[str, str] = {}
        self.work_dir = work_dir
        self.uploads_dir = work_dir / "uploads"
        self.aggregated_dir = work_dir / "aggregated"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.aggregated_dir.mkdir(parents=True, exist_ok=True)
        self.created_at = time.time()

    # -- Workers --

    def register_worker(self, worker: WorkerInfo) -> None:
        with self.lock:
            self.workers[worker.worker_id] = worker

    def update_worker_heartbeat(self, hb: HeartbeatPayload) -> Optional[WorkerInfo]:
        with self.lock:
            w = self.workers.get(hb.worker_id)
            if not w:
                return None
            w.status = hb.status
            w.current_job_id = hb.current_job_id
            w.current_step = hb.current_step
            w.total_steps = hb.total_steps
            w.current_vehicles = hb.current_vehicles
            w.last_heartbeat = time.time()
            return w

    def alive_workers(self, timeout_sec: float = DEFAULT_WORKER_TIMEOUT) -> List[WorkerInfo]:
        with self.lock:
            now = time.time()
            return [
                w for w in self.workers.values()
                if (now - w.last_heartbeat) < timeout_sec
            ]

    # -- Jobs --

    def _register_trips_if_any(self, cfg: JobConfig) -> None:
        if cfg.custom_trips_file:
            self.job_trips_paths[cfg.job_id] = str(cfg.custom_trips_file)

    def submit_job(self, cfg: JobConfig) -> str:
        with self.lock:
            self.pending_jobs.append(cfg)
            self._register_trips_if_any(cfg)
            return cfg.job_id

    def submit_parent_sim(self, parent_id: str, jobs: List[JobConfig], meta: dict) -> None:
        with self.lock:
            self.parent_sims[parent_id] = {
                "meta": meta,
                "sub_job_ids": [j.job_id for j in jobs],
                "submitted_at": time.time(),
                "completed_sub_dirs": {},
                "aggregated_dir": None,
                "status": "pending",
            }
            for j in jobs:
                self.pending_jobs.append(j)
                self._register_trips_if_any(j)

    def get_trips_path(self, job_id: str) -> Optional[str]:
        with self.lock:
            return self.job_trips_paths.get(job_id)

    # -- Sync sessions (synced-spatial mode) --

    def register_sync_session(
        self,
        parent_sim_id: str,
        participants: List[str],
        edge_owner_path: Path,
    ) -> SyncSession:
        with self.lock:
            sess = SyncSession(parent_sim_id, participants, edge_owner_path)
            self.sync_sessions[parent_sim_id] = sess
            self.sync_edge_owner_files[parent_sim_id] = str(edge_owner_path)
            return sess

    def get_sync_session(self, parent_sim_id: str) -> Optional[SyncSession]:
        with self.lock:
            return self.sync_sessions.get(parent_sim_id)

    def get_edge_owner_file(self, parent_sim_id: str) -> Optional[str]:
        with self.lock:
            return self.sync_edge_owner_files.get(parent_sim_id)

    def pull_job(self, worker_id: str) -> Optional[JobConfig]:
        with self.lock:
            if not self.pending_jobs:
                return None
            cfg = self.pending_jobs.popleft()
            self.running_jobs[cfg.job_id] = (cfg, worker_id, time.time())
            w = self.workers.get(worker_id)
            if w:
                w.status = WorkerStatus.BUSY.value
                w.current_job_id = cfg.job_id
            return cfg

    def complete_job(self, result: JobResult) -> Optional[JobConfig]:
        """Mark a job complete; returns its JobConfig (or None if unknown)."""
        with self.lock:
            entry = self.running_jobs.pop(result.job_id, None)
            cfg = entry[0] if entry else None
            if result.success:
                self.completed_jobs[result.job_id] = result
                w = self.workers.get(result.worker_id)
                if w:
                    w.jobs_completed += 1
                    w.total_runtime_sec += result.duration_sec
                    w.status = WorkerStatus.IDLE.value
                    w.current_job_id = None
            else:
                self.failed_jobs[result.job_id] = result
                w = self.workers.get(result.worker_id)
                if w:
                    w.jobs_failed += 1
                    w.status = WorkerStatus.IDLE.value
                    w.current_job_id = None
            return cfg

    # -- Snapshot for dashboard / status --

    def snapshot(self, timeout_sec: float = DEFAULT_WORKER_TIMEOUT) -> dict:
        with self.lock:
            now = time.time()
            workers = []
            for w in self.workers.values():
                d = w.to_dict()
                d["alive"] = (now - w.last_heartbeat) < timeout_sec
                d["heartbeat_age_sec"] = round(now - w.last_heartbeat, 1)
                workers.append(d)

            pending = [j.to_dict() for j in list(self.pending_jobs)]
            running = []
            for job_id, (cfg, worker_id, started) in self.running_jobs.items():
                running.append({
                    "job": cfg.to_dict(),
                    "worker_id": worker_id,
                    "started_at": started,
                    "running_for_sec": round(now - started, 1),
                })

            parents = {}
            for pid, p in self.parent_sims.items():
                parents[pid] = {
                    "meta": p["meta"],
                    "sub_job_ids": p["sub_job_ids"],
                    "completed": len(p["completed_sub_dirs"]),
                    "total": len(p["sub_job_ids"]),
                    "status": p["status"],
                    "aggregated_dir": p["aggregated_dir"],
                }

            completed_detail = [
                {"job_id": jid, "worker_id": r.worker_id, "success": r.success,
                 "duration_sec": round(r.duration_sec, 1),
                 "label": (r.metadata or {}).get("label", "")}
                for jid, r in self.completed_jobs.items()
            ]
            failed_detail = [
                {"job_id": jid, "worker_id": r.worker_id,
                 "error": (r.error or "")[:200]}
                for jid, r in self.failed_jobs.items()
            ]

            return {
                "workers": workers,
                "pending_jobs": pending,
                "running_jobs": running,
                "completed_jobs": len(self.completed_jobs),
                "failed_jobs": len(self.failed_jobs),
                "completed_detail": completed_detail,
                "failed_detail": failed_detail,
                "parent_sims": parents,
                "uptime_sec": round(now - self.created_at, 1),
            }


# ==================== Submission helpers (used by GUI / CLI) ====================

def _build_jobs_for_sim(
    state: MasterState,
    *,
    mode: str,
    road: str,
    ratio: str,
    sharing: List[str],
    accident: bool,
    accident_start: Optional[int],
    accident_duration: Optional[int],
    force_cv: bool,
    vehicles: Optional[int],
    save_environment: bool,
    save_knowledge: bool,
    save_tripinfo: bool,
    max_steps: int,
    workers: int,
    label: Optional[str] = None,
    trips: Optional[str] = None,
    sync_master_url: Optional[str] = None,
    balanced: bool = False,
    barrier_addr: Optional[Tuple[str, int]] = None,
    partition: Optional[str] = None,
    barrier_transport: str = "tcp",
) -> List[JobConfig]:
    """Create the JobConfig list for one logical simulation.

    * ``mode = batch``  -> returns 1 job (no spatial decomposition)
    * ``mode = spatial`` -> returns N jobs (one per corridor group, NO sync)
    * ``mode = synced-spatial`` -> N jobs + per-step handoff via master
    """

    base_kwargs = dict(
        road=road,
        ratio=ratio,
        sharing=list(sharing) if sharing else ["none"],
        accident=accident,
        accident_start=accident_start,
        accident_duration=accident_duration,
        force_cv=force_cv,
        vehicles=vehicles,
        save_environment=save_environment,
        save_knowledge=save_knowledge,
        save_tripinfo=save_tripinfo,
        max_steps=max_steps,
    )

    if mode == SimMode.BATCH.value:
        return [JobConfig(job_id=JobConfig.new_id(), trips=trips, **base_kwargs)]

    # ---- SPATIAL / SYNCED-SPATIAL ----
    # Resolve the partition strategy. ``partition`` (e.g. "balanced-x-3",
    # "highway-2", "corridor-2") takes precedence; fall back to legacy
    # ``balanced`` flag / corridor split keyed on ``workers``.
    from SIMULATION.distributed.partitioning import parse_partition
    if partition:
        spec = parse_partition(partition)
        workers = spec.num_groups
    elif balanced:
        spec = parse_partition(f"balanced-x-{workers}")
    else:
        spec = parse_partition(f"corridor-{workers}")
    p_balanced = spec.balanced
    p_highway = spec.highway
    p_axis = spec.axis
    p_ngroups = spec.num_groups

    if spec.kind == "corridor" and workers not in corridors.SPLITS:
        raise ValueError(
            f"Corridor mode supports {sorted(corridors.SPLITS)} workers; got {workers}"
        )
    parent_id = f"sim_{uuid.uuid4().hex[:10]}"

    net_file = get_network_file(road)
    trips_file = get_trips_file(road, ratio, force_cv=force_cv, vehicles=vehicles)
    if not os.path.isfile(trips_file):
        raise FileNotFoundError(f"Trips file not found: {trips_file}")

    # Accident edges (for accident-aware balanced partitioning).
    accident_edges_cfg: Optional[List[str]] = None
    if accident:
        from SIMULATION.CONSTANTS import get_road_config
        cfg_edges = get_road_config(road).get("accident_edges")
        if cfg_edges:
            accident_edges_cfg = list(cfg_edges)

    paths = split_trips(
        net_file, trips_file, workers,
        balanced=p_balanced, accident=accident, accident_edges=accident_edges_cfg,
        axis=p_axis, num_groups=p_ngroups, highway=p_highway,
    )

    is_synced = mode == SimMode.SYNCED_SPATIAL.value

    # For synced mode, pre-compute the edge ownership map and register a
    # SyncSession so workers can do step-by-step handoffs.
    if is_synced:
        if not sync_master_url:
            raise ValueError(
                "synced-spatial mode requires sync_master_url to be set so "
                "workers know where to POST their handoffs."
            )
        owner_map, _counts = build_edge_owner_map(
            net_file, trips_file, workers,
            balanced=p_balanced, accident=accident,
            accident_edges=accident_edges_cfg,
            axis=p_axis, num_groups=p_ngroups, highway=p_highway,
        )
        # Persist edge_owner under the master_work dir keyed by parent_id so
        # workers can GET it once at startup.
        sync_dir = state.work_dir / "sync_sessions" / parent_id
        sync_dir.mkdir(parents=True, exist_ok=True)
        edge_owner_path = sync_dir / "edge_owner.json"
        b_host, b_port = (barrier_addr or (None, None))
        edge_owner_path.write_text(
            json.dumps({
                "edge_owner": owner_map,
                "split": list(paths.keys()),
                "partition": spec.name,
                "parent_sim_id": parent_id,
                "barrier_host": b_host,
                "barrier_port": b_port,
                "barrier_transport": barrier_transport,
            }, indent=2),
            encoding="utf-8",
        )
        participants = list(paths.keys())
        state.register_sync_session(parent_id, participants, edge_owner_path)
        sys.stderr.write(
            f"[master] sync session created: parent={parent_id} "
            f"participants={participants} edges={len(owner_map)}\n"
        )

    jobs: List[JobConfig] = []
    peers = list(paths.keys())
    for grp_label, grp_path in paths.items():
        cfg = JobConfig(
            job_id=JobConfig.new_id(),
            parent_sim_id=parent_id,
            spatial_corridor=grp_label,
            custom_trips_file=grp_path,
            sync_master=sync_master_url if is_synced else None,
            sync_group=grp_label if is_synced else None,
            sync_peers=[p for p in peers if p != grp_label] if is_synced else None,
            **base_kwargs,
        )
        jobs.append(cfg)

    state.submit_parent_sim(
        parent_id,
        jobs,
        meta={
            "label": label or f"{road} ratio={ratio} workers={workers}",
            "mode": mode,
            "road": road,
            "ratio": ratio,
            "workers": workers,
            "accident": accident,
        },
    )
    return jobs


# ==================== HTTP server ====================

def _ensure_methods(server, state: MasterState):
    server.master_state = state


class MasterHandler(BaseHTTPRequestHandler):
    """One handler instance per request (ThreadingHTTPServer)."""

    server_version = "VehicleKnowledgeMaster/1.0"

    # -- Helpers --

    @property
    def state(self) -> MasterState:
        return self.server.master_state  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON body: {e}")

    def log_message(self, format: str, *args) -> None:
        # Quieter than default
        sys.stderr.write(
            f"[master {datetime.now().strftime('%H:%M:%S')}] "
            f"{self.address_string()} - {format % args}\n"
        )

    # -- Routing --

    def do_GET(self):
        try:
            if self.path == EP_DASHBOARD or self.path == "":
                return self._send_html(200, _render_dashboard(self.state))
            if self.path == EP_STATUS:
                return self._send_json(200, self.state.snapshot())
            if self.path.startswith(EP_TRIPS_PREFIX):
                return self._serve_trips()
            if self.path.startswith(EP_SYNC_EDGES_PREFIX):
                return self._serve_sync_edges()
            if self.path.startswith("/api/download/"):
                return self._serve_download()
            return self._send_json(404, {"error": "Not found", "path": self.path})
        except Exception as e:
            traceback.print_exc()
            return self._send_json(500, {"error": str(e)})

    def do_POST(self):
        try:
            if self.path == EP_REGISTER:
                return self._handle_register()
            if self.path == EP_HEARTBEAT:
                return self._handle_heartbeat()
            if self.path == EP_PULL_JOB:
                return self._handle_pull_job()
            if self.path == EP_COMPLETE:
                return self._handle_complete()
            if self.path == EP_UPLOAD:
                return self._handle_upload()
            if self.path == EP_SUBMIT:
                return self._handle_submit()
            if self.path == EP_SYNC_BARRIER:
                return self._handle_sync_barrier()
            if self.path == EP_SYNC_FINISH:
                return self._handle_sync_finish()
            return self._send_json(404, {"error": "Not found", "path": self.path})
        except ValueError as e:
            return self._send_json(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send_json(500, {"error": str(e)})

    # -- Handlers --

    def _handle_register(self):
        data = self._read_json()
        worker = WorkerInfo.from_dict(data)
        worker.last_heartbeat = time.time()
        worker.registered_at = time.time()
        if worker.status not in (
            WorkerStatus.IDLE.value, WorkerStatus.BUSY.value
        ):
            worker.status = WorkerStatus.IDLE.value
        self.state.register_worker(worker)
        sys.stderr.write(
            f"[master] worker {worker.worker_id} registered: "
            f"{worker.hostname} ({worker.ip}) cpu={worker.cpu_count} "
            f"ram={worker.ram_mb:.0f}MB\n"
        )
        return self._send_json(200, {"ok": True, "worker_id": worker.worker_id})

    def _handle_heartbeat(self):
        data = self._read_json()
        hb = HeartbeatPayload.from_dict(data)
        w = self.state.update_worker_heartbeat(hb)
        if not w:
            return self._send_json(404, {"error": "unknown worker", "worker_id": hb.worker_id})
        return self._send_json(200, {"ok": True, "ts": time.time()})

    def _handle_pull_job(self):
        data = self._read_json()
        worker_id = data.get("worker_id")
        if not worker_id:
            return self._send_json(400, {"error": "worker_id required"})
        cfg = self.state.pull_job(worker_id)
        if cfg is None:
            return self._send_json(204, {"ok": True, "job": None})
        return self._send_json(200, {"ok": True, "job": cfg.to_dict()})

    def _handle_complete(self):
        data = self._read_json()
        result = JobResult.from_dict(data)
        cfg = self.state.complete_job(result)

        # Track parent sim if applicable
        if cfg and cfg.parent_sim_id:
            self._maybe_finish_parent(cfg, result)

        return self._send_json(200, {"ok": True, "job_id": result.job_id})

    def _handle_upload(self):
        """Receive a zipped sub-run output. Body = raw zip bytes.

        Headers used:
          X-Job-Id: <job_id>
          X-Worker-Id: <worker_id>
          X-Output-Filename: <suggested filename>
        """
        job_id = self.headers.get("X-Job-Id")
        worker_id = self.headers.get("X-Worker-Id", "unknown")
        if not job_id:
            return self._send_json(400, {"error": "X-Job-Id header required"})

        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return self._send_json(400, {"error": "empty upload"})

        # Stream-copy to disk to avoid loading huge archives in memory
        target_dir = self.state.uploads_dir / job_id
        target_dir.mkdir(parents=True, exist_ok=True)
        zip_path = target_dir / "result.zip"
        bytes_left = length
        with zip_path.open("wb") as fp:
            while bytes_left > 0:
                chunk = self.rfile.read(min(65536, bytes_left))
                if not chunk:
                    break
                fp.write(chunk)
                bytes_left -= len(chunk)

        # Extract
        extract_dir = target_dir / "extracted"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile as e:
            return self._send_json(400, {"error": f"bad zip: {e}"})

        sys.stderr.write(
            f"[master] upload received: job={job_id} worker={worker_id} "
            f"size={length} -> {extract_dir}\n"
        )
        return self._send_json(200, {
            "ok": True,
            "extracted_dir": str(extract_dir),
            "size": length,
        })

    def _handle_submit(self):
        """GUI / CLI submits a logical simulation here."""
        data = self._read_json()
        # Sync master URL: clients can override; otherwise fall back to the
        # host:port the server is bound to (so workers reach back here).
        sync_master_url = (
            data.get("sync_master_url")
            or getattr(self.server, "advertised_url", None)
            or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        )
        # Barrier transport: the chosen fast transport server lives on the
        # same host as the sync URL, at port + BARRIER_PORT_OFFSETS[transport].
        barrier_transport = (data.get("barrier_transport") or "tcp").lower()
        barrier_addr = None
        try:
            _bh, _bp = sync_master_url.split(":")
            off = BARRIER_PORT_OFFSETS.get(barrier_transport, BARRIER_PORT_OFFSET)
            barrier_addr = (_bh, int(_bp) + off)
        except Exception:
            barrier_addr = None
        try:
            jobs = _build_jobs_for_sim(
                self.state,
                mode=data.get("mode", SimMode.BATCH.value),
                road=data.get("road", "modified_4ways_all"),
                ratio=str(data.get("ratio", "99")),
                sharing=data.get("sharing") or ["none"],
                accident=bool(data.get("accident", False)),
                accident_start=data.get("accident_start"),
                accident_duration=data.get("accident_duration"),
                force_cv=bool(data.get("force_cv", False)),
                vehicles=data.get("vehicles"),
                save_environment=bool(data.get("save_environment", True)),
                save_knowledge=bool(data.get("save_knowledge", False)),
                save_tripinfo=bool(data.get("save_tripinfo", True)),
                max_steps=int(data.get("max_steps", 0)),
                workers=int(data.get("workers", 1)),
                label=data.get("label"),
                trips=data.get("trips"),
                sync_master_url=sync_master_url,
                balanced=bool(data.get("balanced", False)),
                barrier_addr=barrier_addr,
                partition=data.get("partition"),
                barrier_transport=barrier_transport,
            )
        except Exception as e:
            traceback.print_exc()
            return self._send_json(400, {"error": str(e)})

        if data.get("mode") == SimMode.BATCH.value:
            for j in jobs:
                self.state.submit_job(j)

        return self._send_json(200, {
            "ok": True,
            "job_ids": [j.job_id for j in jobs],
            "parent_sim_id": jobs[0].parent_sim_id if jobs else None,
            "mode": data.get("mode", SimMode.BATCH.value),
            "count": len(jobs),
        })

    # -- Parent sim aggregation --

    def _maybe_finish_parent(self, cfg: JobConfig, result: JobResult) -> None:
        st = self.state
        with st.lock:
            parent = st.parent_sims.get(cfg.parent_sim_id)
            if not parent:
                return
            if not result.success:
                parent["status"] = "partial_failure"
                return
            extracted = st.uploads_dir / result.job_id / "extracted"
            inner = extracted / "result"
            sub_dir = inner if inner.is_dir() else extracted
            parent["completed_sub_dirs"][result.job_id] = str(sub_dir)
            done = len(parent["completed_sub_dirs"])
            total = len(parent["sub_job_ids"])
            sys.stderr.write(
                f"[master] parent {cfg.parent_sim_id}: {done}/{total} sub-jobs ready\n"
            )
            if done < total:
                return
            ready_to_aggregate = parent["status"] != "aggregated"
        if not ready_to_aggregate:
            return

        # Aggregation outside of the lock (it does I/O)
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = st.aggregated_dir / f"{ts}_{cfg.parent_sim_id}"
            sub_dirs = list(parent["completed_sub_dirs"].values())
            aggregate_results(
                [Path(p) for p in sub_dirs],
                out_dir,
                parent_sim_id=cfg.parent_sim_id,
                extra_info={"meta": parent["meta"]},
            )
            with st.lock:
                parent["status"] = "aggregated"
                parent["aggregated_dir"] = str(out_dir)
            sys.stderr.write(
                f"[master] parent {cfg.parent_sim_id} aggregated -> {out_dir}\n"
            )
        except Exception:
            traceback.print_exc()
            with st.lock:
                parent["status"] = "aggregation_error"

    # -- Sync handlers (synced-spatial mode) --

    def _serve_sync_edges(self):
        """GET /api/sync/edges/<parent_sim_id> -> edge_owner.json"""
        parent_id = self.path[len(EP_SYNC_EDGES_PREFIX):].strip("/")
        if not parent_id:
            return self._send_json(400, {"error": "missing parent_sim_id"})
        path = self.state.get_edge_owner_file(parent_id)
        if not path or not os.path.isfile(path):
            return self._send_json(404, {"error": "no sync session for parent",
                                         "parent_sim_id": parent_id})
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as fp:
            shutil.copyfileobj(fp, self.wfile)

    def _handle_sync_barrier(self):
        """POST /api/sync/barrier
        Body: {parent_sim_id, group, step, outgoing: [...]}
        Returns: {ok, incoming: [...], step}
        """
        data = self._read_json()
        parent_id = data.get("parent_sim_id")
        group = data.get("group")
        step = int(data.get("step", -1))
        outgoing = data.get("outgoing") or []
        if not parent_id or not group or step < 0:
            return self._send_json(400, {"error": "parent_sim_id+group+step required"})
        sess = self.state.get_sync_session(parent_id)
        if not sess:
            return self._send_json(404, {"error": "no sync session", "parent_sim_id": parent_id})
        result = sess.submit_handoffs(step, group, outgoing)
        status = 200 if result.get("ok") else 408   # 408 = barrier timeout
        return self._send_json(status, result)

    def _handle_sync_finish(self):
        """POST /api/sync/finish
        Body: {parent_sim_id, group}
        """
        data = self._read_json()
        parent_id = data.get("parent_sim_id")
        group = data.get("group")
        if not parent_id or not group:
            return self._send_json(400, {"error": "parent_sim_id+group required"})
        sess = self.state.get_sync_session(parent_id)
        if not sess:
            return self._send_json(404, {"error": "no sync session", "parent_sim_id": parent_id})
        return self._send_json(200, sess.finish(group))

    # -- Trips file delivery (workers without shared disk) --

    def _serve_trips(self):
        """Serve the trips XML registered for a given job_id.

        Path: /api/trips/<job_id>
        """
        job_id = self.path[len(EP_TRIPS_PREFIX):].strip("/")
        if not job_id:
            return self._send_json(400, {"error": "missing job_id"})
        path = self.state.get_trips_path(job_id)
        if not path or not os.path.isfile(path):
            return self._send_json(404, {"error": "no trips registered for job_id",
                                         "job_id": job_id})
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(size))
        self.send_header("X-Original-Filename", os.path.basename(path))
        self.end_headers()
        with open(path, "rb") as fp:
            shutil.copyfileobj(fp, self.wfile)

    # -- Downloads (let workers pull aggregated output back if they want) --

    def _serve_download(self):
        # /api/download/<job_id>/result.zip
        parts = self.path.split("/")
        if len(parts) < 5:
            return self._send_json(404, {"error": "bad download path"})
        job_id = parts[3]
        rest = "/".join(parts[4:])
        candidate = self.state.uploads_dir / job_id / rest
        if not candidate.is_file():
            return self._send_json(404, {"error": "file not found"})
        size = candidate.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with candidate.open("rb") as fp:
            shutil.copyfileobj(fp, self.wfile)


# ==================== HTML dashboard ====================

DASHBOARD_TPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>VehicleKnowledge Master</title>
<meta http-equiv="refresh" content="3"/>
<style>
body { font-family: system-ui, sans-serif; margin: 1.5rem; background:#0d1117; color:#e6edf3; }
h1 { margin: 0 0 0.5rem 0; font-size: 1.3rem; }
h2 { margin-top: 1.5rem; font-size: 1.05rem; color:#7ee787; }
.card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:1rem; margin:0.5rem 0; }
.workers { display:grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap:0.75rem; }
.worker { padding:0.75rem; border-radius:8px; background:#161b22; border:1px solid #30363d; }
.worker.busy { border-color:#1f6feb; }
.worker.idle { border-color:#3fb950; }
.worker.offline { border-color:#f85149; opacity:0.6; }
.worker .name { font-weight:600; font-size:1.05em; }
.worker .ip { color:#8b949e; font-size:0.85em; }
.bar { height:6px; background:#30363d; border-radius:3px; overflow:hidden; margin-top:6px; }
.bar > span { display:block; height:100%; background:#3fb950; }
.bar.busy > span { background:#1f6feb; }
table { border-collapse:collapse; width:100%; }
th, td { padding:6px 10px; text-align:left; border-bottom:1px solid #30363d; font-size:0.9em; }
th { color:#8b949e; font-weight:500; }
.muted { color:#8b949e; font-size:0.85em; }
.tag { display:inline-block; padding:1px 7px; border-radius:10px; font-size:0.75em; margin-left:4px; }
.tag.spatial { background:#1f6feb; color:white; }
.tag.batch { background:#6e7681; color:white; }
</style>
</head>
<body>
<h1>VehicleKnowledge Master <span class="muted">uptime __UPTIME__s</span></h1>

<h2>Workers (__N_WORKERS_ALIVE__/__N_WORKERS_TOTAL__ alive)</h2>
<div class="workers">
__WORKERS_HTML__
</div>

<h2>Job Queue (__N_PENDING__ pending, __N_RUNNING__ running, __N_COMPLETED__ done, __N_FAILED__ failed)</h2>
<div class="card">
<table>
<tr><th>Job</th><th>Parent</th><th>Corridor</th><th>Worker</th><th>Step</th><th>Vehicles</th><th>Elapsed</th></tr>
__RUNNING_ROWS__
__PENDING_ROWS__
</table>
</div>

<h2>Spatial simulations</h2>
<div class="card">
<table>
<tr><th>Parent ID</th><th>Mode</th><th>Sub-jobs</th><th>Status</th><th>Aggregated</th></tr>
__PARENTS_ROWS__
</table>
</div>
</body>
</html>
"""


def _render_dashboard(state: MasterState) -> str:
    snap = state.snapshot()
    workers = snap["workers"]
    n_alive = sum(1 for w in workers if w.get("alive"))

    worker_cards = []
    for w in workers:
        cls = "offline" if not w.get("alive") else (
            "busy" if w["status"] == WorkerStatus.BUSY.value else "idle"
        )
        progress_pct = 0
        if w.get("total_steps"):
            progress_pct = int(100 * w["current_step"] / max(1, w["total_steps"]))
        worker_cards.append(f"""
        <div class="worker {cls}">
          <div class="name">{w['hostname']} <span class="muted">({w['worker_id'][:8]})</span></div>
          <div class="ip">{w['ip']} - {w['cpu_count']} CPU - {int(w['ram_mb'])} MB</div>
          <div class="muted">status: {w['status']} - jobs done: {w['jobs_completed']} / failed: {w['jobs_failed']}</div>
          <div class="muted">heartbeat: {w['heartbeat_age_sec']}s ago</div>
          <div>step {w['current_step']}/{w['total_steps']} - {w['current_vehicles']} vehs</div>
          <div class="bar {cls}"><span style="width:{progress_pct}%"></span></div>
        </div>
        """)

    running_rows = ""
    for r in snap["running_jobs"]:
        cfg = r["job"]
        tag = ('<span class="tag spatial">spatial</span>'
               if cfg.get("parent_sim_id") else '<span class="tag batch">batch</span>')
        running_rows += f"""<tr>
            <td>{cfg['job_id'][:8]} {tag}</td>
            <td>{(cfg.get('parent_sim_id') or '')[:10]}</td>
            <td>{cfg.get('spatial_corridor') or '-'}</td>
            <td>{r['worker_id'][:8]}</td>
            <td>(running)</td>
            <td>-</td>
            <td>{r['running_for_sec']}s</td>
        </tr>"""

    pending_rows = ""
    for cfg in snap["pending_jobs"][:30]:
        tag = ('<span class="tag spatial">spatial</span>'
               if cfg.get("parent_sim_id") else '<span class="tag batch">batch</span>')
        pending_rows += f"""<tr class="muted">
            <td>{cfg['job_id'][:8]} {tag}</td>
            <td>{(cfg.get('parent_sim_id') or '')[:10]}</td>
            <td>{cfg.get('spatial_corridor') or '-'}</td>
            <td>(pending)</td><td>-</td><td>-</td><td>-</td>
        </tr>"""

    parents_rows = ""
    for pid, p in snap["parent_sims"].items():
        agg = p.get("aggregated_dir") or "-"
        parents_rows += f"""<tr>
            <td>{pid}</td>
            <td>{p['meta'].get('mode', '?')}</td>
            <td>{p['completed']}/{p['total']}</td>
            <td>{p['status']}</td>
            <td>{agg}</td>
        </tr>"""

    # Use plain string substitution (NOT .format()) so that the CSS braces
    # in DASHBOARD_TPL don't trigger KeyError on every dashboard render.
    replacements = {
        "__UPTIME__": str(int(snap["uptime_sec"])),
        "__N_WORKERS_ALIVE__": str(n_alive),
        "__N_WORKERS_TOTAL__": str(len(workers)),
        "__WORKERS_HTML__": "\n".join(worker_cards) or '<div class="muted">No workers connected</div>',
        "__N_PENDING__": str(len(snap["pending_jobs"])),
        "__N_RUNNING__": str(len(snap["running_jobs"])),
        "__N_COMPLETED__": str(snap["completed_jobs"]),
        "__N_FAILED__": str(snap["failed_jobs"]),
        "__RUNNING_ROWS__": running_rows or "",
        "__PENDING_ROWS__": pending_rows or "",
        "__PARENTS_ROWS__": parents_rows or '<tr class="muted"><td colspan="5">No spatial sims yet</td></tr>',
    }
    html = DASHBOARD_TPL
    for token, value in replacements.items():
        html = html.replace(token, value)
    return html


# ==================== Server lifecycle ====================

def _detect_lan_ip() -> str:
    """Best-effort LAN IP detection (does NOT actually open a connection)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.0)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def run_master(host: str = "0.0.0.0", port: int = DEFAULT_MASTER_PORT,
               work_dir: Optional[Path] = None,
               daemon: bool = False,
               transports: Optional[List[str]] = None) -> Tuple[ThreadingHTTPServer, MasterState, threading.Thread]:
    work_dir = Path(work_dir or (PROJECT_ROOT / "SIMULATION" / "distributed" / "master_work"))
    work_dir.mkdir(parents=True, exist_ok=True)
    state = MasterState(work_dir)
    server = ThreadingHTTPServer((host, port), MasterHandler)
    server.master_state = state  # type: ignore[attr-defined]

    lan_ip = _detect_lan_ip()
    # Advertised URL = host:port that workers will use to reach back here.
    # Used to populate JobConfig.sync_master automatically.
    server.advertised_url = f"{lan_ip}:{port}"  # type: ignore[attr-defined]

    # Barrier transport servers (each on port + BARRIER_PORT_OFFSETS[name]).
    # http needs no server (served by this HTTP handler).
    from SIMULATION.distributed.transports import start_servers
    transports = transports or ["tcp"]
    try:
        servers = start_servers(state, host, port, list(transports))
        server.barrier_servers = servers  # type: ignore[attr-defined]
    except Exception as e:
        print(f"[master] WARN: could not start barrier transports: {e} "
              f"(workers will fall back to HTTP barrier)")

    print(f"[master] Listening on http://{host}:{port}  (LAN: http://{lan_ip}:{port})")
    print(f"[master] Dashboard: http://{lan_ip}:{port}{EP_DASHBOARD}")
    print(f"[master] Work dir : {work_dir}")

    thread = threading.Thread(target=server.serve_forever, name="master-http", daemon=daemon)
    thread.start()
    return server, state, thread


# ==================== CLI ====================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VehicleKnowledge distributed master")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_MASTER_PORT)
    parser.add_argument("--work-dir", default=None)
    parser.add_argument(
        "--transports", default="tcp,udp,zmq,grpc",
        help="comma-separated barrier transports to start (tcp,udp,zmq,grpc)")
    args = parser.parse_args(argv)

    server, state, thread = run_master(
        host=args.host, port=args.port,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        daemon=False,
        transports=[t.strip() for t in args.transports.split(",") if t.strip()],
    )

    try:
        thread.join()
    except KeyboardInterrupt:
        print("\n[master] Shutting down...")
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
