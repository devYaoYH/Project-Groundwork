import base64
import json

from starlette.testclient import TestClient

from a2a_engine.content import AssetStore
from cloud_platform.tests.test_api import platform, sign_in


def test_generated_image_endpoint_is_owner_and_episode_scoped(platform, tmp_path):
    application, control, _ = platform
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")
    image = AssetStore(tmp_path / "images").put(png, "image/png")
    trace = {"events": [{"type": "observation", "data": {"speaker": "follower", "content": [image.model_dump()]}}]}
    control.artifacts.put("launches/visual/episodes/visual.json", json.dumps(trace).encode())
    control.artifacts.put(f"launches/visual/assets/{image.sha256}", png)
    control.repo.put("episodes", "visual", {"id": "visual", "owner_id": "local:alice", "launch_id": "visual", "artifact_json_key": "launches/visual/episodes/visual.json"})
    url = f"/api/episodes/visual/assets/{image.sha256}"
    assert TestClient(application).get(url).status_code == 401
    assert sign_in(application, "Bob").get(url).status_code == 404
    client = sign_in(application, "Alice")
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == png
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/episodes/visual/assets/" + "0" * 64).status_code == 404
