from a2a_agent.seat_plugins import SeatPlugin


def policy(context):
    from .runtime_strategy import NegotiationStrategy

    return NegotiationStrategy(
        config=context.config if context.config.get("type") == "llm" else None
    )


seat_plugin = SeatPlugin(
    "negotiation", policy, supported_policy_kinds=frozenset({"scripted", "heuristic", "llm"})
)
