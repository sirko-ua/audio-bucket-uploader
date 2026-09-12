# Audio Bucket Uploader

Витягує вибрані аудіодоріжки, субтитри та вкладені шрифти з `.mkv` і завантажує їх до Audio Bucket.

## Огляд

- Обробляє один `.mkv` або рекурсивно сканує каталог.
- Типово завантажує українське аудіо й усі субтитри; за потреби можна вибрати інші мови.
- Витягує доріжки та шрифти за один прохід `mkvextract`.
- Пропускає вміст, який уже є на сервері, та дублікати завантаження.
- Показує прогрес витягання і завантаження, результати, пропуски, помилки та підсумок.
- За потреби завантажує окремі аудіофайли й субтитри поруч із відеофайлом-джерелом.

## Встановлення і використання

Найпростіший спосіб — допоміжний скрипт на основі Docker. Спочатку встановіть і запустіть [Docker](https://www.docker.com/get-started/).

### macOS і Linux

Завантажте скрипт і дозвольте його виконання:

```bash
curl -fsSLO https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Або скористайтеся `wget`:

```bash
wget https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.sh
chmod +x ukrab-uploader.sh
```

Запустіть його з API-ключем і шляхом до одного `.mkv` або каталогу (каталоги скануються рекурсивно):

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory
```

Типово завантаження публічні. Додайте `draft`, щоб створювати чернетки:

```bash
./ukrab-uploader.sh <your_api_key> /path/to/movie-or-directory draft
```

Також працює форма з іменованими параметрами: `./ukrab-uploader.sh --api-key <your_api_key> --input /path/to/movie-or-directory --visibility draft`. Додайте `--verbose` для докладного виводу. Скрипт за потреби завантажує `ghcr.io/sirko-ua/audio-bucket-uploader:latest`, використовує `https://ukrab.work/api/uploader` і зберігає стандартні параметри.

### Windows

Встановіть і запустіть [Docker Desktop](https://www.docker.com/products/docker-desktop/). У PowerShell завантажте скрипт:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/sirko-ua/audio-bucket-uploader/main/scripts/ukrab-uploader.bat -OutFile ukrab-uploader.bat
```

Запустіть його з API-ключем і шляхом до файлу або каталогу:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory"
```

Додайте `draft` останнім аргументом для створення чернеток:

```powershell
.\ukrab-uploader.bat <your_api_key> "C:\path\to\movie-or-directory" draft
```

Також працює форма з іменованими параметрами: `.\ukrab-uploader.bat --api-key <your_api_key> --input "C:\path\to\movie-or-directory" --visibility draft`. Додайте `--verbose` для докладного виводу.

## Аргументи

| Аргумент | Обов’язковий | Типове значення | Опис |
| --- | --- | --- | --- |
| `--api-key` | Так | немає | API-ключ користувача Audio Bucket. Надсилається як bearer-токен для завантажень і в `X-API-Key` для перевірки оригінального відео. |
| `--api-url` | Так | немає | URL кінцевої точки завантажувача Audio Bucket, наприклад `https://audio-bucket.site/api/uploader`. |
| `--input` | Ні | `/input` | Один `.mkv` або каталог із `.mkv`. Каталоги скануються рекурсивно. |
| `--audio-language` | Ні | `uk` | Мови аудіодоріжок. Повторюйте параметр або передавайте значення через кому, наприклад `--audio-language uk,en`. |
| `--subtitle-language` | Ні | `all` | Мови субтитрів. Повторюйте параметр або передавайте значення через кому; `all` завантажує всі субтитри. |
| `--output-dir` | Ні | Тимчасовий каталог ОС | Каталог для витягнутих доріжок і шрифтів перед завантаженням. |
| `--keep-extracted` | Ні | `false` | Зберігати витягнуті файли після успішного завантаження. |
| `--visibility` | Ні | `public` | Видимість доріжок: `draft` або `public`. |
| `--standalone`, `--no-standalone` | Ні | `true` | Також шукати окремі аудіофайли та субтитри. Див. [Самостійні файли](#самостійні-файли). |
| `--verbose`, `--no-verbose` | Ні | `false` | Показувати списки файлів, таблиці витягнутих даних, HTTP-статуси, подробиці `mkvextract` і шляхи очищення. Тіла запитів не виводяться. |

Мовні фільтри нормалізуються; підтримуються псевдоніми на кшталт `uk`, `ukr` і `ukrainian`.

## Як це працює всередині

### Огляд

Завантажувач читає MediaInfo відеофайлу, вибирає відповідні доріжки та один раз запускає `mkvextract` для доріжок і шрифтів. Витягнуті доріжки називаються так:

```text
{original_movie_name}_track{track_id_inside_container}.{detected_extension}
```

Шрифти мають формат імені:

```text
{original_movie_name}_attachment{attachment_id}_{safe_container_filename}
```

Кожне завантаження доріжки містить витягнутий `media_file`, JSON і текст оригінального MediaInfo MKV, MediaInfo `ID` як `track_id_inside_container` та вибране `visibility`. Успішно завантажені витягнуті файли видаляються, якщо не вказано `--keep-extracted`.

Кожна подія журналу має формат `час | рівень | ціль | дія | подробиці`. Типовий вивід містить поступ витягання/завантаження, результати, пропуски, помилки й підсумок; `--verbose` додає діагностику, не змінюючи формат.

```text
<timestamp> | INFO | Movie.mkv | extract | tracks=2 attachments=4
<timestamp> | INFO | Movie.mkv | extract |  50%
<timestamp> | INFO | Movie_track2.eac3 | upload | kind=track type=audio visibility=public id=123
<timestamp> | INFO | run | summary | tracks_extracted=2 attachments_extracted=4 tracks_uploaded=2 attachments_uploaded=4 already_published=0 attachments_already_present=0 standalone_uploaded=0 standalone_skipped=0 failed=0
```

### Перевірка дублікатів

Перед запуском `mkvextract` завантажувач надсилає `unique_id` оригінального відео з MediaInfo до `POST /api/uploader/original-video-check` із `X-API-Key`. Якщо відео вже існує, витягуються лише ID MediaInfo, відсутні в `track_ids_inside_container`, і шрифти, чиї складені імена відсутні в `attachment_original_filenames`. ID MediaInfo зіставляються з зазвичай іншими, нуль-базованими ID `mkvextract`; до `track_id_inside_container` надсилається саме ID MediaInfo.

Перед кожним завантаженням доріжки обчислюється 64-символьний BLAKE3-256 дайджест і виконується `POST /api/uploader/hash-check`. Відповідь `{"exists": true}` пропускає завантаження. Перевірка однакова для `public` і `draft` та застосовується до самостійних доріжок.

### Вкладені шрифти

Шрифти беруться зі структурованого масиву `attachments`, який повертає `mkvmerge -J`. Завантажувач розпізнає офіційні MIME-типи шрифтів Matroska, поширені застарілі варіанти та розширення шрифтів у вкладеннях `application/octet-stream`; обкладинки й інші вкладення ігноруються. Імена з контейнера перетворюються на безпечні компоненти перед записом у `--output-dir`.

Шрифти завантажуються на URL `--api-url` із доданим `/attachments`, наприклад `https://audio-bucket.site/api/uploader/attachments`. Multipart-запит містить MediaInfo джерела, витягнутий файл, складене ім’я як `original_filename` і беззнаковий 64-бітний UID вкладення Matroska. Для шрифтів використовується цей UID-орієнтований endpoint, а не перевірка хешу доріжок.

### Самостійні файли

З `--standalone` (типово) завантажувач також шукає окремі файли:

- **Аудіо:** `wav`, `mp3`, `aac`, `flac`, `ogg`, `m4a`, `opus`, `ac3`, `eac3`, `ac4`, `dts`, `dtshd`, `truehd`, `mlp`, `thd`
- **Субтитри:** `ass`, `srt`, `pgs`, `sup`

Такий файл завантажується, якщо його мову можна визначити з назви (наприклад, `Movie.uk.srt` або `Movie_track2_[ukr]_DELAY 0ms.eac3`) чи MediaInfo. Відеофайл-сусід із такою самою основою є необов’язковим: якщо він має `unique_id` у MediaInfo, його метадані ідентифікують завантаження, а самостійна доріжка за потреби додається до MediaInfo як додаткова. Без придатного відеофайлу-сусіда завантажувач надсилає власний MediaInfo самостійного файлу. Самостійні файли-джерела ніколи не видаляються.

## Запуск через Docker або локально

### Docker

Завантажте опублікований образ:

```bash
docker pull ghcr.io/sirko-ua/audio-bucket-uploader:latest
```

Мінімальний запуск:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader
```

Тут використовується типове `--input /input`. Щоб зберегти витягнуті файли, змонтуйте каталог із правом запису й вкажіть `--output-dir`:

```bash
docker run --rm \
  -v /path/to/movies:/input:ro \
  -v /path/to/extracted:/output \
  ghcr.io/sirko-ua/audio-bucket-uploader:latest \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --output-dir /output \
  --keep-extracted \
  --verbose
```

### Локально

Встановіть Python-залежності, а також `mediainfo` і MKVToolNix (він містить `mkvmerge` та `mkvextract`), після чого виконайте:

```bash
python -m pip install -r requirements.txt
python -m uploader \
  --api-key <your_api_key> \
  --api-url https://audio-bucket.site/api/uploader \
  --input /media/movies
```
