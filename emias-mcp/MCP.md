# MCP-сервер медкарты ЕМИАС

Реализация по требованиям к безопасности MCP-серверов: stdio MCP, безопасный
вход через официальную страницу mos.ru, состояние без секретов в чате,
read-only первый релиз, нормализованные DTO, сборки `.mcpb` и onefile для
OpenCode.

## Состав

```text
emias/                 # HTTP-клиент медкарты (общий с CLI)
emias_mcp/
  mcp_server.py        # FastMCP, tools и shutdown
  service.py           # операции, DTO, проверки периода/лимитов
  login.py             # Playwright-вход через mos.ru + перехват токенов
  state.py             # Keychain macOS, fallback 0600, импорт CLI-сессии
  safe_log.py          # журнал без секретов и ПДн
  errors.py            # безопасные ошибки
run_server.py          # точка входа
bank_extension/        # упаковка .mcpb
../scripts/
  build_mcpb.sh
  build_opencode.py
opencode.json.example
```

## Вход

```text
session_status = bootstrap
  -> open_bootstrap  (запускает Chrome на login.mos.ru)
  -> пользователь входит на mos.ru: телефон/пароль/код
  -> SPA медкарты сохраняет токены в localStorage
  -> worker забирает access/refresh, кладёт в Keychain
  -> bootstrap_status = ready
  -> quick_login = ready
```

* Секреты вводятся **только на сайте mos.ru**, не в чате и не на локальной
  форме. Пароль и SMS-код сервер не видит.
* Один постоянный поток Playwright, профиль Chrome — временный каталог,
  удаляется после входа.
* `quick_login` проверяет токен read-only запросом профиля; при истёкшем
  access делает один refresh.
* Если браузер закрыт пользователем — статус `closed`, токены не появляются.

## Инструменты (read-only)

| Инструмент | Что возвращает |
|---|---|
| `session_status` | bootstrap / login_pending / quick_login / ready |
| `open_bootstrap` | запуск входа в Chrome |
| `bootstrap_status` | waiting / ready / timeout / closed |
| `close_bootstrap` | закрыть окно входа |
| `quick_login` | проверка сессии и число пациентов |
| `refresh_tokens` | обновление пары токенов |
| `logout` | удалить токены из Keychain |
| `patients` | patient_id (UUID), ФИО, дата рождения, тип доступа |
| `visits` | дата/время, специальность, врач, организация, document_id |
| `analyzes` | дата, код, название, COVID, document_id |
| `vaccinations` | дата, инфекции, вакцина, доза |
| `prescriptions` | дата, препарат, статус, id |
| `referrals` | срок действия, тип, организация |
| `appointments` | текущие записи: дата/время, врач, место |
| `specialities` | id и названия специальностей |
| `purposes` | поводы обращения по категориям |
| `slots` | свободное время; нужны `speciality_id` + `purpose_id` |
| `complaint_tags` | жалобы для опросника (read-only) |
| `complaint_questions` | вопросы и варианты по выбранным жалобам |
| `booking_prepare` | проверка слота + детали + `confirm_token` |
| `booking_execute` | создать запись (одноразовый токен, после подтверждения) |
| `cancel_prepare` | детали записи + `confirm_token` |
| `cancel_execute` | отменить запись (одноразовый токен) |

Периоды: `half_year`, `year`, `all_time` (`month` ЕМИАС отвергает).

В ответах нет токенов, cookie, номера полиса, СНИЛС, URL с query и сырых
JSON. Лимиты (`limit`) ограничены 200.

## Запись и отмена

`reservation-confirm` требует заполненный опросник жалоб (`422 NoComplaint`),
поэтому запись — это управляемый сценарий:

```text
slots
  -> booking_prepare (read-only: перепроверяет слот, возвращает детали + token)
  -> показать пользователю дату/врача/кабинет
  -> отдельное явное подтверждение
  -> [если needs_complaints] complaint_tags -> complaint_questions
     -> вопросы пользователю -> ответы в booking_execute
  -> booking_execute(confirm_token, complaints?)
  -> проверка через appointments
```

* `confirm_token` — 32 hex, TTL 5 минут, одноразовый; хранится в `state.json`
  без секретов.
