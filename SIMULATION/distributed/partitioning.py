"""
Partition-strategy registry for distributed (synced-spatial) simulation.

A *partition strategy* decides how the network edges are split among the N
workers. There are three kinds:

* ``corridor`` -- route-classification split into the predefined corridor
  groups (``AN``/``CV`` for 2, ``A7``/``N340``/``CV1``[/``N225``] for 3-4).
  A vehicle tends to stay inside one corridor for the whole trip, so there
  are relatively few hand-offs.
* ``balanced`` -- load-balanced *geographic strips* along the X (west<->east)
  or Y (south<->north) axis, sliced so every strip carries ~1/N of the
  traffic load. Trips traverse the strips in order, so a single vehicle is
  handed off across *every* worker in turn. This is the configuration that
  actually exercises inter-PC vehicle circulation.
* ``highway`` -- split every edge to the nearest of the two parallel
  highways (A7 vs N340); vehicles that weave between them are handed off.

Strategy names (used in the submit payload / orchestrator matrix):

    corridor-2, corridor-3, corridor-4
    highway-2
    balanced-x-2, balanced-x-3, balanced-x-4
    balanced-y-2, balanced-y-3, balanced-y-4
    balanced-2  (alias of balanced-x-2, kept for backwards compat)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class PartitionSpec:
    name: str
    kind: str            # "corridor" | "balanced" | "highway"
    num_groups: int
    axis: str = "x"      # only meaningful for kind == "balanced"

    @property
    def balanced(self) -> bool:
        return self.kind == "balanced"

    @property
    def highway(self) -> bool:
        return self.kind == "highway"


# Canonical list of strategies the orchestrator can sweep over.
PARTITION_NAMES: List[str] = [
    "corridor-2", "corridor-3", "corridor-4",
    "highway-2",
    "balanced-x-2", "balanced-x-3", "balanced-x-4",
    "balanced-y-2", "balanced-y-3", "balanced-y-4",
]


def parse_partition(name: Optional[str]) -> PartitionSpec:
    """Parse a strategy name into a :class:`PartitionSpec`.

    ``None`` / unknown defaults to ``balanced-x-2``.
    """
    if not name:
        return PartitionSpec("balanced-x-2", "balanced", 2, "x")
    n = str(name).strip().lower()

    if n in ("balanced", "balanced-2"):
        return PartitionSpec("balanced-x-2", "balanced", 2, "x")

    parts = n.split("-")
    head = parts[0]

    if head == "corridor":
        ng = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 2
        return PartitionSpec(f"corridor-{ng}", "corridor", ng, "x")

    if head == "highway":
        ng = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 2
        return PartitionSpec(f"highway-{ng}", "highway", ng, "x")

    if head == "balanced":
        # balanced-<axis>-<n>
        axis = "x"
        ng = 2
        rest = parts[1:]
        for p in rest:
            if p in ("x", "y"):
                axis = p
            elif p.isdigit():
                ng = int(p)
        return PartitionSpec(f"balanced-{axis}-{ng}", "balanced", ng, axis)

    # Unknown -> safe default
    return PartitionSpec("balanced-x-2", "balanced", 2, "x")


def groups_for(spec: PartitionSpec) -> List[str]:
    """Return the ordered group labels produced by ``spec``."""
    if spec.kind == "balanced":
        from SIMULATION.distributed.balanced_partition import group_labels
        return group_labels(spec.num_groups)
    if spec.kind == "highway":
        return ["A7", "N340"]
    # corridor
    from SIMULATION.distributed.corridors import get_default_split
    return get_default_split(spec.num_groups)


__all__ = ["PartitionSpec", "PARTITION_NAMES", "parse_partition", "groups_for"]
