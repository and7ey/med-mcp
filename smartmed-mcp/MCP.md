# MCP-сервер SmartMed

Реализация по требованиям к безопасности MCP-серверов: stdio MCP, безопасный
вход через официальную страницу smartmed.pro, состояние без секретов в чате,
read-only первый релиз, нормализованные DTO, `.mcpb` и onefile для OpenCode.

## Состав

```text
smartmed/                # HTTP-клиент шлюза
  client.py              # заголовки, конверт, refresh, Authorization
  api.py                 # read-only методы кабинета
  errors.py              # безопасные ошибки
smartmed_mcp/
  mcp_server.py          # FastMCP, tools, shutdown
  service.py             # операции, DTO, выбор пациента
  login.py               # Playwright-вход через smartmed.pro
  state.py               # Keychain macOS, fallback 0600
  safe_log.py            # журнал без секретов и ПДн
  errors.py              # безопасные ошибки MCP
run_server.py            # точка входа
bank_extension/          # упаковка .mcpb
../scripts/
  build_mcpb.sh
  build_opencode.py
opencode.json.example
```

## Машина состояния

```text
нет токенов           → bootstrap
браузер открыт         → login_pending
токены, не проверены   → quick_login
токены проверены <15м  → ready
SESSION_TOKEN_ERROR    → один refresh (только read-only) и один повтор
refresh отклонён       → bootstrap (токены не удаляются автоматически)
```

`open_bootstrap` поднимает Chrome на `https://smartmed.pro/sign-in` в
отдельном временном профиле. Worker ловит ответ
`POST /personal/api/users/completelogon/v1`, берёт `token`/`refreshToken`
(резервно — cookies `_smed-at`/`_smed-rt`) и сохраняет пару в Keychain.
Профиль браузера удаляется, cookies никуда не копируются.

## Политика запросов

* Все инструменты чтения — read-only. Запись и отмена — только через
  `prepare → подтверждение → execute`.
* Один refresh и один повтор только для read-only запроса. Мутирующие
  запросы (`booking_execute`, `cancel_execute`) не повторяются.
* `auth_style` (`auto`/`raw`/`bearer`) запоминается после первого успешного
  запроса, чтобы не перебирать заголовок каждый раз.
* Тела ответов и токены не логируются. В журнал идут только имена событий,
  код ошибки и HTTP-статус.

## DTO

Каждый инструмент возвращает минимальный набор полей:

* `patients` — `patient_id`, ФИО, дата рождения, `age_level`, `is_default`;
* `appointments` — дата/время, тип, состояние, специальность, врач, клиника,
  кабинет;
* `documents` — `file_id`, название, дата, врач, теги;
* `referrals` — `referral_id`, название, тип, врач, состояние, активность;
* `prescriptions` — препарат, код, форма, дозировка, курс, признак рецепта;
* `profile` — `user_id`, ФИО, дата рождения, маска телефона.

Телефон маскируется, email и адреса не возвращаются. Сырые ответы шлюза не
выводятся никогда.

## Скачивание документов

`documents` возвращает `file_id`; `document_download` скачивает файл
`GET /personal/api/file/storage/v2/{id}` и сохраняет его в
`<каталог состояния>/documents/`:

* каталог `0700`, файл `0600`;
* имя строится только из `file_id`, прошедшего `[A-Za-z0-9_-]`;
* содержимое в ответ MCP не попадает — только путь, размер и формат.

## Секреты

* Keychain macOS, служба `smartmed-mcp`, аккаунт `tokens`.
* Fallback — `tokens.json` `0600`, атомарная запись, без сетевых каталогов.
* `state.json` не содержит токенов; тест это проверяет.
* `logout` удаляет локальную пару, но не вызывает серверный `logoff`:
  отзыв сессии на стороне SmartMed пользователь делает в кабинете.

## Сборка

### Claude Desktop `.mcpb`

```bash
../scripts/build_mcpb.sh smartmed-mcp
```

Требуется `uv` и `npx`; версия манифеста и пакета — `0.2.0`.

### OpenCode onefile

```bash
../scripts/build_opencode.py smartmed-mcp
```

Требуется нативный Python целевой архитектуры с PyInstaller, Playwright и mcp.
Chrome нужен на машине пользователя только для входа.

На macOS onefile-бинарник стартует десятки секунд (распаковка во временный
каталог и проверка подписи), поэтому для OpenCode рекомендуется запуск из
venv: `PYTHONPATH=<проект> <проект>/.venv/bin/python -m
smartmed_mcp.mcp_server`. Это же подключение записано в `opencode.json`.

## Проверка

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Покрыто: состояние и права `0600`, отсутствие токенов в `state.json`,
нормализация DTO, конверт и refresh на фейковом транспорте, stdio-контракт,
отказ без токенов и полный сценарий записи/отмены на фейковом API.

Проверено вручную на реальном аккаунте:

* вход на smartmed.pro, токены в Keychain, `quick_login = ready`, 5 пациентов;
* живые `appointments`, `cities`, `specialties`, `clinics`,
  `available_dates`, `slots`;
* `booking_prepare` перепроверил слот (`is_free`), запись создана
  (`approved`), появилась в `appointments` с кабинетом;
* `cancel_prepare` → `cancel_execute`: статус `canceled_by_client`, запись
  исчезла из предстоящих, повторный `cancel_prepare` вернул
  `already_canceled`.

Проверено вручную без аккаунта: живой шлюз (базовые URL, заголовки, конверт,
коды сессии), `.mcpb` (uv sync + MCP smoke), onefile `darwin-arm64` (MCP
smoke), браузерный worker (запуск/остановка без остатка процессов).

Не покрыто: живые `profile`, `documents`, `referrals`, `prescriptions`,
сценарий `reserved` (клиника не подтвердила сразу), Windows/Linux. Их нужно
проверить отдельно и не выдавать unit-тесты за end-to-end.