* execute не принимает подтверждение из prepare: его нужно получить отдельно.
* При ошибке сети исход считается неизвестным: сервер **не повторяет**
  запись и не отменяет бронь; нужно проверить `appointments`.
* При явном отказе сервера (HTTP-статус есть) бронь снимается.
* После успеха выполняется read-only проверка по списку записей.
* Жалобы попадают в медкарту: агент обязан спросить пользователя, а не
  придумывать симптомы.

Отмена: `cancel_prepare` → показать детали → подтверждение → `cancel_execute`,
с такой же политикой неизвестного исхода.

## Состояние и секреты

* Токены — macOS Keychain, служба `emias-medcard-mcp`.
* Fallback (Linux/CI) — `tokens.json` с правами `0600` и атомарной записью.
* `state.json` — только метаданные, без токенов.
* Журнал `bootstrap-debug.log` (0600) — только события: `browser_started`,
  `tokens_captured`, `auth_failed`, HTTP-статус.
* При первом запуске токены могут быть импортированы из
  `~/.config/emias/session.json` (CLI), чтобы не входить повторно.
  Отключается `EMIAS_MCP_IMPORT_CLI=0`.

Переменные окружения:

| Переменная | Назначение |
|---|---|
| `EMIAS_MCP_HOME` | каталог состояния (по умолчанию `~/.emias-mcp`) |
| `EMIAS_MCP_DISABLE_KEYCHAIN=1` | файловый режим, без Keychain |
| `EMIAS_MCP_IMPORT_CLI=0` | не подхватывать сессию CLI |

## Сборка

### Claude Desktop `.mcpb`

```bash
../scripts/build_mcpb.sh emias-mcp
# dist/emias-medcard-mcp-0.2.0.mcpb
```

Требуется `uv`, `npx`. Манифест: `manifest_version 0.4`, запуск через
`uv run --directory ${__dirname} src/run_server.py`.

### OpenCode onefile

```bash
../scripts/build_opencode.py emias-mcp
# dist/opencode/emias-medcard-mcp-0.2.0-darwin-arm64
```

Требуется нативный Python с PyInstaller, Playwright, mcp. Chrome остаётся
обязательным для входа. Пример конфигурации — `opencode.json.example`:

```json
{
  "mcp": {
    "emias-medcard": {
      "type": "local",
      "command": ["/absolute/path/emias-medcard-mcp-0.2.0-darwin-arm64"],
      "enabled": true,
      "timeout": 60000
    }
  }
}
```

## Проверено

* stdio MCP: `initialize`, инструменты, `session_status`,
  `auth_required` без токенов — `unittest`.
* write-сценарии на фейковом клиенте: успешная запись, отмена брони при
  явном отказе, отсутствие повтора при сети, одноразовость токена,
  `complaints_required` без расхода токена.
* Реальный `booking_prepare` на живом слоте: слот перепроверен, токен выдан,
  `needs_complaints=true`; `booking_execute` без жалоб корректно отказал.
* Реальные read-only вызовы через MCP: `quick_login`, `patients`, `visits`,
  `vaccinations`.
* `.mcpb`: распакован, `uv sync`, MCP smoke — 23 инструмента, bootstrap.
* onefile: собран под `darwin-arm64`, MCP smoke — 23 инструмента.
* OpenCode: сервер подключён через `opencode.json`, реальные вызовы
  `session_status → quick_login → patients → visits → slots` прошли.
* Браузерный worker: запуск Chrome, ожидание, закрытие без оставшихся
  процессов (проверено и из onefile).

## Не проверено

* Полный интерактивный вход через `open_bootstrap` с перехватом токенов:
  требует выхода из текущей сессии. Проверен запуск/остановка браузера и
  импорт уже существующих токенов.
* Реальная запись с заполнением жалоб не создавалась: это изменило бы
  медкарту и требует настоящих симптомов пользователя. Проверены
  read-only prepare и отказ без жалоб; полный execute покрыт тестами на
  фейковом клиенте.
* Windows/Linux-сборки и Keychain-поведение вне macOS.

## Тесты

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Покрыто: состояние и права `0600`, отсутствие токенов в `state.json`,
нормализация DTO, stdio-контракт MCP, отказ без токенов.
