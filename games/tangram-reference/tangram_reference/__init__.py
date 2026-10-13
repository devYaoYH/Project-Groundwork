"""Tangram Reference environment.

Importing this package registers the environment via ``environment.py``'s ``register_environment``
side effect. See ``SPEC.md`` for the protocol.
"""

from tangram_reference.environment import (
    TangramReferenceConfig,
    TangramReferenceEnvironment,
)

__all__ = ["TangramReferenceConfig", "TangramReferenceEnvironment"]
