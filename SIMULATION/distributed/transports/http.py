"""HTTP barrier transport (universal fallback).

Rides the master's existing HTTP handler (``/api/sync/barrier`` and
``/api/sync/finish``); no extra server is started. Slowest transport
(one POST + connection per step) but always available, used as the safety
net when a faster transport fails to connect.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib import request as urllib_request

from SIMULATION.distributed.transports.base import BarrierClient


def _post_json(url: str, payload: dict, timeout: float) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


class HttpClient(BarrierClient):
    name = "http"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._base = f"http://{self.host}:{self.port}"

    def connect(self) -> None:
        # Stateless; nothing to do. The master HTTP server is already up.
        return None

    def step(self, step: int, outgoing: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload = {
            "parent_sim_id": self.parent_sim_id,
            "group": self.group,
            "step": step,
            "outgoing": outgoing,
        }
        return _post_json(f"{self._base}/api/sync/barrier", payload,
                          timeout=self.timeout + 30.0)

    def finish(self) -> None:
        try:
            _post_json(f"{self._base}/api/sync/finish",
                       {"parent_sim_id": self.parent_sim_id,
                        "group": self.group},
                       timeout=15.0)
        except Exception:
            pass

    def close(self) -> None:
        return None


__all__ = ["HttpClient"]
