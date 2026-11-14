FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    --root-user-action=ignore -r requirements.txt

RUN useradd --uid 10001 --create-home --user-group gateway

# Explicit runtime allowlist only; never COPY the whole build context.
COPY --chown=root:root app.py gemini_client.py plan_schema.py replay_store.py ./
COPY --chown=root:root static ./static
COPY --chown=root:root templates ./templates
COPY --chown=root:root replay ./replay
RUN chmod 0444 app.py gemini_client.py plan_schema.py replay_store.py \
    && find static templates replay -type d -exec chmod 0555 {} + \
    && find static templates replay -type f -exec chmod 0444 {} +

USER gateway

EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["python", "app.py"]
