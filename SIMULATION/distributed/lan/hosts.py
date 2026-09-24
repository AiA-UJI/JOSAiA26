"""Inventory of lab machines for the distributed benchmark sweep.

Override any of these from the environment without editing the file:

    VK_LAN_USER       ssh user            (default: practicas)
    VK_LAN_PASSWORD   initial ssh password (default: practicas) -- only used
                      once to install the public key
    VK_LAN_HOSTS      comma-separated FQDNs (default: labrob07..12)
    VK_LAN_MASTER     host that runs the master + barrier servers
                      (default: first host)
    VK_LAN_REMOTE_DIR remote checkout dir (default: ~/VehicleKnowledge)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

DEFAULT_HOSTS = [f"labrob{n:02d}.act.uji.es" for n in range(7, 13)]


@dataclass
class LanConfig:
    user: str
    password: str
    hosts: List[str]
    master: str
    remote_dir: str
    python: str = "python3"

    @property
    def workers_hosts(self) -> List[str]:
        return list(self.hosts)


def load_config() -> LanConfig:
    user = os.environ.get("VK_LAN_USER", "usuario")
    password = os.environ.get("VK_LAN_PASSWORD", "practicas")
    hosts_env = os.environ.get("VK_LAN_HOSTS")
    hosts = ([h.strip() for h in hosts_env.split(",") if h.strip()]
             if hosts_env else list(DEFAULT_HOSTS))
    master = os.environ.get("VK_LAN_MASTER", hosts[0] if hosts else "")
    remote_dir = os.environ.get("VK_LAN_REMOTE_DIR", "VehicleKnowledge")
    return LanConfig(user=user, password=password, hosts=hosts,
                     master=master, remote_dir=remote_dir)


__all__ = ["LanConfig", "load_config", "DEFAULT_HOSTS"]
