"""Buyer-seller bargaining game.

Importing this package registers the game via ``game.py``'s ``register_game``
side effect. See ``SPEC.md`` for the protocol it implements.
"""

from buyer_seller.game import BuyerSellerConfig, BuyerSellerGame

__all__ = ["BuyerSellerConfig", "BuyerSellerGame"]
