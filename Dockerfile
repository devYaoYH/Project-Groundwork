FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/workspace

WORKDIR /workspace

COPY . /workspace

# Install every shipped environment so the runner's entry-point discovery works in the
# same way it does for a collaborator's editable local checkout.  This image
# deliberately contains no cloud credentials or provider configuration.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir \
       -e a2a-engine \
       -e a2a-judge \
       -e expt-runner \
       -e games/buyer-seller \
       -e games/calendar \
       -e games/negotiation \
       -e games/word-guess
