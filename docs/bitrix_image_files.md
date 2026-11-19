# Изображения с компьютера: что должен уметь сайт

Публикатор умеет добавлять к новости изображения двух видов:

| Вид | Как уходит на сайт | Нужна ли доработка сайта |
|---|---|---|
| Ссылка (из постов Telegram или добавленная вручную) | поле `image_urls`, анонсовое фото — `preview_image_url` | нет, работает как раньше |
| Файл с компьютера | поле `image_files`, анонсовое фото — `preview_image_file` | **да**, см. ниже |

Добавление файлов с компьютера **включено** (`UPLOAD_LOCAL_IMAGES = True` в
`core/config.py`), но сайт должен уметь принимать поле `image_files` и подтверждать
приём. Пока обработчик не доработан, новость создаётся без этих файлов, а после
публикации приложение показывает предупреждение «сайт не подтвердил приём файлов».
Чтобы спрятать кнопки добавления файлов, поставьте `UPLOAD_LOCAL_IMAGES = False`.

## Что присылает приложение

Если среди изображений есть файлы с диска, в JSON добавляются два поля
(если файлов нет — запрос не отличается от прежнего):

```json
{
  "image_urls": ["https://cdn.../1.jpg"],
  "preview_image_url": null,
  "image_files": [
    {"name": "photo_1.jpg", "data": "<JPEG в base64>"}
  ],
  "preview_image_file": "photo_1.jpg"
}
```

- Приложение уменьшает файл до 2000 px по большей стороне и сохраняет как JPEG
  (качество 88, учитывается поворот из EXIF) — настройки `LOCAL_IMAGE_MAX_SIDE`
  и `LOCAL_IMAGE_QUALITY` в `core/config.py`.
- Имена в `image_files` уникальны в пределах одного запроса.
- Анонсовое фото — либо `preview_image_url` (если это ссылка), либо
  `preview_image_file` (если это файл); второе поле при этом `null`/отсутствует.
- Порядок: сначала идут ссылки (`image_urls`), потом файлы (`image_files`),
  внутри каждой группы порядок как в окне.

## Что сайт должен вернуть

В JSON-ответ к обычным полям (`status`, `id_ru`, …) добавьте число сохранённых файлов:

```json
{"status": "success", "id_ru": 123, "files_saved": 1}
```

Приложение сверяет его с числом отправленных файлов. Если поля нет или число меньше,
после публикации появится предупреждение.

## Пример обработчика (PHP)

Это набросок: подставьте его в то место обработчика, где сейчас
обрабатывается `image_urls`, и приведите результат к тому виду, который уже
используется (массив файла Битрикса для свойства или `PREVIEW_PICTURE`).
Он не проверялся на вашем сайте.

```php
<?php
/**
 * Сохраняет файлы из поля image_files во временную папку и возвращает массивы
 * Битрикса (как CFile::MakeFileArray), ключ — имя файла, присланное клиентом.
 */
function dolomit_save_image_files(array $files, int $maxFiles = 20): array
{
    $result = [];
    $dir = $_SERVER['DOCUMENT_ROOT'] . '/upload/tmp/news_import/';
    if (!is_dir($dir) && !mkdir($dir, 0755, true) && !is_dir($dir)) {
        return $result;
    }
    foreach (array_slice($files, 0, $maxFiles) as $f) {
        $name   = (string)($f['name'] ?? '');
        $binary = base64_decode((string)($f['data'] ?? ''), true);
        if ($name === '' || $binary === false || $binary === '') {
            continue;
        }
        $path = $dir . uniqid('img_', true) . '.jpg';
        if (file_put_contents($path, $binary) === false) {
            continue;
        }
        // убеждаемся, что это действительно изображение
        $info = @getimagesize($path);
        if ($info === false || !in_array($info[2], [IMAGETYPE_JPEG, IMAGETYPE_PNG, IMAGETYPE_WEBP], true)) {
            unlink($path);
            continue;
        }
        $arFile = CFile::MakeFileArray($path);
        $arFile['name'] = preg_replace('/[^\w.\-]+/u', '_', $name);
        $result[$name] = $arFile;
    }
    return $result;
}

// использование:
$uploaded = dolomit_save_image_files($data['image_files'] ?? []);
$previewFile = null;
if (!empty($data['preview_image_file']) && isset($uploaded[$data['preview_image_file']])) {
    $previewFile = $uploaded[$data['preview_image_file']];   // анонсовое фото
}
// остальные $uploaded добавьте к галерее вместе с файлами из image_urls
// в ответ добавьте число сохранённых файлов: $response['files_saved'] = count($uploaded);
// после успешного добавления элемента удалите временные файлы из /upload/tmp/news_import/
```

Не забудьте про лимиты размера запроса: `post_max_size` в PHP и
`client_max_body_size` в nginx (JSON с base64 в 1,33 раза больше самих файлов;
10 фото по ~0,5 МБ — это около 7 МБ).

