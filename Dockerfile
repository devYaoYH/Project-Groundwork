FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH=/workspace
WORKDIR /workspace
COPY . /workspace
RUN python -m pip install --no-cache-dir --upgrade pip && python scripts/install_workspace.py
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
