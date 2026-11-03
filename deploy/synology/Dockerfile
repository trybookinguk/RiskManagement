# RiskManagement image for Synology DSM (Container Manager). x86-64 models only.
# One image, two containers (see docker-compose.yml):
#   riskmanagement      worker: nightly jobs run with `docker exec`
#   riskmanagement-web  dashboard + JSON API (step 5)
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Europe/London \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# The repo is bind-mounted read-only at /app (git pull on the NAS updates the
# code without a rebuild); the warehouse and logs live on /data.
WORKDIR /app
CMD ["sleep", "infinity"]
