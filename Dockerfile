FROM python:3.13-alpine
LABEL org.opencontainers.image.title="nc-link-watcher" \
      org.opencontainers.image.description="Keeps Nextcloud External Sites links in step with Docker container labels" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY watcher.py .
VOLUME /data
ARG VERSION=dev
ENV VERSION=$VERSION
ENV PYTHONUNBUFFERED=1
# Unhealthy means no sync has completed for three SYNC_INTERVALs.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=2 \
    CMD ["python", "watcher.py", "--healthcheck"]
CMD ["python", "watcher.py"]
