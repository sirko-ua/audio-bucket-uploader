FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Run history and failure log. Mount a host directory here (-v state:/state), or
# a restarted container re-extracts and re-hash-checks everything it already did.
# No VOLUME: with --rm that would only create an anonymous volume and delete it.
ENV UPLOADER_STATE_DIR=/state

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends mediainfo mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

COPY uploader ./uploader

ENTRYPOINT ["python", "-m", "uploader"]
