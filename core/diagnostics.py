"""Compatibility module for the diagnostic tracing helper.

The implementation lives in :mod:`core.diagnostic`; this plural module name
is retained because the orchestrator and existing integrations import
``core.diagnostics``.
"""

from core.diagnostic import trace

__all__ = ["trace"]
