"""Hidden Profile environment.

Importing this package registers the environment via ``environment.py``'s ``register_environment``
side effect. See ``SPEC.md`` for the protocol.
"""

from hidden_profile.environment import HiddenProfileConfig, HiddenProfileEnvironment

__all__ = ["HiddenProfileConfig", "HiddenProfileEnvironment"]
