# APISIX-MCP

Сервис на базе Micro, предоставляющий MCP-инструменты для ИИ-агента, синхронизирующие состояние корпоративного API-шлюза APISIX и репозитория MPSDK в GitLab.

APISIX выступает единой точкой доступа к внешним API (rate-limiting, мониторинг, управление секретами), а MPSDK — Python-библиотека с удобными обёртками для запросов к этому APISIX. Каждый мутационный инструмент изменяет обе системы: сначала APISIX, затем MPSDK через коммит в дневную ветку `update-DD-MM-YYYY` с единым merge request на день.

Полное техническое задание — в [specs/SPEC.md](specs/SPEC.md).

## MCP Server

- **SDK:** FastMCP (закреплён в `pyproject.toml`).
- **Транспорт:** Streamable HTTP, смонтирован как ASGI sub-app внутри FastAPI-приложения по пути `/mcp` (endpoint: `/mcp/`). Lifespan session-менеджера FastMCP объединён с `app_lifespan` в `src/main.py`.
- **Авторизация:** `/mcp` защищён существующим Keycloak JWT middleware (НЕ входит в `exclude_auth_paths`). Агент вызывает инструменты с заголовком `Authorization: Bearer <jwt>`.
- **Ошибки:** возвращаются как tool result с `isError: true` и структурированным сообщением, никогда как protocol error:

```json
{
    "error": "<human-readable message>",
    "apisix": "applied | reverted | untouched | failed",
    "gitlab": "applied | failed | untouched",
    "detail": "<original exception text>"
}
```

### Инструменты

| Инструмент | Назначение | Аннотации |
|---|---|---|
| `apisix_read` | Текущее состояние route/service/upstream в APISIX (сырой JSON) | `readOnlyHint` |
| `mpsdk_read` | Текущее состояние функции MPSDK (исходник из дневной ветки, иначе из базовой) | `readOnlyHint` |
| `create_route` | Создать route в APISIX + записать функцию в MPSDK. Падает, если route существует | — |
| `update_route` | Обновить route (merge по снапшоту) + обновить функцию. Падает, если route нет | — |
| `deprecate_route` | Префикс `DEPRECATED: ` в имени route + декоратор `@deprecated` у функции | — |
| `delete_route` | Ничего не удаляет: `status: 0` у route + запись в `TO-BE-DELETED.md` | `destructiveHint` |
| `report_issue` | Сообщить о непонятном фрагменте документации: запись в `NEEDS-REVIEW.md` в отдельном MR | — |

### Идентификация route

- **id / name:** без ведущего слеша, `/` → `:`, фигурные скобки сохраняются: `/users/{id}/orders` → `users:{id}:orders`.
- **uri:** параметры пути конвертируются в переменные APISIX: `/users/{id}/orders` → `uri: /users/:id/orders`.
- При создании `name == id`; депрекация добавляет префикс к `name`, id и uri не меняются.
- Все записи — через **PUT** (read-merge-write); PATCH не используется.

### Rate-limit (опционально)

При наличии `rate_limit_count` и `rate_limit_period` к route добавляется плагин `limit-count` (`policy: local`, `rejected_code: 429`). При update без параметров rate-limit существующий плагин сохраняется нетронутым.

## GitLab / MPSDK

- Все изменения дня коммитятся в ветку `update-DD-MM-YYYY` (UTC), отрезанную от `MPSDK_BASE_BRANCH`; ветка создаётся при отсутствии.
- На дневную ветку существует ровно один открытый MR (заголовок `MPSDK update DD-MM-YYYY`, target — `MPSDK_BASE_BRANCH`); создаётся при первом коммите дня.
- Запись функций — AST-upsert: функция с тем же именем заменяется, иначе добавляется в конец файла; файл создаётся при отсутствии.
- Депрекация вставляет `@deprecated("<message>")` из `typing_extensions` над `def`, добавляя импорт при отсутствии.
- Каждый коммит бампает **patch**-версию в `pyproject.toml` MPSDK.
- `TO-BE-DELETED.md` (корень проекта) — append-only, формат строки:

