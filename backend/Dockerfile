FROM python:3.11-slim

WORKDIR /app

# git is needed at build time: requirements.txt installs neo_api_client straight from GitHub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# neo_api_client's own setup pins websockets==8.1 for a live-feed feature this app doesn't
# use, downgrading the websockets pin above it - reinstall the newer pin last so
# FastAPI/uvicorn's /ws/live endpoint gets the version it needs (see requirements.txt).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir websockets==12.0

COPY . .

ENV PYTHONUNBUFFERED=1

# Railway sets $PORT at runtime; default to 8000 for local `docker run`.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
