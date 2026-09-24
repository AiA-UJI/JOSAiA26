"""TCP barrier transport (persistent connection, length-framed JSON).

This is the default fast path: each worker keeps a single TCP connection open
for the whole run, eliminating the per-step handshake / HTTP header cost.
"""

from __future__ import annotations

import socket
import sys
import threading
from typing import Any, Dict, List, Optional

from SIMULATION.distributed.transports.base import (
    BarrierClient,
    dispatch_message,
    frame_recv,
    frame_send,
)


class TcpServer:
    """Threaded TCP server; one thread per persistent worker connection."""

    def __init__(self, host: str, port: int, state):
        self.host = host
        self.port = port
        self.state = state
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
            target=self._accept_loop, name="barrier-tcp-accept", daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr),
                             name="barrier-tcp-conn", daemon=True).start()

    def _handle(self, conn: socket.socket, addr) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        parent_id: Optional[str] = None
        group: Optional[str] = None
        try:
            hello = frame_recv(conn)
            if not hello:
                return
            parent_id = hello.get("parent_sim_id")
            group = hello.get("group")
            frame_send(conn, {"ok": True})
            while True:
                msg = frame_recv(conn)
                if msg is None:
                    break
                resp = dispatch_message(self.state, parent_id, group, msg)
                frame_send(conn, resp)
                if msg.get("finish"):
                    break
        except (OSError, ValueError) as e:
            sys.stderr.write(f"[barrier-tcp] conn {addr} error: {e}\n")
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


class TcpClient(BarrierClient):
    name = "tcp"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sock: Optional[socket.socket] = None

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port),
                                        timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)
        frame_send(sock, {"parent_sim_id": self.parent_sim_id,
                          "group": self.group})
        ack = frame_recv(sock)
        if not ack or not ack.get("ok"):
            raise RuntimeError(f"barrier HELLO rejected: {ack}")
        self._sock = sock

    def step(self, step: int, outgoing: List[Dict[str, Any]]) -> Dict[str, Any]:
        if self._sock is None:
            raise OSError("tcp barrier not connected")
        frame_send(self._sock, {"step": step, "outgoing": outgoing})
        resp = frame_recv(self._sock)
        if resp is None:
            raise OSError("barrier socket closed by peer")
        return resp

    def finish(self) -> None:
        if self._sock is None:
            return
        try:
            frame_send(self._sock, {"finish": True})
            frame_recv(self._sock)
        except Exception:
            pass

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


__all__ = ["TcpServer", "TcpClient"]
