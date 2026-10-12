"""Map Director environment.

Importing this package registers the environment via ``environment.py``'s ``register_environment``
side effect. See ``SPEC.md`` for the protocol.
"""

from map_director.environment import MapDirectorConfig, MapDirectorEnvironment

__all__ = ["MapDirectorConfig", "MapDirectorEnvironment"]
