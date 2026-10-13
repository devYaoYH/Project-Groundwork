from a2a_agent.seat_plugins import SeatPlugin


def policy(context):
    from .runtime_strategy import BuyerSellerStrategy

    return BuyerSellerStrategy(client=context.model_client())


seat_plugin = SeatPlugin("buyer_seller", policy)
