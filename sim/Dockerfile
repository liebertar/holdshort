# syntax=docker/dockerfile:1
# The world: a seeded simulator standing in for the aircraft. It does what it is told.
# Build context: the repository root.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.2
COPY shared /app/shared
COPY sim /app/sim
# The world reads real airspace, buildings and addresses; it exits at start without them.
COPY configs/airspace /app/configs/airspace
CMD ["python", "-m", "sim.service"]
