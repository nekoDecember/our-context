FROM node:24.22.1-alpine AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12.15-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY our_context/ ./our_context/
COPY wiki-policy.md ./wiki-policy.md
COPY docs/ ./docs/
COPY --from=frontend /ui/dist ./static
EXPOSE 8096
CMD ["uvicorn", "our_context.api:app", "--host", "0.0.0.0", "--port", "8096"]

FROM runtime AS test
RUN pip install --no-cache-dir 'pytest>=8,<10' 'ruff>=0.9,<1'
COPY tests/ ./tests/
COPY pyproject.toml ./
CMD ["pytest", "-q"]

FROM runtime AS app
