# rebe-agent - single process, single replica (see deployment spec, section 2.2).
FROM python:3.12-slim

# tzdata so OS-level local time honours TZ. The app also depends on the Python
# tzdata package, so ZoneInfo resolves zones regardless of the base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Mexico_City

WORKDIR /app

COPY pyproject.toml ./
COPY rebe_agent ./rebe_agent
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 rebe
USER rebe

# No exposed port and no public FQDN: Evolution reaches the agent over the shared
# internal Docker network, and liveness is a push heartbeat to Uptime Kuma.
ENTRYPOINT ["python", "-m", "rebe_agent"]
