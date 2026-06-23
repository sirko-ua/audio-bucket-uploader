# Audio Bucket Uploader

Extracts audio and subtitle tracks from `.mkv` files by language and uploads them to Audio Bucket.

## Overview

- finds one `.mkv` file or recursively scans a directory for `.mkv` files
- extracts matching audio and subtitle tracks with `mkvextract`
- names extracted files as:

```text
{original_movie_name}_[{track_language}_{codec}_{channels}_{bitrate}_{FPS_of_video}].{extension}
```

- uploads each extracted track to Audio Bucket as a draft through `POST /api/uploader`
- sends `track_type`, `language`, `fps`, and `media_file` in the upload request
- shows per-file upload progress while each extracted track is being sent
- removes each extracted file after a successful upload unless `--keep-extracted` is set

For subtitle tracks without channel or bitrate metadata, `na` is used in those slots.
If the source video FPS cannot be detected, the track is not uploaded because the API requires `original_video_fps`.

## Arguments

| Argument | Required | Default | Description |
| --- | --- | --- | --- |
| `--api-key` | Yes | none | Audio Bucket user API key. It is sent as a bearer token in the upload request. |
| `--api-url` | Yes | none | Audio Bucket uploader endpoint URL, for example `https://audio-bucket.site/api/uploader`. |
| `--input` | No | `/input` | Path to a single `.mkv` file or a directory containing `.mkv` files. Directories are scanned recursively. |
| `--audio-language` | No | `uk` | Target audio track language. Pass it multiple times or use comma-separated values, for example `--audio-language uk --audio-language en` or `--audio-language uk,en`. |
| `--subtitle-language` | No | `all` | Target subtitle track language. Pass it multiple times or use comma-separated values. `all` uploads every subtitle track regardless of language. |
| `--output-dir` | No | OS-specific temp directory | Directory where extracted tracks are written before upload. On macOS and Linux this is typically `/tmp`; on Windows it follows the standard temp location from the OS environment. |
| `--keep-extracted` | No | `false` | Keep extracted files after successful upload. By default, uploaded extracted files are deleted. |
| `--verbose`, `--no-verbose` | No | `true` | Print detailed extraction planning and cleanup output. Use `--no-verbose` to disable it. |

Language filters are normalized, and common aliases are supported for languages such as `uk`, `ukr`, and `ukrainian`.

## Run With Docker

Pull the published image:

```bash
docker pull ghcr.io/sirko-ua/audio-bucket-uploader:latest
```

Minimum required parameters:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader
```

This uses the default `--input /input`, so the uploader scans the mounted movie directory.

Full version with all available parameters:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  -v /path/to/extracted:/output \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --input /input \
  --audio-language uk \
  --subtitle-language all \
  --output-dir /output \
  --keep-extracted \
  --verbose
```

## Run Locally

Install Python dependencies first:

```bash
python -m pip install -r requirements.txt
```

Minimum required parameters:

```bash
python -m uploader \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader
```

Full version:

```bash
python -m uploader \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --input /media/movies \
  --audio-language uk,en \
  --subtitle-language all \
  --output-dir ./extracted \
  --keep-extracted \
  --verbose
```
