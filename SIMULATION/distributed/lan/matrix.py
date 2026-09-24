"""Experiment matrix for the LAN benchmark sweep.

Staged design so every dimension the study cares about is covered while the
total run count stays feasible overnight:

* baselines: 1 instance per (vehicles x accident)  -> speedup denominators
* Stage T (transports): fixed partition, fixed load, sweep tcp/udp/zmq/grpc/http
* Stage P (partitions): fixed transport, fixed load, sweep all partitions
                        (corridor/highway/balanced-x/balanced-y, 2/3/4 ways)
* Stage V (vehicle scaling): fixed best partition+transport, sweep 8/10/12/15k
* Stage A (accident): same as Stage V but with an incident (reroute load)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from SIMULATION.distributed.partitioning import parse_partition

VEHICLE_COUNTS = [8000, 10000, 12000, 15000]
TRANSPORTS = ["tcp", "udp", "zmq", "grpc", "http"]
PARTITIONS = [
    "corridor-2", "corridor-3", "corridor-4",
    "highway-2",
    "balanced-x-2", "balanced-x-3", "balanced-x-4",
    "balanced-y-2", "balanced-y-3", "balanced-y-4",
]

# Reference points for the staged sweep.
REF_PARTITION = "balanced-x-2"
REF_TRANSPORT = "tcp"
REF_VEHICLES = 10000

TRIPS_FOR = {
    8000: "fullnet_trips_intelligent_99_8000.xml",
    10000: "fullnet_trips_intelligent_99.xml",
    12000: "fullnet_trips_intelligent_99_12000.xml",
    15000: "fullnet_trips_intelligent_99_15000.xml",
}

# Per-road trips mapping; falls back to TRIPS_FOR (Almenara naming).
TRIPS_FOR_ROAD = {
    "modified_4ways_all": TRIPS_FOR,
    "rotterdam_arterial": {
        15000: "rotterdam_trips_intelligent_99_15000.xml",
        20000: "rotterdam_trips_intelligent_99_20000.xml",
        30000: "rotterdam_trips_intelligent_99_30000.xml",
    },
}


def trips_for(road: str, vehicles: int) -> str:
    return TRIPS_FOR_ROAD.get(road, TRIPS_FOR)[vehicles]


@dataclass
class Experiment:
    kind: str                 # "baseline" | "distributed"
    vehicles: int
    accident: bool
    partition: Optional[str] = None
    transport: Optional[str] = None
    stage: str = ""

    @property
    def num_groups(self) -> int:
        if self.kind == "baseline" or not self.partition:
            return 1
        return parse_partition(self.partition).num_groups

    @property
    def label(self) -> str:
        acc = "acc" if self.accident else "noacc"
        if self.kind == "baseline":
            return f"baseline_{self.vehicles}_{acc}"
        return (f"{self.partition}_{self.transport}_{self.vehicles}_{acc}")


def build_custom_matrix(partitions: List[str], vehicles: List[int],
                        accidents: List[bool], transports: List[str],
                        max_groups: int = 6) -> List[Experiment]:
    """Cartesian-product matrix for a focused sweep, with the baselines needed
    as speedup denominators prepended."""
    exps: List[Experiment] = []
    seen = set()

    def add(e: Experiment):
        if e.num_groups > max_groups or e.label in seen:
            return
        seen.add(e.label)
        exps.append(e)

    for v in vehicles:
        for acc in accidents:
            add(Experiment("baseline", v, acc, stage="baseline"))
    for p in partitions:
        for v in vehicles:
            for acc in accidents:
                for t in transports:
                    add(Experiment("distributed", v, acc, partition=p,
                                   transport=t, stage="C"))
    return exps


def build_matrix(max_groups: int = 6) -> List[Experiment]:
    """Return the ordered list of experiments (baselines first)."""
    exps: List[Experiment] = []
    seen = set()

    def add(e: Experiment):
        if e.num_groups > max_groups:
            return
        key = e.label
        if key in seen:
            return
        seen.add(key)
        exps.append(e)

    # Baselines for everything we will need a denominator for.
    for v in VEHICLE_COUNTS:
        for acc in (False, True):
            add(Experiment("baseline", v, acc, stage="baseline"))

    # Stage T: transports @ ref partition/load, no accident
    for t in TRANSPORTS:
        add(Experiment("distributed", REF_VEHICLES, False,
                       partition=REF_PARTITION, transport=t, stage="T"))

    # Stage P: partitions @ ref transport/load, no accident
    for p in PARTITIONS:
        add(Experiment("distributed", REF_VEHICLES, False,
                       partition=p, transport=REF_TRANSPORT, stage="P"))

    # Stage V: vehicle scaling @ ref partition/transport, no accident
    for v in VEHICLE_COUNTS:
        add(Experiment("distributed", v, False,
                       partition=REF_PARTITION, transport=REF_TRANSPORT,
                       stage="V"))

    # Stage A: accident @ ref partition/transport, all loads
    for v in VEHICLE_COUNTS:
        add(Experiment("distributed", v, True,
                       partition=REF_PARTITION, transport=REF_TRANSPORT,
                       stage="A"))

    return exps


__all__ = ["Experiment", "build_matrix", "VEHICLE_COUNTS", "TRANSPORTS",
           "PARTITIONS", "TRIPS_FOR", "TRIPS_FOR_ROAD", "trips_for",
           "REF_PARTITION", "REF_TRANSPORT", "REF_VEHICLES"]
