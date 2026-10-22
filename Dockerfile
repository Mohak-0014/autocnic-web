FROM python:3.12-slim

# git is needed to clone the repos users submit.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTOCNIC_DATA_DIR=/data \
    AUTOCNIC_ALLOW_LOCAL_PATHS=0 \
    PORT=8000

WORKDIR /app
COPY autocnic_web/backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY autocnic_web/ .

# The server runs as an unprivileged user: analyzed repos execute their own
# Python code. Startup fixes ownership of /data first, because mounted disks
# usually arrive owned by root.
RUN useradd --create-home --uid 1000 app && mkdir -p /data
VOLUME ["/data"]

WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

# Single worker: live sessions are held in process memory.
CMD ["sh", "-c", "chown -R app:app /data 2>/dev/null; exec setpriv --reuid=app --regid=app --init-groups python main.py"]
