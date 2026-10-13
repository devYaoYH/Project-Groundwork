from a2a_agent.seat_plugins import SeatPlugin


def policy(context):
    from .runtime_strategy import TangramReferenceStrategy

    return TangramReferenceStrategy(client=context.model_client())


seat_plugin = SeatPlugin("tangram_reference", policy)
