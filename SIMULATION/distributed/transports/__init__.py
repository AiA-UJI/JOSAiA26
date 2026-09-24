"""
Pluggable barrier transports for synced-spatial distribution.

All transports implement the *same* per-step rendezvous (delegated to
``MasterState.get_sync_session().submit_handoffs`` on the server side), so
they are drop-in interchangeable and directly comparable in benchmarks.

Server side (master): :func:`start_servers` launches one server per requested
transport on ``master_port + BARRIER_PORT_OFFSETS[name]``. ``http`` needs no
extra server (it rides the existing master HTTP handler).

Client side (worker): :func:`make_client` returns a :class:`BarrierClient`
for the chosen transport with a uniform API::

    client = make_client("tcp", host, port, parent_sim_id, group)
    client.connect()
    resp = client.step(step, outgoing)     # -> {"ok": bool, "incoming": [...]}
    client.finish()
    client.close()

Availability: ``http`` and ``tcp`` are always available (stdlib only). Any
other transport name raises :class:`TransportUnavailable` so the caller can
fall back to ``http``.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from SIMULATION.distributed.transports.base import (
    BarrierClient,
    TransportUnavailable,
    frame_recv,
    frame_send,
)


def make_client(transport: str, host: str, port: int,
                parent_sim_id: str, group: str,
                timeout: float = 30.0) -> BarrierClient:
    """Factory: build a connected-capable client for ``transport``."""
    t = (transport or "tcp").lower()
    if t == "tcp":
        from SIMULATION.distributed.transports.tcp import TcpClient
        return TcpClient(host, port, parent_sim_id, group, timeout)
    if t == "http":
        from SIMULATION.distributed.transports.http import HttpClient
        return HttpClient(host, port, parent_sim_id, group, timeout)
    raise TransportUnavailable(f"unknown transport {transport!r}")


def start_servers(state, host: str, base_port: int,
                  transports: List[str]) -> Dict[str, object]:
    """Start barrier servers for the requested transports.

    Returns ``{name: server_object}`` for the ones that started. ``http`` is
    skipped (served by the master HTTP handler). Unknown transports are
    skipped instead of raising, so the master keeps running and workers fall
    back to whatever is available.
    """
    from SIMULATION.distributed.protocol import BARRIER_PORT_OFFSETS
    import sys

    started: Dict[str, object] = {}
    for name in transports:
        name = name.lower()
        if name in ("http",):
            continue
        offset = BARRIER_PORT_OFFSETS.get(name)
        if offset is None:
            continue
        port = base_port + offset
        try:
            if name == "tcp":
                from SIMULATION.distributed.transports.tcp import TcpServer
                srv = TcpServer(host, port, state)
            else:
                continue
            srv.start()
            started[name] = srv
            print(f"[master] barrier transport '{name}' on {host}:{port}")
        except TransportUnavailable as e:
            print(f"[master] transport '{name}' unavailable: {e}")
        except OSError as e:
            print(f"[master] WARN: transport '{name}' failed to bind "
                  f"{host}:{port}: {e}")
    return started


__all__ = [
    "BarrierClient", "TransportUnavailable", "make_client", "start_servers",
    "frame_send", "frame_recv",
]
