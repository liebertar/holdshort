# syntax=docker/dockerfile:1
# The sky-net runtime: judges every filing, writes the ledger, commands the aircraft.
# The only image that carries the aircraft adapters. Build context: the repository root.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.2 pymavlink==2.4.49
COPY shared /app/shared
COPY backend /app/backend
COPY configs /app/configs
# Recorded briefing scenes, read when TAVILY_API_KEY is absent. Hand-written and all marked
# "recorded"; without them the recorded mode reads an empty list and the briefing is empty.
COPY tests/fixtures/tavily /app/tests/fixtures/tavily
CMD ["python", "-m", "backend.api.server"]
