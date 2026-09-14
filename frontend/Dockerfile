# syntax=docker/dockerfile:1
# The map and the manual approval page: static files behind a small no-cache server.
# Build context: the repository root.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY frontend /app/frontend
CMD ["python", "frontend/serve.py", "3000", "frontend"]
