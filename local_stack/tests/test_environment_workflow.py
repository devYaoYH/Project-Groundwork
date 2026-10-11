import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from local_stack.control_plane import ControlPlane
from local_stack.server import LocalStackHandler
from word_guess.game import WordGuessGame
from buyer_seller.game import BuyerSellerGame
from negotiation_game.game import NegotiationGame
from negotiation_game.backend.engine import GameEngine


def test_workflow_projection_preserves_declaration_and_never_constructs_game(tmp_path, monkeypatch):
    control = ControlPlane(tmp_path / "db", workspace=Path(__file__).resolve().parents[2])
    before = control.environment_detail("word_guess")
    bargaining_before = control.environment_detail("buyer_seller")
    negotiation_before = control.environment_detail('negotiation')
    def fail(*args, **kwargs):
        raise AssertionError("constructed an episode")
    monkeypatch.setattr(WordGuessGame, "__init__", fail)
    monkeypatch.setattr(BuyerSellerGame, "__init__", fail)
    monkeypatch.setattr(BuyerSellerGame, "runtime_capability", fail)
    monkeypatch.setattr(NegotiationGame, '__init__', fail)
    monkeypatch.setattr(GameEngine, '__init__', fail)
    monkeypatch.setattr(NegotiationGame, 'runtime_capability', fail)
    assert control.environment_workflow("word_guess")["status"] == "available"
    assert control.environment_detail("word_guess") == before
    assert control.environment_workflow("calendar")["status"] == "available"
    assert control.environment_workflow("buyer_seller")["status"] == "available"
    assert control.environment_detail("buyer_seller") == bargaining_before
    assert control.environment_workflow("negotiation")["status"] == "available"
    assert control.environment_detail('negotiation') == negotiation_before
    monkeypatch.setattr(WordGuessGame, "build_program", fail)
    assert control.environment_workflow("word_guess") == {"status": "invalid", "reason": "invalid_executable_program"}


def test_real_workflow_http_status_and_no_source_reader(tmp_path):
    class Handler(LocalStackHandler):
        database = tmp_path / "db"
        workspace = Path(__file__).resolve().parents[2]
        _control_plane = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        def get(path):
            client = HTTPConnection(*server.server_address)
            try:
                client.request("GET", path)
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        status, body = get("/api/environments/word_guess/workflow")
        assert status == 200 and body["status"] == "available"
        assert get("/api/environments/calendar/workflow")[1]["status"] == "available"
        status, bargaining = get("/api/environments/buyer_seller/workflow")
        assert status == 200 and bargaining["status"] == "available"
        assert {e.get("route_key") for e in bargaining["workflow"]["graphs"][0]["edges"] if e["conditional"]} == {"continue", "finished", "accepted", "rejected"}
        assert not any(key in json.dumps(bargaining) for key in ("seller_cost", "buyer_value", "capability", "positions"))
        status, negotiation = get('/api/environments/negotiation/workflow')
        assert status == 200 and negotiation['status'] == 'available'
        graphs = negotiation['workflow']['graphs']
        assert len(graphs) == 5
        assert {'bypass_decided', 'retry', 'both_decided', 'cap', 'stopped'} <= {edge.get('route_key') for graph in graphs for edge in graph['edges']}
        assert not any(key in json.dumps(negotiation) for key in ('/Users/', 'api_key', 'resource_supply', 'positions'))
        assert get("/api/environments/missing/workflow")[0] == 404
        assert get("/api/environments/word_guess/source/workflow")[0] == 404
        assert not any(key in json.dumps(body) for key in ("/Users/", "secret_word", "capability", "positions"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
