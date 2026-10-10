FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/runtime
COPY a2a-engine /opt/runtime/a2a-engine
COPY a2a-agent /opt/runtime/a2a-agent
COPY games/word-guess /opt/runtime/word-guess
COPY games/buyer-seller /opt/runtime/buyer-seller
COPY expt-runner /opt/runtime/expt-runner
RUN python -m pip install --no-cache-dir /opt/runtime/a2a-engine /opt/runtime/a2a-agent /opt/runtime/expt-runner /opt/runtime/word-guess /opt/runtime/buyer-seller
CMD ["python", "-m", "a2a_agent.service", "--origin", "http://runtime-agent:8001", "--environment-origin", "http://runtime-environment:8000"]
