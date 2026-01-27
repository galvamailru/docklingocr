## Docling PDF OCR Server (OCR2)

Расширенная версия сервера на FastAPI с Docling:
- распознанный текст
- список объектов документа (тип: `table`, `image`, `text`, bbox, фрагмент текста)

Также есть простой Web UI для загрузки PDF и просмотра результатов.

### Быстрый старт (docker-compose)
```
docker-compose up --build
```
UI: `http://127.0.0.1:8001/`

### Запуск в Docker (без compose)
1) Сборка:
```
docker build -t docling-ocr2:latest .
```
2) Запуск:
```
docker run --rm -p 8001:8000 docling-ocr2:latest
```

### Локальный запуск (без Docker)
1) Установка:
```
pip install -r requirements.txt
```
2) Старт:
```
uvicorn app.main:app --reload
```
UI: `http://127.0.0.1:8000/`

### API
- `GET /healthz` — проверка статуса.
- `GET /` — редирект на Web UI.
- `POST /parse` — multipart поле `file` (PDF). Ответ:
  - `filename`
  - `text`
  - `objects`: массив `{type, bbox, text}`, где:
    - `type` ∈ `table | image | text | ...`
    - `bbox` — координаты объекта (если есть)
    - `text` — фрагмент содержимого (для таблиц — markdown, для изображений — подпись, для текста — сам текст)

### Заметки
- Для OCR и рендеринга в Docker ставятся `tesseract-ocr`, `poppler-utils`, `libgl1`, `libglib2.0-0`.
- Если нужен русский OCR, добавьте пакет `tesseract-ocr-rus` в `Dockerfile`.

