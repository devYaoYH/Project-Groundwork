"""Buyer-seller bargaining environment.

Importing this package registers the environment via ``environment.py``'s ``register_environment``
side effect. See ``SPEC.md`` for the protocol it implements.
"""

from buyer_seller.game import BuyerSellerConfig, BuyerSellerGame

__all__ = ["BuyerSellerConfig", "BuyerSellerGame"]
