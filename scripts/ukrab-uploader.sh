#!/usr/bin/env sh

# A small Docker wrapper for macOS and Linux. It deliberately exposes only the
# required values a user needs to provide; all extraction options use the
# image's defaults.

set -eu

IMAGE="ghcr.io/sirko-ua/audio-bucket-uploader:latest"
API_URL="https://ukrab.work/api/uploader"

usage() {
    cat <<'EOF'
Usage:
  ukrab-uploader.sh API_KEY PATH [public|draft]
  ukrab-uploader.sh --api-key API_KEY --input PATH [--visibility public|draft]

Required values:
  API_KEY            Your Audio Bucket API key.
  PATH               An .mkv file or a directory to scan recursively for .mkv files.

Optional options:
  public|draft       Upload visibility. Defaults to public.
  --api-key API_KEY  Named alternative for API_KEY.
  --input PATH       Named alternative for PATH.
  --visibility VALUE Named alternative for public|draft.

All other uploader settings use their defaults: Ukrainian audio, all subtitle
languages, temporary extracted files, and verbose output.
EOF
}

fail() {
    printf '%s\n' "Error: $*" >&2
    printf '%s\n' "Run '$0 --help' for usage." >&2
    exit 1
}

api_key=""
input_path=""
visibility="public"
visibility_set=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --api-key)
            [ "$#" -ge 2 ] || fail "--api-key requires a value."
            api_key=$2
            shift 2
            ;;
        --input)
            [ "$#" -ge 2 ] || fail "--input requires a path."
            input_path=$2
            shift 2
            ;;
        --visibility)
            [ "$#" -ge 2 ] || fail "--visibility requires a value."
            visibility=$2
            visibility_set=1
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --*)
            fail "Unknown option: $1"
            ;;
        *)
            if [ -z "$api_key" ]; then
                api_key=$1
            elif [ -z "$input_path" ]; then
                input_path=$1
            elif [ "$visibility_set" -eq 0 ]; then
                visibility=$1
                visibility_set=1
            else
                fail "Unexpected argument: $1"
            fi
            shift
            ;;
    esac
done

[ -n "$api_key" ] || fail "--api-key is required."
[ -n "$input_path" ] || fail "--input is required."
[ -e "$input_path" ] || fail "Input path does not exist: $input_path"

case "$visibility" in
    public|draft) ;;
    *) fail "--visibility must be either public or draft." ;;
esac

if [ -d "$input_path" ]; then
    host_input=$(cd "$input_path" && pwd -P)
    container_input="/input"
elif [ -f "$input_path" ]; then
    case "$input_path" in
        *.mkv|*.MKV) ;;
        *) fail "Input file must have an .mkv extension: $input_path" ;;
    esac
    host_input=$(cd "$(dirname "$input_path")" && pwd -P)
    container_input="/input/$(basename "$input_path")"
else
    fail "Input path must be a regular file or directory: $input_path"
fi

command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install and start Docker, then try again."
docker info >/dev/null 2>&1 || fail "Docker is not running. Start Docker, then try again."

printf '%s\n' "Pulling the latest Audio Bucket uploader image..."
docker pull "$IMAGE"

printf '%s\n' "Starting Audio Bucket uploader..."

exec docker run --rm \
    --volume "$host_input:/input:ro" \
    "$IMAGE" \
    --api-key "$api_key" \
    --api-url "$API_URL" \
    --input "$container_input" \
    --visibility "$visibility"
