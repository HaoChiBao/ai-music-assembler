# Assembly worker — same pipeline as `make-short-music-video`.
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    awscli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY music_assembler ./music_assembler
COPY fonts ./fonts

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[segmentation]"

COPY scripts/assemble-job.sh /app/scripts/assemble-job.sh
RUN chmod +x /app/scripts/assemble-job.sh

ENV WORK_DIR=/work
ENTRYPOINT ["/app/scripts/assemble-job.sh"]
