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

FROM dependencies AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH=/workspace
WORKDIR /workspace
COPY . /workspace
RUN python scripts/install_workspace.py --installer uv --no-deps
RUN python scripts/export_environment_ids.py --output /opt/groundwork-environment-ids.json

FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
COPY --from=runtime /opt/groundwork-environment-ids.json /web/lib/environment-ids.json
RUN npm run build

FROM runtime
COPY --from=web /web/out /opt/groundwork-web
