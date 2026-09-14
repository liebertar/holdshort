# syntax=docker/dockerfile:1
# Two images from one folder. Same agent code, same model; only the wiring differs.
# Build context: the repository root.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.2

# guarded: files proposals with the runtime and nothing else. The runtime, the adapters and the
# direct wiring are never copied in, so no code in this container can reach a motor.
FROM base AS guarded
COPY shared /app/shared
COPY drone/__init__.py /app/drone/__init__.py
COPY drone/agent /app/drone/agent
RUN test ! -e /app/backend \
 && test ! -e /app/drone/direct
CMD ["python", "-m", "drone.agent.loop"]

# direct: how most fleets are wired today. The agent holds its own actuator client.
# Being a different image from the one above is the point.
FROM base AS direct
RUN pip install --no-cache-dir pymavlink==2.4.49
COPY shared /app/shared
COPY drone/__init__.py /app/drone/__init__.py
COPY drone/agent /app/drone/agent
COPY drone/direct /app/drone/direct
CMD ["python", "-m", "drone.direct.loop"]
