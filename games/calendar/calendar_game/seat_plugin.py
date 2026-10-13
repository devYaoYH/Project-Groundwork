from a2a_agent.seat_plugins import SeatPlugin


def policy(context):
    from a2a_agent.scripted import ScriptedPolicy
    from a2a_agent.strategies import StructuredOutputStrategy

    client = context.model_client()
    return StructuredOutputStrategy(client) if client is not None else ScriptedPolicy()


seat_plugin = SeatPlugin("calendar", policy, runtime_adapter="a2a-turns/1")
