"""
Fast TCP barrier server for synced-spatial mode.

The HTTP barrier (one ``urllib`` POST per simulation step) re-opens a TCP
connection on every step, so the handshake + HTTP header parsing dominate
the per-step cost and cancel the benefit of splitting the work. This module
replaces that hot path with a raw TCP server where each worker keeps a
**single persistent connection** open for the whole run.

Wire protocol (each frame = 4-byte big-endian length + JSON payload):

    worker -> server   HELLO   {"parent_sim_id": str, "group": str}
    server -> worker           {"ok": true}
    worker -> server   STEP    {"step": int, "outgoing": [handoff, ...]}
    server -> worker           {"ok": true, "incoming": [handoff, ...]}
    worker -> server   FINISH  {"finish": true}
    (server closes the connection)

The actual rendezvous logic is delegated to the existing
``SyncSession.submit_handoffs`` / ``finish`` on the master state, so both
the HTTP and the socket path share identical barrier semantics.
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import threading
from typing import Any, Optional, Tuple

_LEN = struct.Struct(">I")


def send_msg(sock: socket.socket, obj: Any) -> None:
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


def recv_msg(sock: socket.socket) -> Optional[Any]:
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


class BarrierServer:
    """Threaded TCP server; one thread per persistent worker connection."""

    def __init__(self, host: str, port: int, state):
        self.host = host
        self.port = port
        self.state = state          # MasterState (has get_sync_session)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(64)
        self._running = True
        self._thread = threading.Thread(
            target=self._accept_loop, name="barrier-accept", daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            t = threading.Thread(
                target=self._handle, args=(conn, addr),
                name="barrier-conn", daemon=True)
            t.start()

    def _handle(self, conn: socket.socket, addr) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        parent_id: Optional[str] = None
        group: Optional[str] = None
        try:
            hello = recv_msg(conn)
            if not hello:
                return
            parent_id = hello.get("parent_sim_id")
            group = hello.get("group")
            send_msg(conn, {"ok": True})
            while True:
                msg = recv_msg(conn)
                if msg is None:
                    break
                sess = self.state.get_sync_session(parent_id)
                if sess is None:
                    send_msg(conn, {"ok": False, "error": "no sync session"})
                    break
                if msg.get("finish"):
                    sess.finish(group)
                    send_msg(conn, {"ok": True})
                    break
                step = int(msg.get("step", -1))
                outgoing = msg.get("outgoing") or []
                result = sess.submit_handoffs(step, group, outgoing)
                send_msg(conn, result)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            sys.stderr.write(f"[barrier] conn {addr} error: {e}\n")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


def connect(host: str, port: int, parent_sim_id: str, group: str,
            timeout: float = 30.0) -> socket.socket:
    """Open a persistent client connection and perform the HELLO handshake."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(None)  # blocking for the rest of the run
    send_msg(sock, {"parent_sim_id": parent_sim_id, "group": group})
    ack = recv_msg(sock)
    if not ack or not ack.get("ok"):
        raise RuntimeError(f"barrier HELLO rejected: {ack}")
    return sock


__all__ = ["BarrierServer", "send_msg", "recv_msg", "connect"]
