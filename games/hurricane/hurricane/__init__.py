"""Hurricane landfall environment.

Importing this package registers the environment via ``environment.py``'s
``register_environment`` side effect. See ``SPEC.md`` for the protocol.
"""

from hurricane.environment import HurricaneConfig, HurricaneEnvironment

__all__ = ["HurricaneConfig", "HurricaneEnvironment"]
