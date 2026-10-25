FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as non-root; /data holds the dedup ledger + ticket routing table
RUN useradd -m bot && mkdir -p /data && chown -R bot:bot /app /data
USER bot
VOLUME ["/data"]

# Long-polling worker: no port to expose, no health endpoint needed.
CMD ["python", "main.py"]
