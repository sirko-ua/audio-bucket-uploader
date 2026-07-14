@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem A small Docker wrapper for Windows. It exposes the required values, upload
rem visibility, and output verbosity; extraction options use the image's defaults.

set "IMAGE=ghcr.io/sirko-ua/audio-bucket-uploader:latest"
set "API_URL=https://ukrab.work/api/uploader"
set "api_key="
set "input_path="
set "visibility=public"
set "visibility_set=0"
set "verbosity_flag="

:parse_args
if "%~1"=="" goto validate_args

if /I "%~1"=="--api-key" (
    if "%~2"=="" (
        call :fail "--api-key requires a value."
        exit /b 1
    )
    set "api_key=%~2"
    shift
    shift
    goto parse_args
)

if /I "%~1"=="--input" (
    if "%~2"=="" (
        call :fail "--input requires a path."
        exit /b 1
    )
    set "input_path=%~2"
    shift
    shift
    goto parse_args
)

if /I "%~1"=="--visibility" (
    if "%~2"=="" (
        call :fail "--visibility requires a value."
        exit /b 1
    )
    set "visibility=%~2"
    set "visibility_set=1"
    shift
    shift
    goto parse_args
)

if /I "%~1"=="--verbose" (
    set "verbosity_flag=--verbose"
    shift
    goto parse_args
)

if /I "%~1"=="--no-verbose" (
    set "verbosity_flag=--no-verbose"
    shift
    goto parse_args
)

if /I "%~1"=="--help" goto usage
if /I "%~1"=="-h" goto usage

set "argument=%~1"
if "%argument:~0,2%"=="--" (
    call :fail "Unknown option: %~1"
    exit /b 1
)

if not defined api_key (
    set "api_key=%~1"
) else if not defined input_path (
    set "input_path=%~1"
) else if "%visibility_set%"=="0" (
    set "visibility=%~1"
    set "visibility_set=1"
) else (
    call :fail "Unexpected argument: %~1"
    exit /b 1
)
shift
goto parse_args

:validate_args
if not defined api_key (
    call :fail "An API key is required."
    exit /b 1
)
if not defined input_path (
    call :fail "An input path is required."
    exit /b 1
)
if not exist "%input_path%" (
    call :fail "Input path does not exist: %input_path%"
    exit /b 1
)

if /I "%visibility%"=="public" goto validate_input
if /I "%visibility%"=="draft" goto validate_input
call :fail "Visibility must be either public or draft."
exit /b 1

:validate_input
if exist "%input_path%\NUL" (
    for %%I in ("%input_path%") do set "host_input=%%~fI"
    set "container_input=/input"
) else (
    for %%I in ("%input_path%") do (
        set "extension=%%~xI"
        set "file_name=%%~nxI"
        for %%J in ("%%~dpI.") do set "host_input=%%~fJ"
    )
    if /I not "%extension%"==".mkv" (
        call :fail "Input file must have an .mkv extension: %input_path%"
        exit /b 1
    )
    set "container_input=/input/%file_name%"
)

where docker >nul 2>&1 || (
    call :fail "Docker is not installed. Install and start Docker, then try again."
    exit /b 1
)
docker info >nul 2>&1 || (
    call :fail "Docker is not running. Start Docker, then try again."
    exit /b 1
)

echo Pulling the latest Audio Bucket uploader image...
docker pull "%IMAGE%"
if errorlevel 1 exit /b %errorlevel%

echo Starting Audio Bucket uploader...
docker run --rm --volume "%host_input%:/input:ro" "%IMAGE%" --api-key "%api_key%" --api-url "%API_URL%" --input "%container_input%" --visibility "%visibility%" %verbosity_flag%
exit /b %errorlevel%

:usage
echo Usage:
echo   ukrab-uploader.bat API_KEY PATH [public^|draft] [--verbose^|--no-verbose]
echo   ukrab-uploader.bat --api-key API_KEY --input PATH [--visibility public^|draft] [--verbose^|--no-verbose]
echo.
echo Required values:
echo   API_KEY            Your Audio Bucket API key.
echo   PATH               An .mkv file or a directory to scan recursively for .mkv files.
echo.
echo Optional options:
echo   public^|draft       Upload visibility. Defaults to public.
echo   --api-key API_KEY  Named alternative for API_KEY.
echo   --input PATH       Named alternative for PATH.
echo   --visibility VALUE Named alternative for public^|draft.
echo   --verbose          Enable detailed uploader output.
echo   --no-verbose       Use concise uploader output (the default).
echo.
echo All other uploader settings use their defaults: Ukrainian audio, all subtitle
echo languages, temporary extracted files, and concise output.
exit /b 0

:fail
echo Error: %~1 1>&2
echo Run "%~nx0 --help" for usage. 1>&2
exit /b 1
