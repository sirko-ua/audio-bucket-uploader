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
- computes a BLAKE3-256 hash and checks `POST /api/uploader/hash-check` before uploading to prevent duplicates
- sends the extracted `media_file`, original MKV MediaInfo JSON/text, MediaInfo `ID` as `track_id_inside_container`, and the selected `visibility` in the upload request
- shows per-file extraction progress and per-file upload progress while each extracted track is being sent
- prints verbose detection and extraction results as tables
- removes each extracted file after a successful upload unless `--keep-extracted` is set

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
| `--visibility` | No | `public` | Visibility for uploaded tracks: `draft` or `public`. |
| `--verbose`, `--no-verbose` | No | `true` | Print detailed file detection, extraction results, and cleanup output. Detection and extraction details are shown as tables. Use `--no-verbose` to disable it. |

Language filters are normalized, and common aliases are supported for languages such as `uk`, `ukr`, and `ukrainian`.

## Duplicate Check

Before each upload, the uploader computes the extracted file’s 64-character BLAKE3-256 digest and sends it to `POST /api/uploader/hash-check`. If the API returns `{"exists": true}`, the file is treated as already published and the upload is skipped.

The duplicate check is based only on the file hash and is not affected by `--visibility`: it runs the same way for both `public` and `draft`. The `--visibility` value is used only for an actual upload after the API returns `{"exists": false}`. Therefore, rerunning with a different visibility also skips the file if the API already finds that hash.

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
