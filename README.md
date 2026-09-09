# Audio Bucket Uploader

Витягує аудіодоріжки, доріжки субтитрів і вкладені шрифти з файлів `.mkv` та завантажує їх до Audio Bucket.

## Огляд

- знаходить один файл `.mkv` або рекурсивно сканує каталог на наявність файлів `.mkv`
- перед витяганням перевіряє `unique_id` відео з MediaInfo через `POST /api/uploader/original-video-check` і пропускає доріжки та вкладення, які вже зберігаються на сервері
- витягує відповідні аудіодоріжки, доріжки субтитрів і вкладені файли шрифтів одним викликом `mkvextract`
- називає витягнуті файли за шаблоном:

```text
{original_movie_name}_track{track_id_inside_container}.{detected_extension}
```

Витягнуті шрифти отримують імена за шаблоном:

```text
{original_movie_name}_attachment{attachment_id}_{safe_container_filename}
```

- завантажує кожну витягнуту доріжку до Audio Bucket як чернетку або публічну доріжку через `POST /api/uploader`
- завантажує кожен вкладений шрифт через `POST /api/uploader/attachments`, зберігаючи його UID із контейнера Matroska
- перед завантаженням обчислює BLAKE3-256 хеш і перевіряє його через `POST /api/uploader/hash-check`, щоб не створювати дублікати
- передає у запиті на завантаження витягнутий `media_file`, оригінальні JSON/текст MediaInfo для MKV, `ID` MediaInfo як `track_id_inside_container` та вибране значення `visibility`
- показує поступ витягання й завантаження, успішні результати, пропуски, помилки та підсумок в одному стислому структурованому стилі
- з параметром `--verbose` додатково виводить списки файлів, таблиці витягнутих даних, HTTP-статуси, подробиці `mkvextract` і шляхи очищення
- видаляє кожен витягнутий файл після успішного завантаження, якщо не вказано `--keep-extracted`
- за потреби також завантажує окремі (самостійні) аудіофайли й файли субтитрів, що лежать поряд зі своїм відеофайлом-джерелом (див. [Самостійні файли](#самостійні-файли))

## Аргументи

| Аргумент | Обов’язковий | Типове значення | Опис |
| --- | --- | --- | --- |
| `--api-key` | Так | немає | API-ключ користувача Audio Bucket. Він надсилається як bearer-токен під час завантаження та в `X-API-Key` для перевірки оригінального відео. |
| `--api-url` | Так | немає | URL кінцевої точки завантажувача Audio Bucket, наприклад `https://audio-bucket.site/api/uploader`. |
| `--input` | Ні | `/input` | Шлях до одного файлу `.mkv` або каталогу з файлами `.mkv`. Каталоги скануються рекурсивно. |
| `--audio-language` | Ні | `uk` | Мова цільової аудіодоріжки. Передавайте параметр кілька разів або використовуйте значення, розділені комами, наприклад `--audio-language uk --audio-language en` чи `--audio-language uk,en`. |
| `--subtitle-language` | Ні | `all` | Мова цільової доріжки субтитрів. Передавайте параметр кілька разів або використовуйте значення, розділені комами. `all` завантажує кожну доріжку субтитрів незалежно від мови. |
| `--output-dir` | Ні | Тимчасовий каталог ОС | Каталог, до якого витягнуті доріжки й шрифти записуються перед завантаженням. У macOS і Linux це зазвичай `/tmp`; у Windows використовується стандартне розташування тимчасових файлів зі змінних середовища ОС. |
| `--keep-extracted` | Ні | `false` | Зберігати витягнуті файли після успішного завантаження. Типово завантажені витягнуті файли видаляються. |
| `--visibility` | Ні | `public` | Видимість завантажених доріжок: `draft` або `public`. |
| `--standalone`, `--no-standalone` | Ні | `true` | Також знаходити й завантажувати окремі (самостійні) аудіофайли та файли субтитрів. Див. [Самостійні файли](#самостійні-файли). Скористайтеся `--no-standalone`, щоб обробляти лише файли `.mkv`. |
| `--verbose`, `--no-verbose` | Ні | `false` | Додатково виводити списки файлів, таблиці витягнутих даних, HTTP-статуси, подробиці `mkvextract` і шляхи очищення. Тіла HTTP-запитів і відповідей ніколи не виводяться. |

Мовні фільтри нормалізуються, а для мов на кшталт `uk`, `ukr` та `ukrainian` підтримуються поширені псевдоніми.

## Журнал

Кожна подія має формат `час | рівень | ціль | дія | подробиці`. Типовий стислий вивід містить загальні лічильники, поступ витягання й завантаження, результати, пропуски, помилки та фінальний підсумок. Подробиці за можливості записуються як компактні поля `ключ=значення`. Параметр `--verbose` додає діагностику, не змінюючи формат.

```text
<timestamp> | INFO | Movie.mkv | extract | tracks=2 attachments=4
<timestamp> | INFO | Movie_track2.eac3 | upload | kind=track type=audio visibility=public id=123
<timestamp> | INFO | run | summary | tracks_extracted=2 attachments_extracted=4 tracks_uploaded=2 attachments_uploaded=4 already_published=0 attachments_already_present=0 standalone_uploaded=0 standalone_skipped=0 failed=0
```

## Вкладені шрифти

Вкладені шрифти визначаються зі структурованого масиву `attachments`, який повертає `mkvmerge -J`. Завантажувач розпізнає офіційні MIME-типи шрифтів Matroska, їхні поширені застарілі варіанти та розширення шрифтів для вкладень із загальним типом `application/octet-stream`. Обкладинки та інші вкладення, що не є шрифтами, ігноруються.

Доріжки й шрифти передаються до різних режимів одного виклику `mkvextract`, тому файл MKV не проходить повторний цикл витягання. Вкладені імена перетворюються на безпечні компоненти імені файлу перед записом до `--output-dir`; ім'я з контейнера не може вибрати каталог призначення.

Кожен шрифт завантажується на URL, утворений додаванням `/attachments` до `--api-url`. Наприклад, `https://audio-bucket.site/api/uploader` перетворюється на `https://audio-bucket.site/api/uploader/attachments`. Multipart-запит містить `original_video_mediainfo`, `original_video_mediainfo_text`, `media_file`, складене значення `{video_name}_attachment{attachment_id}_{container_filename}` як `original_filename` та беззнаковий 64-бітний `uid` вкладення. Ім'я multipart-файлу та `original_filename` завжди мають однакове складене значення. Завантажені файли шрифтів видаляються, якщо не вказано `--keep-extracted`.

## Самостійні файли

З `--standalone` (типове значення) завантажувач також обробляє окремі аудіофайли й файли субтитрів, а не лише доріжки всередині контейнерів `.mkv`:

- **Аудіо:** `wav`, `mp3`, `aac`, `flac`, `ogg`, `m4a`, `opus`, `ac3`, `eac3`, `ac4`, `dts`, `dtshd`, `truehd`, `mlp`, `thd`
- **Субтитри:** `ass`, `srt`, `pgs`, `sup`

Кінцева точка завантажувача ідентифікує доріжку за `unique_id` з MediaInfo відео-джерела й зчитує тип і мову доріжки з MediaInfo того самого відео. Тому самостійний файл завантажується лише за виконання обох умов нижче, інакше він пропускається з поясненням:

1. Його мову можна визначити з імені файлу (наприклад, `Movie.uk.srt`, `Show.en-GB.forced.srt`, `Movie_track2_[ukr]_DELAY 0ms.eac3`) або з MediaInfo.
2. У тому самому каталозі є **відеофайл-сусід** з такою самою основою імені (наприклад, `Movie.mkv` поряд із `Movie.uk.srt` чи `Movie_track2.eac3`). Відео повинно мати `unique_id` у MediaInfo — `.mkv` його має, більшість `.mp4` — ні.

Відео-джерело **не обов'язково** має вже містити відповідну доріжку. Зовнішні субтитри зазвичай не мають відповідника всередині контейнера (`Movie.mkv` має українську аудіодоріжку, але не має українських субтитрів), тому MediaInfo самого самостійного файлу додається до MediaInfo відео-джерела як додаткова доріжка, а `track_id_inside_container` вказує на неї. Загальний `unique_id` залишається таким, як у справжнього відео-джерела, тож завантаження потрапляє до правильного релізу, а кінцева точка зчитує правильні тип і мову.

Самостійні файли-джерела ніколи не видаляються (`--keep-extracted` на них не поширюється).

## Перевірка дублікатів

Перед запуском `mkvextract` завантажувач надсилає `unique_id` оригінального відео з MediaInfo до `POST /api/uploader/original-video-check` із заголовком `X-API-Key`. Якщо відео вже існує, витягаються лише ті доріжки, чиїх ID MediaInfo немає в `track_ids_inside_container`, і ті шрифти, чиїх складених імен немає в `attachment_original_filenames`. Значення `ID` MediaInfo зіставляється з відповідним ID доріжки для `mkvextract` (зазвичай це інше, нуль-базоване число); у поле `track_id_inside_container` і далі надсилається саме ID MediaInfo.

Перед кожним завантаженням програма обчислює 64-символьний BLAKE3-256 дайджест витягнутого файлу та надсилає його до `POST /api/uploader/hash-check`. Якщо API повертає `{"exists": true}`, файл вважається вже опублікованим і завантаження пропускається.

Перевірка залежить лише від хешу файлу й не змінюється через `--visibility`: вона виконується однаково для `public` і `draft`. Параметр `--visibility` використовується лише під час фактичного завантаження, коли API повернув `{"exists": false}`. Тому повторний запуск з іншою видимістю також буде пропущено, якщо API вже знайшов цей хеш.

Перевірка застосовується і до самостійних файлів доріжок. Вкладені шрифти завантажуються за UID через окрему кінцеву точку й не використовують перевірку хешу доріжок.

## Найпростіший спосіб запуску (macOS і Linux)

Спочатку встановіть і запустіть [Docker](https://www.docker.com/get-started/). Потім завантажте допоміжний скрипт і дозвольте його виконання:

```bash
curl -fsSLO https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Або скористайтеся `wget`:

```bash
wget https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Запустіть його з вашим API-ключем і шляхом до одного файлу `.mkv` або каталогу з файлами `.mkv`. Каталоги скануються рекурсивно:

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory
```

Типово завантаження є публічними. Щоб натомість створювати чернетки, додайте `draft`:

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory draft
```

Також працює форма з іменованими параметрами: `./ukrab-uploader.sh --api-key <your_api_key> --input /path/to/movie-or-directory --visibility draft`. Додайте `--verbose` до будь-якої форми для докладного виводу (або `--no-verbose`, щоб явно вибрати стислий вивід). Допоміжний скрипт автоматично завантажує образ `ghcr.io/sirko-ua/audio-bucket-uploader:latest`, коли це потрібно, і використовує `https://ukrab.work/api/uploader`. Він зберігає стандартні значення завантажувача: українська аудіодоріжка, усі субтитри, тимчасові витягнуті файли та стислий вивід.

## Найпростіший спосіб запуску (Windows)

Спочатку встановіть і запустіть [Docker Desktop](https://www.docker.com/products/docker-desktop/). У PowerShell завантажте допоміжний скрипт для Windows:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.bat -OutFile ukrab-uploader.bat
```

Запустіть його з вашим API-ключем і шляхом до одного файлу `.mkv` або каталогу з файлами `.mkv`:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory"
```

Типово завантаження є публічними. Додайте `draft` як останній аргумент, щоб натомість створювати чернетки:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory" draft
```

Також працює форма з іменованими параметрами: `.\ukrab-uploader.bat --api-key <your_api_key> --input "C:\path\to\movie-or-directory" --visibility draft`. Додайте `--verbose` до будь-якої форми для докладного виводу (або `--no-verbose`, щоб явно вибрати стислий вивід).

## Запуск Docker безпосередньо

Завантажте опублікований образ:

```bash
docker pull ghcr.io/sirko-ua/audio-bucket-uploader:latest
```

Мінімально необхідні параметри:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader
```

Тут використовується типове значення `--input /input`, тому завантажувач сканує змонтований каталог із фільмами.

Повна команда з усіма доступними параметрами:

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

## Локальний запуск

Спочатку встановіть залежності Python:

```bash
python -m pip install -r requirements.txt
```

Мінімально необхідні параметри:

```bash
python -m uploader \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader
```

Повна команда:

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
