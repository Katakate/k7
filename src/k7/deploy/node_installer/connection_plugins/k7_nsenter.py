"""Ansible connection: the controller is a privileged hostPID container, the target is its host.

Commands run in PID 1's namespaces, so systemctl, mount and /proc/mounts behave
as on the host. Files cross through the host's / mounted at HOST_ROOT.
"""

from __future__ import annotations

import shutil

from ansible.plugins.connection.local import Connection as LocalConnection

DOCUMENTATION = """
name: k7_nsenter
short_description: Run tasks on the host of a privileged hostPID container
description:
  - Wraps every command in nsenter --target 1 --all; transfers files through /host.
author: K7
"""

HOST_ROOT = "/host"
NSENTER = "nsenter --target 1 --all --root --wd -- "


class Connection(LocalConnection):
    transport = "k7_nsenter"

    def exec_command(self, cmd: str, in_data: bytes | None = None, sudoable: bool = True) -> tuple[int, bytes, bytes]:
        return super().exec_command(NSENTER + cmd, in_data=in_data, sudoable=sudoable)

    def put_file(self, in_path: str, out_path: str) -> None:
        super().put_file(in_path, HOST_ROOT + out_path)

    def fetch_file(self, in_path: str, out_path: str) -> None:
        shutil.copyfile(HOST_ROOT + in_path, out_path)