```
- <METHOD> <URI> — <function name> in <path> (marked YYYY-MM-DD)
```

### Needs-review репорты

`report_issue` не трогает дневную ветку обновлений. Запись коммитится в отдельную ветку `needs-review-DD-MM-YYYY` (UTC, от `MPSDK_BASE_BRANCH`) с собственным MR (`MPSDK needs review DD-MM-YYYY`). Коммит содержит **только** `NEEDS-REVIEW.md` — без бампа версии и других изменений. Файл append-only, дубликаты пропускаются:

```
## <anchor link> (reported YYYY-MM-DD)
- URI: <uri или "—"> <method или "">
- Fragment: <текст фрагмента>
- Problem: <что именно непонятно>
```

### Сообщения коммитов

| Инструмент | Сообщение |
|---|---|
| `create_route` | `feat: add <uri>` |
| `update_route` | `fix: update <uri>` |
| `deprecate_route` | `deprecate: <uri>` |
| `delete_route` | `chore: mark <uri> to-be-deleted` |
| `report_issue` | `docs: flag <anchor> for review` |

## Семантика ошибок

- Порядок: **сначала APISIX, затем GitLab** для каждого мутационного инструмента.
- Перед записью снимается снапшот route; при падении GitLab-шага выполняется компенсация:
  - `create_route` → DELETE только что созданного route;
  - `update_route` / `deprecate_route` / `delete_route` → PUT снапшота обратно.
- Инструмент возвращает `isError`-результат с состоянием по системам (`apisix: applied|reverted`, `gitlab: failed`) — агент может безопасно повторить вызов.
- **Идемпотентность:** PUT в APISIX идемпотентен; GitLab-сторона проверяет перед коммитом — если содержимое файла не изменилось, коммит не создаётся.

## Конфигурация

Приложение деплоится через GitLab CI с переменными окружения — все настройки читаются из окружения процесса. В корне репозитория лежит `.env.example` со списком всех переменных; для локальной разработки скопируйте его в `.env.dev` (`make dev` передаёт его uvicorn через `--env-file`). Базовые настройки Micro (`DATABASE_URL`, `KEYCLOAK_URL` и т.д.) по-прежнему читаются механизмом Micro Settings.

| Переменная | Назначение |
|---|---|
| `APISIX_ADMIN_URL` | Базовый URL APISIX Admin API |
| `APISIX_ADMIN_KEY` | Ключ Admin API, заголовок `X-API-KEY` |
| `GITLAB_URL` | Базовый URL GitLab |
| `GITLAB_TOKEN` | Project access token, ограниченный проектом MPSDK |
| `MPSDK_PROJECT_ID` | ID проекта MPSDK в GitLab |
| `MPSDK_BASE_BRANCH` | Базовая ветка для дневных веток (по умолчанию `main`) |
| `EXTERNAL_HTTP_TIMEOUT_S` | Таймаут HTTP-вызовов к APISIX/GitLab (по умолчанию `30`) |

Доступ к GitLab ограничен токеном; ни один инструмент не принимает параметр проекта — все операции адресуются исключительно `MPSDK_PROJECT_ID`.

## Запуск

```bash
# Установка зависимостей (включая dev: pytest, pytest-asyncio, pytest-cov, httpx)
uv sync --extra dev

# Dev режим
make dev
# или на другом порту:
make dev 8001

# Production режим
MICRO_MODE=prod uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

При запуске: поднимается session-менеджер MCP, инициализируется схема БД, стартует телеметрия. Механизм задач и планов Micro не используется (воркер и планировщик не запускаются).

## Структура проекта

```
src/
├── main.py                  # Точка входа, lifespan (MCP + БД + телеметрия), монтирование /mcp
├── mcp_server.py            # Экземпляр FastMCP и регистрация 7 инструментов
├── clients/
│   ├── apisix_client.py     # Async httpx-клиент APISIX 3.x Admin API
│   └── gitlab_client.py     # Async httpx-клиент GitLab REST API v4 (только MPSDK)
├── models/
│   └── tool_calls_table.py  # SQLModel-таблица tool_calls (метрики вызовов)
├── services/
│   ├── sync_service.py      # Оркестрация: порядок, снапшоты, компенсация, AST-upsert
│   └── metrics_service.py   # Запись и агрегация метрик tool_calls
├── ui/
│   ├── metrics_ui.py        # UI-роутер (landing, дашборд метрик /metrics)
│   └── templates/           # Jinja2-шаблоны (landing.html, metrics.html, base.html)
└── utils/
    ├── config.py            # Чтение заголовка/описания из pyproject.toml
    └── db_init.py           # Инициализация схемы БД
