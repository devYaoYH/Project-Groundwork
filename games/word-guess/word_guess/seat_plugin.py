from a2a_agent.seat_plugins import SeatPlugin


def policy(context):
    from .runtime_strategy import WordGuessStrategy

    return WordGuessStrategy(client=context.model_client())


seat_plugin = SeatPlugin("word_guess", policy)
