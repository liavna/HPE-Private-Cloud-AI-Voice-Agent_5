# WebSocket Server - Natural Conversation Mode (v4.1.0)
# Improved LLM prompts, connection pooling, retry logic, async I/O
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    websockets \
    "httpx[http2]" \
    psycopg2-binary \
    asyncpg

COPY app.py /app/

ENV PYTHONUNBUFFERED=1

EXPOSE 8765 8766

CMD ["python", "app.py"]