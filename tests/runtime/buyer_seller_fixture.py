"""Credential-free controlled provider for the real bargaining container gate."""

import hashlib
import json
from copy import deepcopy

CONFIG = {"environment_id": "buyer_seller", "seller_cost": 7.51, "buyer_value": 33.27,
          "num_items": 2, "max_rounds": 4, "opening_price": 25,
          "discount_factor": .5, "enforce_monotonic_offers": True}
LINES = (["PRICE: $20.126", "PRICE: 40", "no number", ""],
         ["REJECT", "ACCEPT", "ACCEPT and REJECT", "ACCEPT"])


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class MockProvider:
    def __init__(self):
        self.lines = None
        self.messages = []

    def oneshot(self, messages):
        self.messages.append(deepcopy(messages))
        if self.lines is None:
            self.lines = iter(LINES[0 if "SELLER" in messages[0]["content"] else 1])
        return next(self.lines)
