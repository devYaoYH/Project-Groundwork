"""Test-only oracle captured from game.py before the LangGraph migration."""

import re


async def original_loop(game):
    events = [("game_start", {"max_turns": game.config.max_turns, "word_pool_size": len(game.config.word_pool)})]
    history, guessed_word, won, turns_used = [], None, False, 0
    for turn in range(1, game.config.max_turns + 1):
        turns_used = turn
        action = await game.guesser.act({"history": list(history), "turns_remaining": game.config.max_turns - turn + 1}, {})
        text = action["text"]
        history.append({"speaker": "guesser", "text": text})
        events.append(("message", {"speaker": "guesser", "text": text}))
        reply = (await game.host.act({"history": list(history[:-1]), "last_guesser": text}, {}))["text"]
        history.append({"speaker": "host", "text": reply})
        events.append(("message", {"speaker": "host", "text": reply}))
        match = re.match(r"\s*i guess[:\s]+([a-zA-Z\-']+)", text.lower())
        if match:
            guessed_word = match.group(1)
            if reply.strip().lower().startswith("correct"):
                won = True
                break
    events.append(("game_end", {"won": won, "turns_used": turns_used}))
    return events, {"secret_word": game.secret_word, "guessed_word": guessed_word, "won": won, "turns_used": turns_used}, {
        "won": won, "turns_used": turns_used, "efficiency": turns_used / game.config.max_turns,
    }
