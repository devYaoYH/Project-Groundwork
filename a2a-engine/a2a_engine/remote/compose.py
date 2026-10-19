"""Explicit local Compose provisioning, shared by CLI and viewer runner jobs."""

import json
import os
import threading
from pathlib import Path

import httpx

from .dispatch import LoopbackServer
from .network import NetworkPolicy
from .seats import descriptor_path, validate_runtimes, write_descriptor
from .server import EnvironmentApp, ManagedEpisode, RuntimeManager

MODEL_FIELDS = frozenset({"type", "model", "source", "credential", "gcp_project", "gcp_location",
                          "api_base", "api_format", "temperature", "max_tokens", "capabilities"})
_managers = set()
_manager_lock = threading.RLock()


def compose_enabled():
    mode = os.environ.get("A2A_RUNTIME_MODE", "loopback")
    if mode not in {"loopback", "compose"}:
        raise ValueError("unknown deployment runtime mode")
    return mode == "compose"


def runtime_config(config):
    if not compose_enabled():
        return config
    agents = config.get("agents", [])
    if not agents:
        raise ValueError("Compose execution requires an explicit agent roster")
    if any(agent.get("api_key") or agent.get("type") == "human" for agent in agents):
        raise ValueError("Compose agents require agent-service credentials and a non-human roster")
    return {**config, "agents": [{**agent, "runtime": "external"} for agent in agents]}


def create_runtime_manager(specs, *, provisioning_dir=None):
    if compose_enabled():
        return ComposeRuntimeManager(specs)
    return RuntimeManager(specs, provisioning_dir=provisioning_dir)


def cancel_active_episodes():
    with _manager_lock:
        for manager in list(_managers):
            with manager.lock:
                manager.cancelled = True
                for episode in list(manager.episodes):
                    episode.close()


def credential_present(provider, seat, credential=None):
    if not provider or not 0 <= seat < 5:
        return False
    try:
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            response = client.get(f"http://runtime-seat-{seat}:8001/credentials",
                                  params={"provider": provider, **({"credential": credential} if credential else {})})
            response.raise_for_status()
            return response.json().get("providers", {}).get(provider) is True
    except (httpx.HTTPError, ValueError):
        return False


class ComposeRuntimeManager(RuntimeManager):
    def __init__(self, specs):
        self.specs = specs
        self.host = os.environ.get("A2A_RUNTIME_HOST", "viewer")
        self.ports = range(8100, 8132)
        self.agent_origins = tuple(f"http://runtime-seat-{seat}:8001" for seat in range(5))
        self.environment_origins = tuple(f"http://{self.host}:{port}" for port in self.ports)
        self.provisioning_dir = Path(os.environ.get("A2A_PROVISIONING_DIR", "/provision"))
        self.episodes, self.lock = set(), threading.RLock()
        self.cancelled = False

    def __enter__(self):
        for port, origin in zip(self.ports, self.environment_origins, strict=True):
            network = NetworkPolicy(advertised_origin=origin,
                allowed_origins=(*self.environment_origins, *self.agent_origins))
            environment = EnvironmentApp(self.specs, network=network)
            io = LoopbackServer(environment.app, port=port, bind_host="0.0.0.0", network=network)
            try:
                io.__enter__()
            except OSError:
                io.socket.close()
                continue
            self.environment, self.io = environment, io
            with _manager_lock:
                _managers.add(self)
            return self
        raise RuntimeError("Compose environment port capacity exhausted")

    def provision(self, config, *, scripted=False):
        import uuid
        if self.cancelled:
            raise RuntimeError("runtime launch cancelled")
        specs = validate_runtimes(config, scripted=scripted)
        remote = {seat: spec for seat, spec in enumerate(specs) if spec.get("runtime") == "external"}
        if not remote or max(remote) >= len(self.agent_origins):
            raise ValueError("Compose deployment supports one to five external seats")
        if any(spec.get("runtime") != "external" for spec in specs):
            raise ValueError("Compose deployment requires isolated external seats")
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            for seat in remote:
                response = client.get(self.agent_origins[seat] + "/health")
                response.raise_for_status()
                if not response.json().get("healthy"):
                    raise RuntimeError("agent service is not healthy")
                if not scripted and remote[seat].get("type") == "llm":
                    from a2a_engine.llm.factory import provider_for_config, credential_for_config
                    provider = provider_for_config(remote[seat])
                    if not credential_present(provider, seat, credential_for_config(remote[seat], provider)):
                        raise ValueError("agent service provider credential is unavailable")
        episode = self.environment.registry.provision(uuid.uuid4().hex, remote,
            environment_id=config["environment_id"], join_timeout_s=config.get("join_timeout_s", 10))
        for index, spec in remote.items():
            episode.seats[index].required_modalities = set(spec.get("required_modalities", config.get("required_modalities", ["text"])))
            episode.seats[index].asset_root = config.get("asset_root")
        runtime = ManagedEpisode(self, episode)
        # Cancellation closes episodes under this lock. Publish and register all
        # descriptor paths before allowing cleanup to snapshot their ownership.
        with self.lock:
            if self.cancelled:
                self.environment.registry.unregister(episode)
                raise RuntimeError("runtime launch cancelled")
            self.episodes.add(runtime)
            try:
                for seat, spec in remote.items():
                    unsupported = {key for key, value in spec.items() if value not in (None, {}, [], "")}
                    unsupported -= MODEL_FIELDS | {"name", "id", "role", "runtime", "harness", "description", "required_modalities", "asset_root"}
                    if unsupported:
                        raise ValueError("unsupported Compose agent policy fields")
                    policy = {"type": "scripted"} if scripted else {key: value for key, value in spec.items() if key in MODEL_FIELDS}
                    if config["environment_id"] == "negotiation" and scripted:
                        policy = {"type": "heuristic"}
                    directory = self.provisioning_dir / f"seat-{seat}"
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(directory, 0o700)
                    path = descriptor_path(directory, episode, episode.seats[seat])
                    if os.path.lexists(path):
                        raise FileExistsError(path)
                    # A same-thread signal can close this reentrant lock and
                    # raise SystemExit before write_descriptor returns.
                    runtime.descriptors.append(path)
                    write_descriptor(directory, episode, episode.seats[seat],
                        f"{self.io.base_url}/episodes/{episode.episode_id}/join", policy=policy)
                    if runtime.closed or self.cancelled:
                        path.unlink(missing_ok=True)
                        raise RuntimeError("runtime launch cancelled")
                return runtime
            except BaseException:
                runtime.close()
                raise

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            with _manager_lock:
                _managers.discard(self)
