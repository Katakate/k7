"""Kernel limits node prep sets, read on the node the suite runs on."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_inotify_instance_limit_is_live_and_persisted():
    assert int(Path("/proc/sys/fs/inotify/max_user_instances").read_text()) >= 8192
    assert Path("/etc/sysctl.d/90-k7-inotify.conf").read_text() == "fs.inotify.max_user_instances = 8192\n"
