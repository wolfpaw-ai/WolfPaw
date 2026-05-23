"""Sandbox subsystem — abstract interface + provider adapters + manager.

Public exports kept tight; concrete providers are imported lazily by
the manager so adapters that need optional deps (docker, e2b) don't
pull them in unless used.
"""

from wolfpaw.sandbox.base import (
    CodeResult,
    InstallResult,
    Sandbox,
    SandboxError,
    SandboxFatalError,
)
from wolfpaw.sandbox.manager import SandboxManager, get_manager, reset_manager

__all__ = [
    "CodeResult",
    "InstallResult",
    "Sandbox",
    "SandboxError",
    "SandboxFatalError",
    "SandboxManager",
    "get_manager",
    "reset_manager",
]
