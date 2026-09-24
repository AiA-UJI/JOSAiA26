"""Common interface + framing helpers for barrier transports."""

from __future__ import annotations

import abc
import json
import socket
import struct
from typing import Any, Dict, List, Optional

_LEN = struct.Struct(">I")


class TransportUnavailable(RuntimeError):
    """Raised when a transport's optional dependency is not installed."""


# ---- length-prefixed JSON framing over a stream socket (TCP) ----

def frame_send(sock: socket.socket, obj: Any) -> None:
    body = json.dumps(obj).encode("utf-8")
    sock.sendall(_LEN.pack(len(body)) + body)


def _recv_exactly(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def frame_recv(sock: socket.socket) -> Optional[Any]:
    header = _recv_exactly(sock, 4)
    if header is None:
        return None
    (length,) = _LEN.unpack(header)
    if length == 0:
        return {}
    body = _recv_exactly(sock, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


# ---- client interface ----

class BarrierClient(abc.ABC):
    """Uniform per-step barrier client.

    Concrete transports implement :meth:`connect`, :meth:`step`,
    :meth:`finish` and :meth:`close`. ``name`` identifies the transport in
    stats/CSV.
    """

    name: str = "base"

    def __init__(self, host: str, port: int, parent_sim_id: str,
                 group: str, timeout: float = 30.0):
        self.host = host
        self.port = int(port)
        self.parent_sim_id = parent_sim_id
        self.group = group
        self.timeout = timeout

    @abc.abstractmethod
    def connect(self) -> None:
        ...

    @abc.abstractmethod
    def step(self, step: int, outgoing: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Submit ``outgoing`` for ``step``; return ``{"ok", "incoming"}``."""

    @abc.abstractmethod
    def finish(self) -> None:
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...


# ---- shared server-side dispatch ----

def dispatch_message(state, parent_id: Optional[str], group: Optional[str],
                     msg: Dict[str, Any]) -> Dict[str, Any]:
    """Translate one decoded barrier message into a SyncSession call.

    Shared by every non-HTTP transport so they have *identical* semantics.
    """
    sess = state.get_sync_session(parent_id)
    if sess is None:
        return {"ok": False, "error": "no sync session"}
    if msg.get("finish"):
        sess.finish(group)
        return {"ok": True}
    step = int(msg.get("step", -1))
    outgoing = msg.get("outgoing") or []
    return sess.submit_handoffs(step, group, outgoing)


__all__ = [
    "TransportUnavailable", "BarrierClient",
    "frame_send", "frame_recv", "dispatch_message",
]
