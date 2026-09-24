"""Thin paramiko wrapper: password or key auth, run commands, sftp transfers.

Windows-friendly (no native ssh/scp needed). Used by the LAN orchestrator to
install keys, deploy code, start/stop master & workers and collect results.
"""

from __future__ import annotations

import os
import posixpath
import stat
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import paramiko


@dataclass
class CmdResult:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class Ssh:
    """One SSH connection to a single host."""

    def __init__(self, host: str, user: str,
                 password: Optional[str] = None,
                 key_filename: Optional[str] = None,
                 port: int = 22, timeout: float = 15.0):
        self.host = host
        self.user = user
        self.password = password
        self.key_filename = key_filename
        self.port = port
        self.timeout = timeout
        self._cli: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None

    def connect(self) -> "Ssh":
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(
            self.host, port=self.port, username=self.user,
            password=self.password, key_filename=self.key_filename,
            timeout=self.timeout, banner_timeout=self.timeout,
            auth_timeout=self.timeout, allow_agent=False,
            look_for_keys=self.key_filename is None and self.password is None,
        )
        self._cli = cli
        return self

    def run(self, command: str, timeout: Optional[float] = None,
            get_pty: bool = False) -> CmdResult:
        assert self._cli is not None, "not connected"
        stdin, stdout, stderr = self._cli.exec_command(
            command, timeout=timeout, get_pty=get_pty)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return CmdResult(rc, out, err)

    def start_background(self, command: str, log_path: str) -> CmdResult:
        """Launch a long-running command detached via nohup, logging to
        ``log_path`` on the remote. Returns immediately with the PID."""
        # NOTE: log_path may contain $HOME / ~ which must stay UNquoted so the
        # remote shell expands it (paths here never contain spaces).
        wrapped = (
            f"mkdir -p \"$(dirname {log_path})\"; "
            f"nohup bash -lc {_shquote(command)} "
            f"> {log_path} 2>&1 & echo $!"
        )
        return self.run(wrapped)

    # ---- sftp ----

    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            assert self._cli is not None
            self._sftp = self._cli.open_sftp()
        return self._sftp

    def mkdirs(self, remote_dir: str) -> None:
        sftp = self.sftp()
        parts = remote_dir.strip("/").split("/")
        cur = "/" if remote_dir.startswith("/") else ""
        for p in parts:
            cur = posixpath.join(cur, p) if cur else p
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass

    def put(self, local: str, remote: str) -> None:
        sftp = self.sftp()
        self.mkdirs(posixpath.dirname(remote))
        sftp.put(local, remote)

    def get(self, remote: str, local: str) -> None:
        sftp = self.sftp()
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        sftp.get(remote, local)

    def exists(self, remote: str) -> bool:
        try:
            self.sftp().stat(remote)
            return True
        except IOError:
            return False

    def get_dir(self, remote_dir: str, local_dir: str) -> int:
        """Recursively download a remote directory. Returns #files."""
        sftp = self.sftp()
        n = 0
        try:
            entries = sftp.listdir_attr(remote_dir)
        except IOError:
            return 0
        os.makedirs(local_dir, exist_ok=True)
        for e in entries:
            rpath = posixpath.join(remote_dir, e.filename)
            lpath = os.path.join(local_dir, e.filename)
            if stat.S_ISDIR(e.st_mode):
                n += self.get_dir(rpath, lpath)
            else:
                try:
                    sftp.get(rpath, lpath)
                    n += 1
                except IOError:
                    pass
        return n

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._cli is not None:
            try:
                self._cli.close()
            except Exception:
                pass
            self._cli = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def install_public_key(host: str, user: str, password: str,
                       pubkey_text: str, timeout: float = 15.0) -> CmdResult:
    """Append the public key to ~/.ssh/authorized_keys (idempotent)."""
    with Ssh(host, user, password=password, timeout=timeout) as s:
        marker = pubkey_text.strip().split()[1][:24]  # part of the key body
        cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys "
            "&& chmod 600 ~/.ssh/authorized_keys && "
            f"(grep -q {_shquote(marker)} ~/.ssh/authorized_keys || "
            f"echo {_shquote(pubkey_text.strip())} >> ~/.ssh/authorized_keys) "
            "&& echo INSTALLED"
        )
        return s.run(cmd)


__all__ = ["Ssh", "CmdResult", "install_public_key"]