```

## База данных

Помимо таблиц Micro (`micro_tasks` и др.) используется таблица `tool_calls`, логирующая каждый вызов инструмента: `tool_name`, `target_uri`, `outcome` (`success` / `error` / `compensated`), `duration_ms`, `created_at`. Таблица создаётся автоматически при старте приложения.

## UI

- `/` — лендинг с описанием проекта и навигацией;
- `/metrics` — дашборд метрик вызовов инструментов (агрегации по `tool_calls`);
- `/micro/` — Micro Dashboard (задачи, планы, телеметрия, запросы);
- `/service/*` — статические файлы из `/var/www/services` (монтируется, только если каталог существует на хосте).

## Требования

- Python 3.13+
- uv (менеджер пакетов)
- PostgreSQL

## Makefile

Доступные команды:

```bash
make dev          # Dev режим с перезагрузкой
make dev 8001     # Dev режим на порту 8001
make sync         # Установка production-зависимостей
make sync-dev     # Установка dev-зависимостей
make test         # Тесты
make patch        # Bump patch-версии (uv version --bump patch)
make minor        # Bump minor-версии (uv version --bump minor)
make lint         # Линтинг (ruff check)
make lint-fix     # Линтинг с автоисправлением
make format       # Форматирование (ruff format)
make typecheck    # Проверка типов (mypy) — см. примечание ниже
make check        # Линтинг + typecheck
make clean        # Удалить кэш (__pycache__, .ruff_cache, .pytest_cache, .mypy_cache)
```

> **Примечание:** `make typecheck` (`mypy micro/`) не работает: локального каталога `micro/` нет — пакет `micro` подключён как git-зависимость и ставится в `.venv`. Команда ничего не проверяет.

## Тесты

Unit- и integration-тесты полностью изолированы: весь внешний HTTP замокан (`httpx.MockTransport`), реальных вызовов к APISIX/GitLab и обращений к БД нет.

```bash
# Все тесты
make test

# Только unit
uv run --no-sync pytest tests/unit/ -v

# Только integration
uv run --no-sync pytest tests/integration/ -v

# Один файл / метод
uv run --no-sync pytest tests/unit/test_sync_service.py -v
uv run --no-sync pytest tests/unit/test_sync_service.py::TestCreateRoute -v

# С покрытием
uv run --no-sync pytest tests/unit/ --cov=src --cov-report=term-missing
```

### Структура тестов

```
tests/
├── unit/
│   ├── test_apisix_client.py   # Клиент APISIX (нормализация id/uri, GET/PUT/DELETE, ошибки)
│   ├── test_gitlab_client.py   # Клиент GitLab (ветки, файлы, коммиты, MR)
│   ├── test_sync_service.py    # Оркестрация: happy path, компенсация, report_issue
│   ├── test_mcp_server.py      # Регистрация инструментов, аннотации, isError-результаты
│   ├── test_config.py          # Утилиты конфигурации
│   ├── test_db_init.py         # Инициализация схемы БД
│   └── test_main.py            # App, lifespan, монтирование /mcp
└── integration/
    ├── test_mcp_api.py         # MCP поверх смонтированного приложения (auth, tools/list, tools/call)
    └── test_metrics_ui.py      # UI (landing, дашборд метрик)
```
