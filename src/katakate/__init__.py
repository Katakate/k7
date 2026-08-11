"""Deprecated compatibility shim — use ``k7_sdk`` (``pip install k7-sdk``)."""

from __future__ import annotations

import warnings

from k7_sdk import AsyncClient, Client, SandboxProxy

warnings.warn(
    "The 'katakate' package is deprecated; pip install k7-sdk and use: from k7_sdk import Client",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["Client", "AsyncClient", "SandboxProxy"]
