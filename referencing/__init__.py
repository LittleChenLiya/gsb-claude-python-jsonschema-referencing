"""
Cross-specification, implementation-agnostic JSON referencing.
"""

from referencing._core import (
    Anchor,
    Registry,
    Resource,
    Specification,
    TraceEntry,
)

__all__ = ["Anchor", "Registry", "Resource", "Specification", "TraceEntry"]
