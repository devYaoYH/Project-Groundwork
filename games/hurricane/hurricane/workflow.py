"""Executable story: a season of one or more storms; each round of a storm runs a main pass
of bounded action turns, convened meetings as live round-robin dialogues and a reply pass for
direct messages; landfall scores the storm and, before another storm, seats may write notes."""

from dataclasses import dataclass
import json
from typing import TypedDict
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from a2a_engine.workflow_runtime import (
    AgentStep,
    GraphProgram,
    GuardedAgent,
    require_agent_step,
)

TURN_KINDS = ("main", "meeting", "reply", "debrief")


class State(TypedDict, total=False):
    storm_index: int
    storms: list
    notes: dict
    observation_chars: list
    round_index: int
    turn_kind: str
    seat: int
    phase_turn: int
    phase_limit: int
    action: dict
    readings: dict
    probe_log: list
    probed_this_round: list
    bulletin: list
    dms: list
    board: list
    meetings: list
    meeting_index: int
    meeting_seat: int
    estimates: list
    recommendations: list
    orders: list
    evacuated: dict
    recon_remaining: int
    attention_used: dict
    attention_debt: dict
    attention_debt_next: dict
    reply_seats: list
    invalid_actions: int
    result: dict


@dataclass
class GameContext:
    game: object
    roster: object = None

    def __post_init__(self):
        self.game.agents = [
            a if isinstance(a, GuardedAgent) else GuardedAgent(a) for a in self.game.agents
        ]


def recursion_limit(config):
    n = config.num_agents
    main = 2 * n + 3
    meetings = config.max_meetings_per_round * (2 * n * config.meeting_turns + 4) + 3
    reply = 2 * n + 3
    debrief = 2 * n + 3
    return 20 + config.num_storms * (config.num_rounds * (main + meetings + reply + 6) + debrief + 8)


def all_seats(cfg):
    return list(range(cfg.num_agents))


# -- round bookkeeping ------------------------------------------------------


def next_round(state):
    return {}


def round_route(state, runtime: Runtime[GameContext]):
    return "next" if state["round_index"] < runtime.context.game.config.num_rounds else "done"


