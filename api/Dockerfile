# Build from the REPO ROOT, not from api/:
#   ./api/scripts/deploy.sh --app-name your-app --org your-org
#   docker build -f api/Dockerfile .
#
# One image, two entrypoints (see fly.toml [processes]): the API and the worker
# run identical code, which keeps the pipeline the worker shells out to exactly
# the one the API advertises.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    COURTSIDE_POSE_MODEL=/opt/courtside/yolo11n-pose.pt \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    HF_HOME=/data/hf

# ffmpeg does the decode and frame sampling; libglib2.0-0 is opencv-headless's
# only remaining shared-object dependency on slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libglib2.0-0 ca-certificates curl gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY api/constraints.txt /srv/api/constraints.txt
ENV PIP_CONSTRAINT=/srv/api/constraints.txt

# CPU-only torch first, from PyTorch's CPU index. Without this, ultralytics
# drags in the CUDA build and the image grows by ~2.5 GB for hardware that a
# Fly shared-cpu machine does not have.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.4" "torchvision>=0.19"

COPY api/requirements.txt /srv/api/requirements.txt
RUN pip install --no-cache-dir -r /srv/api/requirements.txt

# The pipeline package. `server` = OpenAI-compatible backend (OpenRouter),
# `pose` = YOLO overlays, `youtube` = URL ingest. Never `local` (Apple-only MLX).
COPY pyproject.toml README.md LICENSE /srv/
COPY courtside /srv/courtside
RUN pip install --no-cache-dir '.[server,pose,youtube]'

# Bake the pose weights so a worker does not re-download them per job.
RUN mkdir -p /opt/courtside && \
    curl -fsSL -o /opt/courtside/yolo11n-pose.pt \
      https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.pt

COPY api/courtside_api /srv/courtside_api

RUN useradd --create-home --uid 10001 courtside && \
    mkdir -p /data/work /tmp/ultralytics && chown -R courtside:courtside /data /tmp/ultralytics
COPY api/scripts/entrypoint.sh /usr/local/bin/courtside-entrypoint
RUN chmod 755 /usr/local/bin/courtside-entrypoint
ENTRYPOINT ["courtside-entrypoint"]

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "courtside_api.app:app", "--host", "0.0.0.0", "--port", "8080"]
