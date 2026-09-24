"""
Corridor definitions for spatial decomposition.

The full network ``modified_4ways_all`` exposes four parallel corridors that
join two common endpoints (entrance from N225 west / exit on the eastern
A7 roundabout area). For the spatial mode we group corridors so each group
runs on one worker:

* ``AN`` group: A7 (motorway) + N340 (national road) - higher-capacity routes
* ``CV`` group: CV-10 / CV-230 + N225 - lower-capacity alternatives

When scaling to 4 workers, each corridor can run alone:
``A7``, ``N340``, ``CV``, ``N225``.

The "key edges" below are sentinel edges located near the centre of each
corridor. Every trip is classified by computing its free-flow route and
checking which key edges it crosses; the trip is then routed to the worker
owning that corridor.

Both directions of each corridor (positive and negative edge IDs) are listed
so that trips going east->west and west->east are matched equally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Set


# Edge sentinels per corridor. These were picked to be:
#   1) entirely contained in the corridor (not in shared shoulders)
#   2) likely to be traversed by ANY trip that uses that corridor
#
# If a trip never crosses any sentinel it is classified as ``OTHER`` and
# routed to the AN group by default (motorway/national absorb spillover).
CORRIDOR_KEY_EDGES: Dict[str, Set[str]] = {
    # A7 motorway core
    "A7": {
        "442062121", "428987668", "467787493", "467787494",
        "68948119", "467787489", "467787490", "68948128",
    },
    # N-340 national road core
    "N340": {
        "114536835#1", "114536835#2", "114536835#3",
        "1304778652", "1304778660",
        "-114536835#1", "-114536835#2", "-114536835#3",
        "-1304778652", "-1304778660",
    },
    # CV-10 / CV-230 (and their connectors)
    "CV": {
        "165826281", "165826282#0", "165826283#0", "165826283#3",
        "165826286#0", "165826286#3", "165826286#5",
        "488675602#0", "488675602#1",
        "-165826281", "-488675602#0", "-488675602#1",
    },
    # N-225 spine
    "N225": {
        "22562073#0", "22562073#3", "22562073#5", "22562073#8",
        "22562073#10", "22562073#12",
        "-22562073#0", "-22562073#3", "-22562073#5", "-22562073#8",
        "-22562073#10", "-22562073#12",
    },
}


# Predefined corridor groups. The keys are human-friendly labels surfaced in
# the GUI; values are sets of corridor names that the worker simulates.
CORRIDOR_GROUPS: Dict[str, FrozenSet[str]] = {
    # 2-way split (default for 2 PCs)
    "AN":   frozenset({"A7", "N340"}),
    "CV":   frozenset({"CV", "N225"}),
    # 4-way split (one corridor per PC)
    "A7":   frozenset({"A7"}),
    "N340": frozenset({"N340"}),
    "CV1":  frozenset({"CV"}),
    "N225": frozenset({"N225"}),
}


# Predefined splits. Each split is an ordered tuple of group labels. Pick
# the split based on ``--workers N``: the first split with N groups wins.
SPLITS: Dict[int, List[str]] = {
    2: ["AN", "CV"],
    3: ["A7", "N340", "CV1"],   # CV1 absorbs N225 trips when --workers=3
    4: ["A7", "N340", "CV1", "N225"],
}


@dataclass(frozen=True)
class Corridor:
    name: str
    key_edges: FrozenSet[str]


def get_default_split(num_groups: int) -> List[str]:
    """Return the canonical group ordering for ``num_groups`` workers."""
    if num_groups in SPLITS:
        return list(SPLITS[num_groups])
    if num_groups <= 1:
        return ["AN+CV"]   # single group = no split, used for batch mode only
    # Fallback: split A7+N340 across half the workers, CV+N225 across the rest
    half = max(1, num_groups // 2)
    groups = []
    for i in range(num_groups):
        groups.append(f"GROUP{i}")
    return groups


def classify_route(route_edges: List[str]) -> Optional[str]:
    """Classify a free-flow route into one of the basic corridors.

    Returns the corridor name (``A7`` / ``N340`` / ``CV`` / ``N225``) of the
    first key edge encountered, or ``None`` if no key edge is matched.
    """
    edge_set = set(route_edges)
    # Order matters when trips cross more than one corridor: we prioritize
    # whichever route the trip spends most edges on.
    best_corridor: Optional[str] = None
    best_overlap = 0
    for corridor_name, keys in CORRIDOR_KEY_EDGES.items():
        overlap = len(edge_set & keys)
        if overlap > best_overlap:
            best_overlap = overlap
            best_corridor = corridor_name
    return best_corridor


def assign_to_group(corridor: Optional[str], split: List[str]) -> str:
    """Map a basic corridor name to a group label that exists in ``split``."""
    if corridor is None:
        # Trip didn't hit any sentinel; default to first group (typically the
        # motorway one, which is the safest place to absorb spillover).
        return split[0]

    for group_name in split:
        if group_name in CORRIDOR_GROUPS and corridor in CORRIDOR_GROUPS[group_name]:
            return group_name

    # Group not in this split (e.g. CV trip but split doesn't include CV1):
    # fall back to the first group as catch-all.
    return split[0]


__all__ = [
    "CORRIDOR_KEY_EDGES",
    "CORRIDOR_GROUPS",
    "SPLITS",
    "Corridor",
    "assign_to_group",
    "classify_route",
    "get_default_split",
]
