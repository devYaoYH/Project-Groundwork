from a2a_agent.seat_plugins import SeatPlugin


def policy(context):
    from .runtime_strategy import HiddenProfileStrategy

    return HiddenProfileStrategy(client=context.model_client())


seat_plugin = SeatPlugin("hidden_profile", policy)
