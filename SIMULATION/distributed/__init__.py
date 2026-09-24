"""
Distributed SUMO simulation module.

Two coordination modes:

* **batch** (Phase 1 - job parallelism): a queue of independent simulation
  jobs is consumed by N workers. Each worker runs a full simulation by itself
  (one ``main.py`` invocation per job). Speedup is ~linear in the number of
  workers.

* **spatial** (Phase 2 - corridor decomposition): a single simulation is
  split into K sub-jobs, one per corridor group (e.g. AN={A7,N340} and
  CV={CV,N225}). Each sub-job runs the full network but with only the trips
  of its corridor. After all sub-jobs finish, an aggregator merges their
  outputs into a single coherent ``simulation_info``.

Public entry points:
    python -m SIMULATION.distributed master --port 9000
    python -m SIMULATION.distributed worker --master 192.168.1.10:9000
    python -m SIMULATION.distributed submit ...
"""

from .protocol import (
    JobConfig,
    JobResult,
    JobStatus,
    WorkerInfo,
    WorkerStatus,
)

__all__ = [
    "JobConfig",
    "JobResult",
    "JobStatus",
    "WorkerInfo",
    "WorkerStatus",
]
