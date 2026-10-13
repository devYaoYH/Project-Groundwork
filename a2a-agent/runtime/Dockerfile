FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/runtime
COPY a2a-engine /opt/runtime/a2a-engine
COPY a2a-agent /opt/runtime/a2a-agent
COPY a2a-judge /opt/runtime/a2a-judge
COPY expt-runner /opt/runtime/expt-runner
COPY games /opt/runtime/games
COPY scripts/install_workspace.py /opt/runtime/scripts/install_workspace.py
RUN python scripts/install_workspace.py
CMD ["python", "-m", "a2a_agent.service", "--origin", "http://runtime-agent:8001", "--environment-origin", "http://runtime-environment:8000"]
