FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

RUN useradd --uid 10001 --create-home --user-group viewer

# Explicit runtime allowlist only; zero pip dependencies, never COPY entire context.
COPY --chown=root:root __init__.py replay_store.py export_replay.py server.py PINS.manifest ./
COPY --chown=root:root static ./static
COPY --chown=root:root replays ./replays
RUN chmod 0444 __init__.py replay_store.py export_replay.py server.py PINS.manifest \
    && find static replays -type d -exec chmod 0555 {} + \
    && find static replays -type f -exec chmod 0444 {} +

USER viewer

EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080", "--pins-file", "/app/PINS.manifest"]
