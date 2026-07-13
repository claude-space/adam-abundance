# Switchboard — web + scheduler image (same image, different CMD per service).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

# Core + data/research extras. Add ads/browser here if you enable those adapters.
RUN pip install -e ".[data,research]"

EXPOSE 8080
# Overridden per service in docker-compose.prod.yml.
CMD ["switchboard", "serve"]
