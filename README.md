# Audio Bucket Uploader

Витягує аудіодоріжки й доріжки субтитрів із файлів `.mkv` за мовою та завантажує їх до Audio Bucket.

## Огляд

- знаходить один файл `.mkv` або рекурсивно сканує каталог на наявність файлів `.mkv`
- витягує відповідні аудіодоріжки й доріжки субтитрів за допомогою `mkvextract`
- називає витягнуті файли за шаблоном:

```text
{original_movie_name}_track{track_id_inside_container}.{detected_extension}
```

- завантажує кожну витягнуту доріжку до Audio Bucket як чернетку або публічну доріжку через `POST /api/uploader`
- передає у запиті на завантаження витягнутий `media_file`, оригінальні JSON/текст MediaInfo для MKV, `ID` MediaInfo як `track_id_inside_container` та вибране значення `visibility`
- показує поступ витягання та завантаження для кожного файлу під час надсилання кожної витягнутої доріжки
- виводить докладні результати виявлення й витягання у вигляді таблиць
- видаляє кожен витягнутий файл після успішного завантаження, якщо не вказано `--keep-extracted`
- за потреби також завантажує окремі (самостійні) аудіофайли й файли субтитрів, що лежать поряд зі своїм відеофайлом-джерелом (див. [Самостійні файли](#самостійні-файли))

## Аргументи

| Аргумент | Обов’язковий | Типове значення | Опис |
| --- | --- | --- | --- |
| `--api-key` | Так | немає | API-ключ користувача Audio Bucket. Він надсилається як bearer-токен у запиті на завантаження. |
| `--api-url` | Так | немає | URL кінцевої точки завантажувача Audio Bucket, наприклад `https://audio-bucket.site/api/uploader`. |
| `--input` | Ні | `/input` | Шлях до одного файлу `.mkv` або каталогу з файлами `.mkv`. Каталоги скануються рекурсивно. |
| `--audio-language` | Ні | `uk` | Мова цільової аудіодоріжки. Передавайте параметр кілька разів або використовуйте значення, розділені комами, наприклад `--audio-language uk --audio-language en` чи `--audio-language uk,en`. |
| `--subtitle-language` | Ні | `all` | Мова цільової доріжки субтитрів. Передавайте параметр кілька разів або використовуйте значення, розділені комами. `all` завантажує кожну доріжку субтитрів незалежно від мови. |
| `--output-dir` | Ні | Тимчасовий каталог ОС | Каталог, до якого витягнуті доріжки записуються перед завантаженням. У macOS і Linux це зазвичай `/tmp`; у Windows використовується стандартне розташування тимчасових файлів зі змінних середовища ОС. |
| `--keep-extracted` | Ні | `false` | Зберігати витягнуті файли після успішного завантаження. Типово завантажені витягнуті файли видаляються. |
| `--visibility` | Ні | `draft` | Видимість завантажених доріжок: `draft` або `public`. |
| `--standalone`, `--no-standalone` | Ні | `true` | Також знаходити й завантажувати окремі (самостійні) аудіофайли та файли субтитрів. Див. [Самостійні файли](#самостійні-файли). Скористайтеся `--no-standalone`, щоб обробляти лише файли `.mkv`. |
| `--verbose`, `--no-verbose` | Ні | `true` | Виводити докладну інформацію про виявлення файлів, результати витягання та очищення. Дані про виявлення й витягання показуються у таблицях. Скористайтеся `--no-verbose`, щоб вимкнути докладний вивід. |

Мовні фільтри нормалізуються, а для мов на кшталт `uk`, `ukr` та `ukrainian` підтримуються поширені псевдоніми.

## Самостійні файли

З `--standalone` (типове значення) завантажувач також обробляє окремі аудіофайли й файли субтитрів, а не лише доріжки всередині контейнерів `.mkv`:

- **Аудіо:** `wav`, `mp3`, `aac`, `flac`, `ogg`, `m4a`, `opus`, `ac3`, `eac3`, `ac4`, `dts`, `dtshd`, `truehd`, `mlp`, `thd`
- **Субтитри:** `ass`, `srt`, `pgs`, `sup`

Кінцева точка завантажувача ідентифікує доріжку за `unique_id` з MediaInfo відео-джерела й зчитує мову доріжки з MediaInfo того самого відео. Тому самостійний файл завантажується лише за виконання всіх умов нижче, інакше він пропускається з поясненням:

1. Його мову можна визначити з імені файлу (наприклад, `Movie.uk.srt`, `Show.en-GB.forced.srt`) або з MediaInfo.
2. У тому самому каталозі є **відеофайл-сусід** з такою самою основою імені (наприклад, `Movie.mkv` поряд із `Movie.uk.srt` чи `Movie_track2.eac3`). Відео повинно мати `unique_id` у MediaInfo — `.mkv` його має, більшість `.mp4` — ні.
3. Це відео містить доріжку **того самого типу й мови**, до якої прикріплюється файл.

Самостійні файли-джерела ніколи не видаляються (`--keep-extracted` на них не поширюється).

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

Також працює форма з іменованими параметрами: `./ukrab-uploader.sh --api-key <your_api_key> --input /path/to/movie-or-directory --visibility draft`. Допоміжний скрипт автоматично завантажує образ `ghcr.io/sirko-ua/audio-bucket-uploader:latest`, коли це потрібно, і використовує `https://ukrab.work/api/uploader`. Він зберігає стандартні значення завантажувача: українська аудіодоріжка, усі субтитри, тимчасові витягнуті файли та докладний вивід.

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

Також працює форма з іменованими параметрами: `.\ukrab-uploader.bat --api-key <your_api_key> --input "C:\path\to\movie-or-directory" --visibility draft`.

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
