"""The ExecutionBackend boundary (ADR-0016 §2, ADR-0017): Aegra is the first
adapter, with A2A a possible future one. Generic router/approval/viewer code
depends on this package, not on a concrete transport module directly.
"""
