"""
Unified entry point: ``python -m SIMULATION.distributed <subcommand> ...``

Subcommands::

    master      Run the master HTTP server.
    worker      Run a worker daemon connected to a master.
    submit      Submit simulation(s) to a running master.
    split       Pre-split a trips XML by corridor (offline).
    aggregate   Aggregate sub-run outputs (offline).

Each subcommand simply forwards argv to its dedicated module so that the
existing module-level CLIs keep working too::

    python -m SIMULATION.distributed.master --port 9000
    python -m SIMULATION.distributed master --port 9000

are equivalent.
"""

from __future__ import annotations

import sys
from typing import List


def _help() -> int:
    print(__doc__)
    return 0


def main(argv: List[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        return _help()

    sub = argv.pop(0)
    if sub == "master":
        from SIMULATION.distributed.master import main as run
        return run(argv)
    if sub == "worker":
        from SIMULATION.distributed.worker import main as run
        return run(argv)
    if sub == "submit":
        from SIMULATION.distributed.submit import main as run
        return run(argv)
    if sub == "split":
        from SIMULATION.distributed.trip_splitter import main as run
        return run(argv)
    if sub == "aggregate":
        from SIMULATION.distributed.aggregator import main as run
        return run(argv)
    if sub == "package":
        from SIMULATION.distributed.package_worker import main as run
        return run(argv)

    print(f"[distributed] unknown subcommand: {sub}")
    return _help() or 2


if __name__ == "__main__":
    sys.exit(main())
