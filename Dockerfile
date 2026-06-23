# Cloud Run assembly worker — `assemble-from-r2` (boto3 + ffmpeg + rembg).
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY music_assembler ./music_assembler
COPY fonts ./fonts
COPY prompts ./prompts

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

ENV WORK_DIR=/work
ENTRYPOINT ["assemble-from-r2", "--work-dir", "/work"]