def begin_round(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    cfg = game.config
    round_number = state["round_index"] + 1
    debt = state["attention_debt"]
    game.events.append(
        "round_start",
        data={
            "round": round_number,
            "rounds_after_this": cfg.num_rounds - round_number,
            "order_effective": cfg.order_effective(round_number),
            "recon_remaining": state["recon_remaining"],
            "attention_debt": {str(s + 1): debt.get(s, 0) for s in all_seats(cfg) if debt.get(s)},
        },
    )
    return {"probed_this_round": [], "attention_used": {}, "reply_seats": []}


def close_round(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    game.events.append(
        "round_end",
        data={
            "round": state["round_index"] + 1,
            "recon_remaining": state["recon_remaining"],
            "orders": len(state["orders"]),
            "meetings": sum(m["round"] == state["round_index"] + 1 for m in state["meetings"]),
        },
    )
    # Meeting attention costs accrued this round apply to the next round's budgets.
    return {"round_index": state["round_index"] + 1, "attention_debt": dict(state["attention_debt_next"]),
            "attention_debt_next": {}}


# -- shared turn plumbing -----------------------------------------------------


def manager(state, context):
    return context.game.agents[state["seat"]]


def observation(state, context):
    game = context.game
    obs = game.observation(state)
    content = json.dumps(obs, ensure_ascii=False)
    game.events.append(
        "observation",
        data={
            "speaker": manager(state, context).name,
            "round": obs["round"],
            "turn_kind": obs["turn_kind"],
            **({"storm": obs["season"]["storm"]} if "season" in obs else {}),
            "content": content,
            "visibility": "private",
        },
    )
    return obs


def observation_chars(state, game):
    """Context-length proxy per storm (the environment cannot see provider token counts);
    the observation is a pure function of the state the seat was handed."""
    chars = list(state["observation_chars"])
    chars[state["storm_index"]] += len(json.dumps(game.observation(state), ensure_ascii=False))
    return chars


async def act(actor, operation, obs, state, context):
    require_agent_step()
    return {"action": await actor.act(obs, {})}


def zone_list(value, num_zones):
    if not isinstance(value, list) or not value:
        return None
    zones = []
    for zone in value:
        if isinstance(zone, bool) or not isinstance(zone, int) or not 0 <= zone < num_zones:
            return None
        if zone not in zones:
            zones.append(zone)
    return zones


def resolve_seat(value, game):
    """A colleague named the way the observation names them: the 1-based participant number
    (as an int or a numeric string) or the exact seat name from `participants`. Live model
    seats addressed direct messages by name and lost them; nothing else is guessed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        names = [a.name for a in game.agents]
        if value in names:
            return names.index(value)
        if not value.strip().isdigit():
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    seat = value - 1
    return seat if seat in all_seats(game.config) else None


def seat_list(value, game, me):
    if not isinstance(value, list) or not value:
        return None
    seats = []
    for participant in value:
        seat = resolve_seat(participant, game)
        if seat is None or seat == me:
            return None
        if seat not in seats:
            seats.append(seat)
    return seats


def actions_of(action):
    """A turn is one response holding an ordered action list; a bare action counts as one."""
    if isinstance(action, list):
        return action
    if isinstance(action, dict) and not action.get("parse_failed"):
        if "actions" in action:
            return action["actions"] if isinstance(action["actions"], list) else None
        return [action]
    return None


def valid_text(cfg, text):
    return isinstance(text, str) and len(text) <= cfg.max_message_chars


def valid_estimate(cfg, estimate):
    return estimate is None or (
        isinstance(estimate, int) and not isinstance(estimate, bool) and 0 <= estimate < cfg.num_zones
    )


class Turn:
    """Mutable working copy of the round state for one seat's turn; commits as updates."""

    def __init__(self, state, game, seat):
        self.state, self.game, self.cfg, self.seat = state, game, game.storm_config, seat
        self.name = game.agents[seat].name
        self.round = state["round_index"] + 1
        self.notes = {k: list(v) for k, v in state["notes"].items()}
        self.readings = {k: [dict(r) for r in v] for k, v in state["readings"].items()}
        self.probe_log = list(state["probe_log"])
        self.probed = list(state["probed_this_round"])
        self.bulletin = list(state["bulletin"])
        self.dms = list(state["dms"])
        self.board = list(state["board"])
        self.meetings = [dict(m, attendees=list(m["attendees"]), transcript=list(m["transcript"]), left=list(m["left"])) for m in state["meetings"]]
        self.estimates = list(state["estimates"])
        self.recommendations = list(state["recommendations"])
        self.orders = list(state["orders"])
        self.evacuated = dict(state["evacuated"])
        self.recon_remaining = state["recon_remaining"]
        self.attention_used = dict(state["attention_used"])
        self.reply_seats = list(state["reply_seats"])
        self.invalid = 0

    # -- helpers --

    def everyone(self):
        return all_seats(self.cfg)

    def disclose(self, audience, channel, to_agents, meeting_id=None):
        """Add `audience` to every own reading they cannot yet see; record one disclosure event."""
        mine = self.readings.get(self.seat, [])
        disclosed = [r for r in mine if any(s not in r["audience"] for s in audience)]
        for r in disclosed:
            r["audience"] = sorted(set(r["audience"]) | set(audience))
        if disclosed:
            self.game.events.append(
                "disclosure",
                data={
                    "round": self.round,
                    "participant": self.seat + 1,
                    "speaker": self.name,
                    "channel": channel,
                    "meeting_id": meeting_id,
                    "to_agents": to_agents,
                    "visibility": "public" if channel == "bulletin" else "private",
                    "readings": [
                        {k: r[k] for k in ("round", "instrument", "ensemble")} for r in disclosed
                    ],
                    "text": "Shared data: "
                    + "; ".join(f"round {r['round']} {r['instrument']} {r['ensemble']}" for r in disclosed),
                },
            )
        return len(disclosed)

    def invalid_action(self, kind, reason):
        self.invalid += 1
        self.game.events.append(
            "invalid_action",
            data={
                "round": self.round,
                "turn_kind": self.state["turn_kind"],
                "participant": self.seat + 1,
                "kind": kind if isinstance(kind, str) else None,
                "reason": reason,
            },
        )

    def claims(self, action, audience, channel, to_agents, meeting_id=None):
        estimate, recommend = action.get("estimate"), action.get("recommend")
        if estimate is not None:
            self.estimates.append(
                {"round": self.round, "participant": self.seat + 1, "value": estimate,
                 "audience": audience, "channel": channel}
            )
        if recommend:
            self.recommendations.append(
                {"round": self.round, "participant": self.seat + 1, "zones": recommend,
                 "audience": audience, "channel": channel}
            )

    def say(self, action, channel, audience, to_agents, meeting_id=None):
        """Validate and commit a text message with optional data sharing and public claims."""
        text, share = action.get("text", ""), action.get("share", False)
        estimate, recommend = action.get("estimate"), action.get("recommend")
        if not valid_text(self.cfg, text) or not isinstance(share, bool):
            return False
        if not valid_estimate(self.cfg, estimate):
            return False
        if recommend == []:
            # "I recommend nothing" is a statement, not a malformed one: the message stands.
            recommend = None
        if recommend is not None:
            recommend = zone_list(recommend, self.cfg.num_zones)
            if recommend is None:
                return False
        shared = self.disclose(audience, channel, to_agents, meeting_id) if share else 0
        message = {
            "speaker": self.name,
            "text": text,
            "round": self.round,
            "turn_kind": self.state["turn_kind"],
            "channel": channel,
            "meeting_id": meeting_id,
            "participant": self.seat + 1,
            "to_agents": to_agents,
            "visibility": "public" if channel == "bulletin" else "private",
            "shared_readings": shared,
            "estimate": estimate,
            "recommend": recommend,
        }
        self.game.events.append("message", data=message)
        self.claims({**action, "recommend": recommend}, audience, channel, to_agents, meeting_id)
        return message

    # -- action kinds --

    def do_probe(self, action):
        from .environment import reading

        instrument = action.get("instrument")
        if instrument not in ("recon", "model") or self.seat in self.probed:
            return False
        if instrument == "recon":
            if self.recon_remaining < 1:
                return False
            self.recon_remaining -= 1
        ensemble = reading(self.cfg, self.round, self.seat, instrument, self.game.reading_storm(self.state))
        row = {"round": self.round, "instrument": instrument, "ensemble": ensemble, "audience": [self.seat]}
        self.readings[self.seat] = [*self.readings.get(self.seat, []), row]
        self.probed.append(self.seat)
        self.probe_log.append({"round": self.round, "participant": self.seat + 1, "instrument": instrument})
        self.game.events.append(
            "probe",
            data={
                "round": self.round, "participant": self.seat + 1, "instrument": instrument,
                "ensemble": ensemble, "speaker": self.name, "visibility": "private",
                "text": f"{instrument} ensemble of landfall zones: {ensemble}",
            },
        )
        return True

    def do_post(self, action):
        others = [s + 1 for s in self.everyone() if s != self.seat]
        message = self.say(action, "bulletin", self.everyone(), others)
        if message:
            self.bulletin.append(message)
        return bool(message)

    def do_dm(self, action):
        seat = resolve_seat(action.get("to"), self.game)
        if seat is None or seat == self.seat:
            return False
        to = seat + 1
        message = self.say(action, "dm", [self.seat, seat], [to])
        if message:
            self.dms.append({**message, "to": to})
            if to - 1 not in self.reply_seats:
                self.reply_seats.append(to - 1)
        return bool(message)

    def do_write_board(self, action):
        text = action.get("text", "")
        if not valid_text(self.cfg, text) or not text.strip():
            return False
        if len(self.board) >= self.cfg.board_capacity:
            return False
        entry = {"round": self.round, "participant": self.seat + 1, "speaker": self.name, "text": text}
        self.board.append(entry)
        self.game.events.append("board", data={**entry, "channel": "board", "entry": len(self.board)})
        return True

    def do_convene(self, action):
        invited = seat_list(action.get("participants"), self.game, self.seat)
        agenda = action.get("agenda", "")
        if invited is None or not valid_text(self.cfg, agenda):
            return False
        this_round = [m for m in self.meetings if m["round"] == self.round]
        if len(this_round) >= self.cfg.max_meetings_per_round or any(m["convener"] == self.seat + 1 for m in this_round):
            return False
        meeting = {
            "id": len(self.meetings) + 1,
            "round": self.round,
            "convener": self.seat + 1,
            "attendees": [self.seat, *invited],
            "agenda": agenda,
            "transcript": [],
            "left": [],
        }
        self.meetings.append(meeting)
        self.game.events.append(
            "convene",
            data={
                "round": self.round, "participant": self.seat + 1, "speaker": self.name,
                "meeting_id": meeting["id"], "to_agents": [s + 1 for s in invited],
                "attendees": [s + 1 for s in meeting["attendees"]], "agenda": agenda,
                "text": f"Convened meeting {meeting['id']} with participants {[s + 1 for s in invited]}: {agenda}".strip(),
            },
        )
        return True

    def do_evacuate(self, action):
        zones = zone_list(action.get("zones"), self.cfg.num_zones)
        if zones is None or any(self.cfg.sector_of(z) != self.seat or z in self.evacuated for z in zones):
            return False
        effective = self.cfg.order_effective(self.round)
        orders = [{"round": self.round, "participant": self.seat + 1, "zone": z, "effective": effective} for z in zones]
        self.orders.extend(orders)
        self.evacuated.update({o["zone"]: o for o in orders})
        self.game.events.append(
            "evacuation_order",
            data={
                "round": self.round, "participant": self.seat + 1, "zones": zones, "effective": effective,
                "population": sum(self.cfg.populations[z] for z in zones), "speaker": self.name,
                "text": f"Ordered evacuation of zones {zones}" + ("." if effective else " (too late to protect anyone)."),
            },
        )
        return True

    MAIN = {"probe": "do_probe", "post": "do_post", "dm": "do_dm", "write_board": "do_write_board",
            "convene": "do_convene", "evacuate": "do_evacuate"}
    REPLY = {"dm": "do_dm", "post": "do_post"}

    def run_main(self, actions):
        budget = max(0, self.cfg.attention_budget - self.state["attention_debt"].get(self.seat, 0))
        used = 0
        for action in actions:
            if not isinstance(action, dict):
                self.invalid_action(None, "not_an_object")
                continue
            kind = action.get("kind")
            if kind == "pass":
                break
            if kind not in self.MAIN:
                self.invalid_action(kind, "unknown_kind")
                continue
            if used >= budget:
                self.invalid_action(kind, "attention_exhausted")
                continue
            used += 1
            if not getattr(self, self.MAIN[kind])(action):
                self.invalid_action(kind, "rejected")
        self.attention_used[self.seat] = used
        self.game.events.append(
            "turn_end",
            data={"round": self.round, "turn_kind": "main", "participant": self.seat + 1,
                  "speaker": self.name, "attention_used": used, "attention_budget": budget},
        )

    def run_reply(self, actions):
        for action in actions[:1]:
            if not isinstance(action, dict):
                self.invalid_action(None, "not_an_object")
                continue
            kind = action.get("kind")
            if kind == "pass":
                continue
            if kind not in self.REPLY:
                self.invalid_action(kind, "unknown_kind")
            elif not getattr(self, self.REPLY[kind])(action):
                self.invalid_action(kind, "rejected")

    def run_debrief(self, actions):
        """One private note, carried into every later storm; nothing else survives the boundary."""
        action = actions[0] if actions else {}
        if not isinstance(action, dict):
            self.invalid_action(None, "not_an_object")
            return
        kind = action.get("kind")
        if kind == "pass":
            return
        if kind != "note":
            self.invalid_action(kind, "unknown_kind")
            return
        text = action.get("text", "")
        if not isinstance(text, str) or not text.strip() or len(text) > self.cfg.max_note_chars:
            self.invalid_action(kind, "rejected")
            return
        storm = self.game.storm_number(self.state)
        self.notes[self.seat] = [*self.notes.get(self.seat, []), {"storm": storm, "text": text}]
        self.game.events.append(
            "note",
            data={"storm": storm, "round": self.round, "participant": self.seat + 1, "speaker": self.name,
                  "text": text, "visibility": "private", "to_agents": []},
        )

    def run_meeting(self, actions):
        meeting = self.meetings[self.state["meeting_index"]]
        attendees = meeting["attendees"]
        action = actions[0] if actions else {}
        if not isinstance(action, dict):
            self.invalid_action(None, "not_an_object")
            return
        kind = action.get("kind")
        if kind == "leave":
            if self.seat not in meeting["left"]:
                meeting["left"].append(self.seat)
            self.game.events.append(
                "leave",
                data={"round": self.round, "participant": self.seat + 1, "speaker": self.name,
                      "meeting_id": meeting["id"], "visibility": "private",
                      "to_agents": [s + 1 for s in attendees if s != self.seat],
                      "text": f"Left meeting {meeting['id']}."},
            )
            return
        if kind != "say":
            self.invalid_action(kind, "unknown_kind")
            return
        message = self.say(
            action, "meeting", attendees, [s + 1 for s in attendees if s != self.seat], meeting["id"]
        )
        if message:
            meeting["transcript"].append(message)
        else:
            self.invalid_action(kind, "rejected")

    def updates(self):
        return {
            "readings": self.readings, "probe_log": self.probe_log, "probed_this_round": self.probed,
            "bulletin": self.bulletin, "dms": self.dms, "board": self.board, "meetings": self.meetings,
            "estimates": self.estimates, "recommendations": self.recommendations,
            "orders": self.orders, "evacuated": self.evacuated, "recon_remaining": self.recon_remaining,
            "attention_used": self.attention_used, "reply_seats": self.reply_seats,
            "notes": self.notes,
            "invalid_actions": self.state["invalid_actions"] + self.invalid,
        }


def record_turn(state, runtime: Runtime[GameContext]):
    game, kind, seat = runtime.context.game, state["turn_kind"], state["seat"]
    turn = Turn(state, game, seat)
    actions = actions_of(state["action"])
    if actions is None:
        # No default replacement; a malformed response consumes the turn.
        turn.invalid_action(None, "malformed_response")
        actions = []
    if kind == "main":
        turn.run_main(actions)
    elif kind == "reply":
        turn.run_reply(actions)
    elif kind == "debrief":
        turn.run_debrief(actions)
    else:
        turn.run_meeting(actions)
    return {**turn.updates(), "phase_turn": state["phase_turn"] + 1,
            "observation_chars": observation_chars(state, game)}


# -- main pass ------------------------------------------------------------------


def begin_main(state, runtime: Runtime[GameContext]):
    return {"turn_kind": "main", "seat": 0, "phase_turn": 0, "phase_limit": runtime.context.game.config.num_agents}


def advance_seat(state, runtime: Runtime[GameContext]):
    return {"seat": (state["seat"] + 1) % runtime.context.game.config.num_agents}


def main_route(state):
    return "done" if state["phase_turn"] >= state["phase_limit"] else "next"


def main_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("begin", begin_main)
    builder.add_node("manager", AgentStep(manager, "act", observation, act))
    builder.add_node("record_turn", record_turn)
    builder.add_node("advance_seat", advance_seat)
    builder.add_edge(START, "begin")
    builder.add_edge("begin", "manager")
    builder.add_edge("manager", "record_turn")
    builder.add_edge("record_turn", "advance_seat")
    builder.add_conditional_edges("advance_seat", main_route, {"next": "manager", "done": END})
    return GraphProgram(builder)


# -- meetings: live round-robin dialogue among attendees --------------------------


def meeting_route(state, runtime: Runtime[GameContext]):
    this_round = state["round_index"] + 1
    for index, meeting in enumerate(state["meetings"]):
        if meeting["round"] == this_round and not meeting.get("closed"):
            return "open"
    return "none"


def open_meeting(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    this_round = state["round_index"] + 1
    index = next(i for i, m in enumerate(state["meetings"]) if m["round"] == this_round and not m.get("closed"))
    meeting = state["meetings"][index]
    game.events.append(
        "meeting_start",
        data={"round": this_round, "meeting_id": meeting["id"], "convener": meeting["convener"],
              "attendees": [s + 1 for s in meeting["attendees"]], "agenda": meeting["agenda"]},
    )
    return {
        "turn_kind": "meeting", "meeting_index": index, "meeting_seat": 0,
        "seat": meeting["attendees"][0], "phase_turn": 0,
        "phase_limit": len(meeting["attendees"]) * game.config.meeting_turns,
    }


def next_attendee(state, runtime: Runtime[GameContext]):
    meeting = state["meetings"][state["meeting_index"]]
    attendees = meeting["attendees"]
    position = state["meeting_seat"]
    for step in range(1, len(attendees) + 1):
        candidate = (position + step) % len(attendees)
        if attendees[candidate] not in meeting["left"]:
            return {"meeting_seat": candidate, "seat": attendees[candidate]}
    return {}


def attendee_route(state, runtime: Runtime[GameContext]):
    meeting = state["meetings"][state["meeting_index"]]
    present = [s for s in meeting["attendees"] if s not in meeting["left"]]
    if state["phase_turn"] >= state["phase_limit"] or len(present) < 2:
        return "done"
    return "next"


def close_meeting(state, runtime: Runtime[GameContext]):
    game, cfg = runtime.context.game, runtime.context.game.config
    meetings = [dict(m) for m in state["meetings"]]
    meeting = meetings[state["meeting_index"]]
    meeting["closed"] = True
    stayed = [s for s in meeting["attendees"] if s not in meeting["left"] or any(
        m["participant"] == s + 1 for m in meeting["transcript"])]
    debt = dict(state["attention_debt_next"])
    for seat in stayed:
        debt[seat] = debt.get(seat, 0) + cfg.meeting_attention_cost
    meeting["attended"] = [s + 1 for s in stayed]
    game.events.append(
        "meeting_end",
        data={"round": state["round_index"] + 1, "meeting_id": meeting["id"],
              "messages": len(meeting["transcript"]), "attended": meeting["attended"],
              "attention_cost_each": cfg.meeting_attention_cost, "money_cost": cfg.meeting_cost * len(stayed)},
    )
    return {"meetings": meetings, "attention_debt_next": debt}


def meeting_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("open_meeting", open_meeting)
    builder.add_node("attendee", AgentStep(manager, "act", observation, act))
    builder.add_node("record_turn", record_turn)
    builder.add_node("next_attendee", next_attendee)
    builder.add_node("close_meeting", close_meeting)
    builder.add_edge(START, "open_meeting")
    builder.add_edge("open_meeting", "attendee")
    builder.add_edge("attendee", "record_turn")
    builder.add_conditional_edges("record_turn", attendee_route, {"next": "next_attendee", "done": "close_meeting"})
    builder.add_edge("next_attendee", "attendee")
    builder.add_edge("close_meeting", END)
    return GraphProgram(builder)


# -- reply pass: seats that received direct messages this round answer once ---------


def begin_reply(state, runtime: Runtime[GameContext]):
    pending = sorted(state["reply_seats"]) if runtime.context.game.config.reply_turns else []
    return {"turn_kind": "reply", "phase_turn": 0, "phase_limit": len(pending), "reply_seats": pending,
            "seat": pending[0] if pending else 0}


def reply_route(state):
    return "done" if state["phase_turn"] >= state["phase_limit"] else "next"


def next_replier(state, runtime: Runtime[GameContext]):
    pending = state["reply_seats"]
    position = state["phase_turn"]
    return {"seat": pending[position]} if position < len(pending) else {}


def reply_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("begin", begin_reply)
    builder.add_node("replier", AgentStep(manager, "act", observation, act))
    builder.add_node("record_turn", record_turn)
    builder.add_node("next_replier", next_replier)
    builder.add_edge(START, "begin")
    builder.add_conditional_edges("begin", reply_route, {"next": "replier", "done": END})
    builder.add_edge("replier", "record_turn")
    builder.add_edge("record_turn", "next_replier")
    builder.add_conditional_edges("next_replier", reply_route, {"next": "replier", "done": END})
    return GraphProgram(builder)


# -- round, landfall, program -------------------------------------------------------


def meetings_done(state):
    return {}


def round_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("begin_round", begin_round)
    builder.add_node("main_pass", main_program().compiled)
    builder.add_node("meeting", meeting_program().compiled)
    builder.add_node("meetings_done", meetings_done)
    builder.add_node("reply_pass", reply_program().compiled)
    builder.add_node("close_round", close_round)
    builder.add_edge(START, "begin_round")
    builder.add_edge("begin_round", "main_pass")
    builder.add_conditional_edges("main_pass", meeting_route, {"open": "meeting", "none": "meetings_done"})
    builder.add_conditional_edges("meeting", meeting_route, {"open": "meeting", "none": "meetings_done"})
    builder.add_edge("meetings_done", "reply_pass")
    builder.add_edge("reply_pass", "close_round")
    builder.add_edge("close_round", END)
    return GraphProgram(builder)


def landfall(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    result = game.landfall(state)
    game.events.append(
        "landfall",
        data={
            **{k: v for k, v in result.items() if k not in ("orders", "rounds_completed")},
            "text": (
                f"Landfall at zone {result['landfall_zone']}; zones {result['hit_zones']} hit. "
                f"Protected {result['population_protected']}, exposed {result['population_exposed']}, "
                f"needlessly evacuated {result['population_needlessly_evacuated']}. "
                f"Total cost {result['total_cost']:g} (oracle {result['oracle_cost']:g})."
            ),
        },
    )
    return {"storms": [*state["storms"], game.storm_summary(state, result)]}


# -- debrief pass: before another storm, each seat may write one private note ---------------


def debrief_route(state, runtime: Runtime[GameContext]):
    cfg = runtime.context.game.config
    more_storms = state["storm_index"] + 1 < cfg.num_storms
    return "debrief" if more_storms and cfg.season_memory == "notes" else "skip"


def begin_debrief(state, runtime: Runtime[GameContext]):
    return {"turn_kind": "debrief", "seat": 0, "phase_turn": 0, "phase_limit": runtime.context.game.config.num_agents}


def debrief_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("begin", begin_debrief)
    builder.add_node("manager", AgentStep(manager, "act", observation, act))
    builder.add_node("record_turn", record_turn)
    builder.add_node("advance_seat", advance_seat)
    builder.add_edge(START, "begin")
    builder.add_edge("begin", "manager")
    builder.add_edge("manager", "record_turn")
    builder.add_edge("record_turn", "advance_seat")
    builder.add_conditional_edges("advance_seat", main_route, {"next": "manager", "done": END})
    return GraphProgram(builder)


# -- season: storms in order, same seats; per-storm state resets, the board may persist -------


def next_storm(state):
    return {}


def storm_route(state, runtime: Runtime[GameContext]):
    return "next" if state["storm_index"] < runtime.context.game.config.num_storms else "done"


def begin_storm(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    index = state["storm_index"]
    game.storm_config = game.storm_configs[index]
    fresh = game.storm_state()
    if game.in_season:
        game.events.append(
            "storm_start",
            data={"storm": index + 1, "storms_total": game.config.num_storms,
                  "scenario_id": game.storm_config.scenario_id,
                  "board_entries": len(state["board"]) if game.config.board_persists else 0},
        )
    if not game.config.board_persists:
        fresh["board"] = []
    return {**fresh, "observation_chars": [*state["observation_chars"], 0]}


def close_storm(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    if game.in_season:
        last = state["storms"][-1]
        game.events.append(
            "storm_end",
            data={"storm": last["storm"], "total_cost": last["total_cost"], "regret": last["regret"],
                  "notes": sum(n["storm"] == last["storm"] for rows in state["notes"].values() for n in rows)},
        )
    return {"storm_index": state["storm_index"] + 1}


def storm_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("begin_storm", begin_storm)
    builder.add_node("next_round", next_round)
    builder.add_node("round", round_program().compiled)
    builder.add_node("landfall", landfall)
    builder.add_node("debrief", debrief_program().compiled)
    builder.add_node("close_storm", close_storm)
    builder.add_edge(START, "begin_storm")
    builder.add_edge("begin_storm", "next_round")
    builder.add_conditional_edges("next_round", round_route, {"next": "round", "done": "landfall"})
    builder.add_edge("round", "next_round")
    builder.add_conditional_edges("landfall", debrief_route, {"debrief": "debrief", "skip": "close_storm"})
    builder.add_edge("debrief", "close_storm")
    builder.add_edge("close_storm", END)
    return GraphProgram(builder)


def finalize(state, runtime: Runtime[GameContext]):
    storms = state["storms"]
    runtime.context.game.events.append(
        "game_end",
        data={"rounds_completed": sum(s["rounds_completed"] for s in storms),
              "storms_completed": len(storms),
              "total_cost": sum(s["total_cost"] for s in storms)},
    )
    return {}


def build_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("next_storm", next_storm)
    builder.add_node("storm", storm_program().compiled)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "next_storm")
    builder.add_conditional_edges("next_storm", storm_route, {"next": "storm", "done": "finalize"})
    builder.add_edge("storm", "next_storm")
    builder.add_edge("finalize", END)
    return GraphProgram(builder)
