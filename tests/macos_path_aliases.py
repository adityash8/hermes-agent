"""Spellings of an existing macOS path that open the same inode yet survive ``realpath``.

Firmlinks (``/System/Volumes/Data/…``), the ``/.nofollow`` and ``/.resolve/1`` namespaces and
volfs (``/.vol/<dev>/<ino>``) all reach the file, so a guard comparing resolved strings must also
test the kernel's spelling (``utils.path_spellings``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ALIAS_KINDS = ("data-volume", "nofollow", "resolve", "volfs", "volfs-inode")

_PREFIXES = {"data-volume": "/System/Volumes/Data", "nofollow": "/.nofollow", "resolve": "/.resolve/1"}


def macos_alias(path: Path, kind: str) -> str:
    """``path`` spelled through ``kind``. ``volfs`` names the parent dir by inode (the file may
    not exist yet); ``volfs-inode`` names the existing file itself, leaving no basename to match.
    Skips when the host lacks that namespace."""
    real = os.path.realpath(path)
    parent, name = os.path.split(real)
    if kind == "volfs-inode":
        st = os.stat(real)
        alias, probe, target = f"/.vol/{st.st_dev}/{st.st_ino}", f"/.vol/{st.st_dev}/{st.st_ino}", real
    elif kind == "volfs":
        st = os.stat(parent)
        probe = f"/.vol/{st.st_dev}/{st.st_ino}"
        alias, target = f"{probe}/{name}", parent
    else:
        alias = _PREFIXES[kind] + real
        probe, target = os.path.dirname(alias), parent
    if not (os.path.exists(probe) and os.path.samefile(probe, target)):
        pytest.skip(f"no {kind} alias on this host")
    return alias
