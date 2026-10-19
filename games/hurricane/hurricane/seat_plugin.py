from a2a_agent.seat_plugins import SeatPlugin


def policy(context):
    from .runtime_strategy import HurricaneStrategy

    return HurricaneStrategy(client=context.model_client())


seat_plugin = SeatPlugin("hurricane", policy)
