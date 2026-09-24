"""
Wire protocol for distributed simulation.

All master <-> worker communication is JSON over HTTP. Dataclasses defined
here are serialized via :func:`dataclasses.asdict` and parsed back through
``cls.from_dict`` helpers, so the wire format stays stable even if internal
fields are added later (extra fields are ignored on the receiving end).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict, fields
from enum import Enum
from typing import Any, Dict, List, Optional


# ==================== ENUMS ====================

class JobStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"
    ERROR = "error"


class SimMode(str, Enum):
    BATCH = "batch"                  # Independent simulations, one per job
    SPATIAL = "spatial"              # Corridor split, NO inter-worker sync (legacy)
    SYNCED_SPATIAL = "synced-spatial"  # Corridor split + step-by-step handoff via master


# ==================== JOB ====================

@dataclass
class JobConfig:
    """Configuration for a single simulation execution.

    A job maps 1:1 to a ``main.py`` invocation on the worker side. For
    spatial-mode batches, multiple jobs share the same ``parent_sim_id``
    and differ only in ``spatial_corridor`` + ``custom_trips_file``.
    """

    # Identity
    job_id: str
    parent_sim_id: Optional[str] = None       # links sub-jobs of same spatial sim
    spatial_corridor: Optional[str] = None    # "AN" / "CV" / "A7" / "N340" / ...

    # Standard simulation params (mirror main.py's argparse)
    road: str = "modified_4ways_all"
    ratio: str = "99"
    sharing: List[str] = field(default_factory=lambda: ["none"])
    accident: bool = False
    accident_start: Optional[int] = None
    accident_duration: Optional[int] = None
    accident_edges: Optional[List[str]] = None
    force_cv: bool = False
    vehicles: Optional[int] = None

    # Output flags
    save_environment: bool = True
    save_knowledge: bool = False
    save_tripinfo: bool = True

    # Runtime control
    max_steps: int = 0
    custom_trips_file: Optional[str] = None   # absolute path; overrides --vehicles/--ratio resolution
    trips: Optional[str] = None               # trips filename (resolved locally by main.py --trips); batch mode

    # Synced-spatial mode (handoff via master between sub-jobs of same parent)
    # When set, ``main.py`` runs in lock-step with peers, sending vehicles
    # crossing between corridors through the master.
    sync_master: Optional[str] = None         # e.g. "192.168.1.10:9000"
    sync_group: Optional[str] = None          # e.g. "AN", "CV"
    sync_peers: Optional[List[str]] = None    # other group labels participating

    # Bookkeeping (master-side)
    created_at: float = field(default_factory=time.time)
    priority: int = 0                          # higher = earlier dispatch

    # ---- helpers ----

    @classmethod
    def new_id(cls) -> str:
        return uuid.uuid4().hex[:12]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobConfig":
        valid_fields = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**clean)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_main_py_args(self) -> List[str]:
        """Translate to argv for ``python -m SIMULATION.main``."""
        args: List[str] = [
            "--road", self.road,
            "--ratio", self.ratio,
        ]
        # NOTE: ``trips`` NO se pasa como --trips: el worker lo resuelve a ruta
        # local y lo inyecta via VEHICLEKNOWLEDGE_TRIPS_OVERRIDE, que soportan
        # tanto el main.py actual como el del commit del paper (f4b2dcb).
        if self.sharing:
            args += ["--sharing", *self.sharing]
        if self.accident:
            args.append("--accident")
            if self.accident_start is not None:
                args += ["--accident-start", str(self.accident_start)]
            if self.accident_duration is not None:
                args += ["--accident-duration", str(self.accident_duration)]
            if self.accident_edges and len(self.accident_edges) == 2:
                args += ["--accident-edges", *self.accident_edges]
        if self.force_cv:
            args.append("--force-cv")
        if self.vehicles is not None:
            args += ["--vehicles", str(self.vehicles)]
        if self.save_environment:
            args.append("--save-environment")
        else:
            args.append("--no-save-environment")
        if self.save_knowledge:
            args.append("--save-knowledge")
        else:
            args.append("--no-save-knowledge")
        if self.save_tripinfo:
            args.append("--save-tripinfo")
        if self.max_steps and self.max_steps > 0:
            args += ["--max-steps", str(self.max_steps)]
        if self.sync_master and self.sync_group and self.parent_sim_id:
            args += [
                "--sync-master", self.sync_master,
                "--sync-group", self.sync_group,
                "--sync-parent", self.parent_sim_id,
            ]
            if self.sync_peers:
                args += ["--sync-peers", *self.sync_peers]
        return args

    def short_label(self) -> str:
        parts = [self.road, f"{self.ratio}%"]
        if self.accident:
            parts.append("acc")
        if self.spatial_corridor:
            parts.append(self.spatial_corridor)
        return "/".join(parts)


# ==================== JOB RESULT ====================

@dataclass
class JobResult:
    job_id: str
    worker_id: str
    success: bool
    duration_sec: float
    output_archive_url: Optional[str] = None    # relative path on master after upload
    output_size_bytes: int = 0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    finished_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobResult":
        valid = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in valid}
        return cls(**clean)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ==================== WORKER ====================

@dataclass
class WorkerInfo:
    worker_id: str
    hostname: str
    ip: str
    cpu_count: int
    ram_mb: float
    sumo_version: str = "unknown"

    # Live state (updated via heartbeats)
    status: str = WorkerStatus.IDLE.value
    current_job_id: Optional[str] = None
    current_step: int = 0
    total_steps: int = 0
    current_vehicles: int = 0
    last_heartbeat: float = field(default_factory=time.time)
    registered_at: float = field(default_factory=time.time)

    # Aggregate stats
    jobs_completed: int = 0
    jobs_failed: int = 0
    total_runtime_sec: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkerInfo":
        valid = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in valid}
        return cls(**clean)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def is_alive(self, timeout_sec: float = 30.0) -> bool:
        return (time.time() - self.last_heartbeat) < timeout_sec

    def progress_ratio(self) -> float:
        if self.total_steps <= 0:
            return 0.0
        return min(1.0, self.current_step / self.total_steps)


# ==================== HEARTBEAT PAYLOAD ====================

@dataclass
class HeartbeatPayload:
    """What workers send periodically while running a job."""

    worker_id: str
    status: str = WorkerStatus.IDLE.value
    current_job_id: Optional[str] = None
    current_step: int = 0
    total_steps: int = 0
    current_vehicles: int = 0
    last_log_line: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HeartbeatPayload":
        valid = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in valid}
        return cls(**clean)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ==================== CONSTANTS ====================

DEFAULT_MASTER_PORT = 9000
DEFAULT_HEARTBEAT_INTERVAL = 5.0   # seconds
DEFAULT_WORKER_TIMEOUT = 30.0      # seconds without heartbeat = offline
DEFAULT_JOB_POLL_INTERVAL = 2.0    # seconds between job-pull attempts when idle

# Recognized HTTP endpoints (kept here so master + worker stay in sync)
EP_REGISTER = "/api/register"
EP_HEARTBEAT = "/api/heartbeat"
EP_PULL_JOB = "/api/pull_job"
EP_COMPLETE = "/api/complete"
EP_UPLOAD = "/api/upload"
EP_SUBMIT = "/api/submit"
EP_STATUS = "/api/status"
EP_DASHBOARD = "/"
EP_TRIPS_PREFIX = "/api/trips/"     # GET /api/trips/<job_id> -> raw XML

# Synced-spatial mode
EP_SYNC_BARRIER = "/api/sync/barrier"           # POST: report outgoing handoffs, block until all peers arrived, return incoming
EP_SYNC_EDGES_PREFIX = "/api/sync/edges/"       # GET /api/sync/edges/<parent_sim_id> -> JSON edge_owner_map
EP_SYNC_FINISH = "/api/sync/finish"             # POST: this group reached EOF; release peers from barrier

DEFAULT_SYNC_BARRIER_TIMEOUT = 120.0     # seconds; if peers don't arrive at a step in this window, give up

# Fast socket barrier (replaces the per-step HTTP round trip). The master
# opens a raw TCP server on ``master_port + BARRIER_PORT_OFFSET`` and each
# worker keeps a single persistent connection open for the whole run, so
# there is no TCP handshake / HTTP header parsing per simulation step.
BARRIER_PORT_OFFSET = 1
BARRIER_MAGIC = b"VKB1"                   # 4-byte protocol marker

# ---- Pluggable barrier transports (synced-spatial) ----
# Each alternative transport server runs on ``master_port + <offset>`` and all
# of them delegate the rendezvous to the SAME ``SyncSession.submit_handoffs``,
# so they are interchangeable and directly comparable in benchmarks. The
# worker picks one per run via ``barrier_transport`` in edge_owner.json.
BARRIER_PORT_OFFSETS: Dict[str, int] = {
    "tcp": 1,
    "udp": 2,
    "zmq": 3,
    "grpc": 4,
}
# Transports that travel over the existing HTTP server (no extra port).
HTTP_TRANSPORTS = ("http",)
# All transport names recognised by the selector. ``http`` is the universal
# fallback (always available, slowest); ``tcp`` is the default fast path.
ALL_TRANSPORTS = ("http", "tcp", "udp", "zmq", "grpc")
DEFAULT_BARRIER_TRANSPORT = "tcp"
