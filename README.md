# Uploader

Extracts audio and subtitle tracks from `.mkv` files by language and uploads them to Audio Bucket.

## Overview

- API key
- Audio Bucket uploader API URL
- path to one `.mkv` file or a directory containing `.mkv` files
- audio target languages, defaulting to `uk`
- subtitle target languages, defaulting to `all`
- optional output directory, defaulting to the system temp directory
- optional `--keep-extracted` flag to preserve extracted files after upload
- uploads each extracted track to Audio Bucket as a draft through `POST /api/uploader`
- shows per-file upload progress while each extracted track is being sent
- removes each extracted file after a successful upload unless `--keep-extracted` is set

```text
{original_movie_name}_[{track_language}_{codec}_{channels}_{bitrate}_{FPS_of_video}].{extension}
```

For subtitle tracks without channel or bitrate metadata, `na` is used in those slots.
If the source video FPS cannot be detected, the track is not uploaded because the API requires `original_video_fps`.

The upload request sends `track_type`, `language`, `fps`, and `media_file`.

Pass multiple target languages with repeated flags or comma-separated values, for example `--audio-language uk --audio-language en` or `--subtitle-language uk,en`. By default, `--subtitle-language` is `all`, which uploads every subtitle track regardless of language.

## Run with Docker

Pull the published image:

```bash
docker pull ghcr.io/sirko-ua/audio-bucket-uploader:latest
```

Run it:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  -v /path/to/output:/output \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --input /input \
  --audio-language uk \
  --subtitle-language all \
  --output-dir /output \
  --verbose
```

## Run locally

Install Python dependencies first:

```bash
python -m pip install -r requirements.txt
```

```bash
python -m uploader \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader  \
  --input /media/movies \
  --verbose
```

By default, `--audio-language` is `uk` and `--subtitle-language` is `all`. Extracted files are written to the system temp directory. On macOS and Linux this is typically `/tmp`; on Windows it follows the standard temp location from the OS environment.

If you want to keep extracted files, pass `--keep-extracted`. If you want them written somewhere specific, pass `--output-dir /your/path`.

```bash
python -m uploader \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --input /media/movies \
  --audio-language uk \
  --subtitle-language all \
  --output-dir ./extracted \
  --keep-extracted
```
