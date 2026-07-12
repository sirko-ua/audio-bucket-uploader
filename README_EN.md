# Audio Bucket Uploader

Extracts audio and subtitle tracks from `.mkv` files by language and uploads them to Audio Bucket.

## Overview

- finds one `.mkv` file or recursively scans a directory for `.mkv` files
- extracts matching audio and subtitle tracks with `mkvextract`
- names extracted files as:

```text
{original_movie_name}_track{track_id_inside_container}.{detected_extension}
```

- uploads each extracted track to Audio Bucket as a draft or public track through `POST /api/uploader`
- sends the extracted `media_file`, original MKV MediaInfo JSON/text, MediaInfo `ID` as `track_id_inside_container`, and the selected `visibility` in the upload request
- shows per-file extraction progress and per-file upload progress while each extracted track is being sent
- prints verbose detection and extraction results as tables
- removes each extracted file after a successful upload unless `--keep-extracted` is set
- optionally also uploads standalone (loose) audio and subtitle files found next to their source video (see [Standalone files](#standalone-files))

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
| `--visibility` | No | `draft` | Visibility for uploaded tracks: `draft` or `public`. |
| `--standalone`, `--no-standalone` | No | `true` | Also discover and upload standalone (loose) audio/subtitle files. See [Standalone files](#standalone-files). Use `--no-standalone` to process `.mkv` files only. |
| `--verbose`, `--no-verbose` | No | `true` | Print detailed file detection, extraction results, and cleanup output. Detection and extraction details are shown as tables. Use `--no-verbose` to disable it. |

Language filters are normalized, and common aliases are supported for languages such as `uk`, `ukr`, and `ukrainian`.

## Standalone files

With `--standalone` (the default) the uploader also picks up loose audio and subtitle files, not just tracks inside `.mkv` containers:

- **Audio:** `wav`, `mp3`, `aac`, `flac`, `ogg`, `m4a`, `opus`, `ac3`, `eac3`, `ac4`, `dts`, `dtshd`, `truehd`, `mlp`, `thd`
- **Subtitles:** `ass`, `srt`, `pgs`, `sup`

The uploader endpoint identifies a track by the source video's MediaInfo `unique_id` and reads the track language from that video's MediaInfo. A standalone file is therefore uploaded only when all of the following hold, and is otherwise skipped with a printed reason:

1. Its language can be determined from the file name (e.g. `Movie.uk.srt`, `Show.en-GB.forced.srt`) or from MediaInfo.
2. A **sibling video** with the same base name sits in the same directory (e.g. `Movie.mkv` next to `Movie.uk.srt`, or `Movie.mkv` next to `Movie_track2.eac3`). The video must expose a MediaInfo `unique_id` — `.mkv` does; most `.mp4` files do not.
3. That video contains a track of the **same type and language** to attach the file to.

Standalone source files are never deleted (`--keep-extracted` does not apply to them).

## Easiest Way to Run (macOS and Linux)

Install and start [Docker](https://www.docker.com/get-started/) first. Then download the helper script and make it executable:

```bash
curl -fsSLO https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Or, with `wget`:

```bash
wget https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Run it with your API key and either one `.mkv` file or a directory of `.mkv` files. Directories are scanned recursively:

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory
```

Uploads are public by default. To create drafts instead, add `draft`:

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory draft
```

The named form also works: `./ukrab-uploader.sh --api-key <your_api_key> --input /path/to/movie-or-directory --visibility draft`. The helper pulls `ghcr.io/sirko-ua/audio-bucket-uploader:latest` automatically when needed and uses `https://ukrab.work/api/uploader`. It keeps the standard uploader defaults: Ukrainian audio, all subtitles, temporary extracted files, and verbose output.

## Easiest Way to Run (Windows)

Install and start [Docker Desktop](https://www.docker.com/products/docker-desktop/) first. In PowerShell, download the Windows helper script:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.bat -OutFile ukrab-uploader.bat
```

Run it with your API key and the path to one `.mkv` file or a directory of `.mkv` files:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory"
```

Uploads are public by default. Add `draft` as the final argument to create drafts instead:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory" draft
```

The named form also works: `.\ukrab-uploader.bat --api-key <your_api_key> --input "C:\path\to\movie-or-directory" --visibility draft`.

## Run With Docker Directly

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
  --visibility public \
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
  --visibility public \
  --verbose
```
