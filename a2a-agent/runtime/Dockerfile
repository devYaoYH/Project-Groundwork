FROM python:3.12-slim AS requirements
WORKDIR /workspace
COPY scripts/install_workspace.py scripts/install_workspace.py
COPY a2a-engine/pyproject.toml a2a-engine/pyproject.toml
COPY a2a-agent/pyproject.toml a2a-agent/pyproject.toml
COPY a2a-judge/pyproject.toml a2a-judge/pyproject.toml
COPY expt-runner/pyproject.toml expt-runner/pyproject.toml
COPY games/ games/
RUN python scripts/install_workspace.py --requirements-output /workspace/workspace-requirements.txt

FROM python:3.12-slim AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
COPY --from=requirements /workspace/workspace-requirements.txt /tmp/workspace-requirements.txt
RUN uv pip install --system -r /tmp/workspace-requirements.txt

FROM dependencies

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/runtime
COPY a2a-engine /opt/runtime/a2a-engine
COPY a2a-agent /opt/runtime/a2a-agent
COPY a2a-judge /opt/runtime/a2a-judge
COPY expt-runner /opt/runtime/expt-runner
COPY games /opt/runtime/games
COPY scripts/install_workspace.py /opt/runtime/scripts/install_workspace.py
RUN python scripts/install_workspace.py --installer uv --no-deps
CMD ["python", "-m", "a2a_agent.service", "--origin", "http://runtime-agent:8001", "--environment-origin", "http://runtime-environment:8000"]
