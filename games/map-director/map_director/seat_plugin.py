from a2a_agent.seat_plugins import SeatPlugin


def policy(context):
    from .runtime_strategy import MapDirectorStrategy

    return MapDirectorStrategy(client=context.model_client())


seat_plugin = SeatPlugin("map_director", policy)
