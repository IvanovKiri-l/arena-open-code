# Arena — мульти-модельный бенчмарк

Один промпт — N=10 параллельных инстансов LLM (от консервативных до креативных),
живой стрим всех ответов в браузере, затем выбор победителя: судейство
(опционально 3 судьи + majority vote) и верификация кода (линтер/компилятор/тесты).
Все забеги хранятся в SQLite и экспортируются в JSON.

## Требования

Только **Docker**. Ничего больше — ни Python, ни Node на машине ставить не нужно.

## Запуск (4 команды)

    git clone <URL-вашего-репозитория> arena
    cd arena
    cp .env.example .env     # → впишите свой LLM_API_KEY
    docker compose up

UI откроется на **http://localhost:3000** (порт — переменная `PORT` в `.env`).

## Куда вписать ключ

Файл `.env` в корне проекта — единственное место с настройками. Минимум:

| Переменная     | Что это                                                        |
|----------------|----------------------------------------------------------------|
| `LLM_API_KEY`  | ваш ключ провайдера (OpenAI, DeepSeek, OpenRouter, Ollama…)     |
| `LLM_BASE_URL` | OpenAI-совместимый эндпоинт, по умолчанию OpenAI               |
| `LLM_MODEL`    | id модели, по умолчанию `gpt-4o-mini`                          |

`.env` внесён в `.gitignore` — ключ не попадёт в репозиторий.

## Пример запроса

    curl -X POST http://localhost:3000/api/arena \
      -H "Content-Type: application/json" \
      -d '{"prompt": "Напиши функцию Python fizzbuzz(n) с type hints"}'
    # → {"run_id": "..."} — дальше UI покажет стрим ответов и вердикт судьи

Скриншот: положите свой в `docs/screenshot.png` и раскомментируйте строку ниже.

<!-- ![Скриншот Arena](docs/screenshot.png) -->

## API

| Метод | Путь                      | Назначение                          |
|-------|---------------------------|-------------------------------------|
| POST  | `/api/arena`              | старт забега → `run_id`             |
| GET   | `/api/arena/{id}/stream`  | SSE-поток ответов по мере готовности|
| GET   | `/api/arena/{id}`         | финальный результат забега          |
| GET   | `/api/arena/{id}/export`  | экспорт забега в JSON               |
| GET   | `/api/runs`               | история забегов                     |

## Troubleshooting

1. **`docker compose up` не стартует: порт занят.** Смените `PORT` в `.env` (например, на 3100) и повторите `docker compose up`.
2. **401 / 403 от провайдера.** Неверный `LLM_API_KEY` или `LLM_BASE_URL`: URL должен быть полным, оканчиваться на `/v1` и быть **без** завершающего слэша.
3. **429 / rate limit.** Уменьшите `MAX_CONCURRENCY` в `.env` — арена повторяет запросы с backoff, но при жёстких лимитах помогает только меньшая параллельность.
4. **Ollama / локальный сервер недоступен.** Внутри контейнера `localhost` — это сам контейнер; используйте `LLM_BASE_URL=http://host.docker.internal:11434/v1`.

## Тесты (для разработки, опционально)

    cd backend
    python -m pip install -r requirements.txt
    python -m unittest discover -s tests -v

## CLI (без UI)

    cd backend
    python cli.py "Напиши fizzbuzz(n) на Python" --fake   # офлайн-демо на фейковых клиентах
    python cli.py "..." --judges 3                        # реальный прогон по провайдеру из .env
    python cli.py "..." --verify                          # + прогон ответов через VERIFY_CMD

Полностью локальное демо без ключей и без интернета:

    python tools/mock_provider.py --port 8011
    # и в .env: LLM_BASE_URL=http://127.0.0.1:8011/v1, LLM_MODEL=mock-model, LLM_API_KEY=mock
