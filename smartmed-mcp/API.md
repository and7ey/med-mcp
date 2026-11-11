# API SmartMed — карта протокола

Документ фиксирует то, что подтверждено по живому шлюзу и официальному
веб-кабинету **10 ноября 2026 года**. API недокументированное: сервер может
изменить пути, версии и форматы без предупреждения. Здесь нет токенов,
телефонов, SMS-кодов и персональных данных.

Источники:

* репозиторий исследования мобильного приложения
  [and7ey/smartmed](https://github.com/and7ey/smartmed) (APK `ru.mts.smartmed`);
* статический разбор веб-кабинета `https://smartmed.pro`
  (сборка `patient@0.21.0`, release date 2026-11-06);
* живые запросы к шлюзу без авторизации.

## 1. Хосты и базы

| Назначение | База |
|---|---|
| Мобильное приложение | `https://gateway.smartmed.pro/` |
| Веб-кабинет: пользовательские методы | `https://gateway.smartmed.pro/personal/api` |
| Веб-кабинет: медицинские методы | `https://gateway.smartmed.pro/personal/proxyapi` |
| Preprod (не используется) | `https://predprod-kong-dev.smartmed.pro/gateway-preprod/` |
| Сам кабинет | `https://smartmed.pro` |

MCP-сервер работает с базами веб-кабинета: токены выдаёт именно он.

## 2. Заголовки

Официальный кабинет отправляет на каждый запрос:

```text
Accept: application/json
Content-Type: application/json
Pragma: no-cache
Application-Version: 2.2.0
OS-Type: Web
CurrentDate: 2026-11-10T18:00:00+03:00
Timezone-Offset: 180
Authorization: <токен как есть>
PatientId: <user_id выбранного пациента, если нужен>
```

* `Application-Version` — версия, которую присылает кабинет; без неё и
  `OS-Type` шлюз отвечает `SYSTEM_ERROR: Неизвестный тип ОС`.
* `CurrentDate` — ISO-время с часовым поясом, `Timezone-Offset` — абсолютное
  смещение в минутах.
* `Authorization` — значение access-токена **как оно пришло**. Мобильное
  приложение кладёт строку без префикса, свежие токены могут уже содержать
  `Bearer `. Клиент сохраняет исходный вид, а при отказе один раз пробует
  второй вариант (только для read-only запросов).
* `PatientId` — числовой `userId` из списка пациентов. Кабинет хранит его в
  `localStorage.__smed-patient-id`.

## 3. Конверт ответа

```json
{
  "data": { },
  "statusInfo": {
    "status": "SUCCESS",
    "infoMessages": [{"code": "…", "descr": "…", "paramName": null}]
  }
}
```

* Успех — `statusInfo.status` со значением `SUCCESS` (в старых сервисах
  встречается `OK`). HTTP 200 сам по себе ничего не доказывает.
* `SESSION_TOKEN_ERROR` — access-токен недействителен, нужен refresh.
* `REFRESH_TOKEN_ERROR` — refresh-токен истёк, нужен повторный вход.
* `APPLICATION_VERSION_TOO_SMALL_ERROR` и код `0` с текстом «приложение
  устарело» — версионный гейт; некоторые сервисы (например,
  `/users/check/v3`) отклоняют версию `2.2.0` при живом кабинете, поэтому
  MCP эти методы не использует.
* `AUTH_ERROR`, `ACCESS_DENY` — отказ авторизации.

## 4. Авторизация

Вход в веб-кабинете — passwordless, по телефону и SMS-коду:

```text
POST /personal/api/users/initlogon/v1
headers: Captcha-Token: <Yandex SmartCaptcha>
body:    {"login": "<телефон>"}
resp:    data.nextSmsRequestInterval — SMS отправлена

POST /personal/api/users/completelogon/v1
body:    {"login": "<телефон>", "smsCode": "<код>"}
resp:    data.token, data.refreshToken, data.userId
```

* Капчу обрабатывает официальная страница кабинета (SmartCaptcha, sitekey
  живёт в бандле). MCP-сервер не решает и не обходит капчу: он открывает
  страницу входа в Chrome, и пользователь вводит телефон и код там.
* Телефон передаётся только цифрами (`ze()` в бандле нормализует номер).
* Токены кабинет хранит в cookies `_smed-at` и `_smed-rt`
  (`path=/`, срок 180 дней).
* Refresh — `POST /personal/api/users/logonbytoken/v2` с телом
  `{"refreshToken": "<…>"}`; ответ содержит новую пару `token`/`refreshToken`.
  При отказе кабинет удаляет сессию и просит войти заново.

`logoff/v4`, `logon/v5`, `register/v4`, `create/v6` — регистрация и выход;
MCP их не вызывает (logout удаляет только локальные токены).

## 5. Read-only методы, которые использует MCP

### Профиль и пациенты

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/personal/api/users/info/v3` | профиль владельца аккаунта |
| GET | `/personal/api/users/getpatients` | массив пациентов |

Элемент `getpatients` — объект с `userDetails` и `isDefault`:

```json
{
  "isDefault": true,
  "userDetails": {
    "userId": 42,
    "lastName": "…", "firstName": "…", "patronymic": "…",
    "birthday": "2015-05-09T00:00:00Z",
    "ageLevel": 1,
    "isClient": true
  }
}
```

`ageLevel`: `1` — ребёнок, `2` — взрослый, `3` — любой.

### Приёмы (медкарта)

```text
POST /personal/proxyapi/reception-line/v3/receptions/medicalcard/list
body: {
  "filters": {
    "types": ["offline_reception", "online_reception",
              "diagnostic_reception", "homecall"],
    "calendar_states": ["current", "upcoming"],
    "patients": ["<userId>"]
  },
  "paginate": {"page": 1, "size": 20}
}
```

Состояния: `current`, `upcoming`, `done`, `canceled`.
Ответ: `data.list` (иногда `items`) со списком приёмов; элемент содержит
`appointment_date`, `type`, `calendar_state` и вложенный `reception`
(`id`, `clinic.name`, `specialty.name`, `doctor.nom/prenom`).

Детали приёма: `GET /personal/proxyapi/core-reception/v3/receptions/{id}`.

### Документы медкарты

| Метод | Путь |
|---|---|
| GET | `/personal/proxyapi/documents_manager/v1/documents/` |
| GET | `/personal/proxyapi/documents_manager/v1/tags/` |
| GET | `/personal/api/file/storage/v2/{file_id}` |

Параметры списка: `page` (с 0), `size`, `search`, `tag_id`, `dateFrom`,
`dateTo` (ISO). Ответ: `data.items`; элемент — `file_id`, `file_name`,
`created_at`, `doctor_name`, `size`, `extension`, `tags[]`,
`is_deletion_allowed`.

Скачивание возвращает бинарный файл (`%PDF`, PNG, JPEG). MCP сохраняет его
в каталог состояния с правами `0600`; содержимое в чат не выводится.

### Направления

```text
GET /personal/proxyapi/referral/v2/referrals
params: page, size, search, isActive, sort=date_asc|date_desc,
        dateFrom, dateTo
```

Ответ: `data.items`; элемент — `referralId`, `name`, `type`, `doctor`,
`state{code,name}`, `isActive`, `bookingAvailability`.

### Назначения

```text
POST /personal/proxyapi/connector/web/purpose/
params: page, size
body:   {"scope": 1 | 2 | 3}     # 1 active, 2 archive, 3 all
```

Ответ: `data.items`, `data.total`, `data.has_items`. Элемент — `id`,
`appointmentId`, `doctor{lastName,firstName,position}`, `medications[]`
(`medicine{name,code,latinName}`, `medicationForm{name}`, `dosage{text}`,
`isRecipe`, `isActive`, `intakeSchedule{text}`).

## 5.1. Запись к врачу и отмена

Веб-кабинет проводит запись через пять шагов. MCP использует те же методы.

### Город

```text
GET /personal/proxyapi/clinics/clinics/v1/cities/
params: page, size, with_clinic=true, context=reception
GET /personal/proxyapi/clinics/clinics/v1/cities/city/user
params: with_clinic=true
```

Ответ города: `data.items[]` с `id`, `name`. `city/user` возвращает
`userLocation.city` либо `nearestCity`.

### Специальности, клиники, врачи

```text
POST /personal/proxyapi/core-reception/v2/specialties/search
body: {paginate:{page,size}, filters:{city_id, online:false, search?}}

POST /personal/proxyapi/core-reception/v2/clinic-specialties
body: {city_id, specialty_ids:[...]}

POST /personal/proxyapi/core-reception/v2/doctor-specialties/search
headers: PatientId: -1
body: {paginate:{page,size},
       filters:{city_id, online:false, search?, specialty_ids?, clinic_ids?}}
```

Клиники возвращаются массивом (`id`, `code`, `name`, `address`), врачи —
`items[]` с `id`, `nom`, `prenom`, `specialties[]`.

### Календарь и слоты

```text
POST /personal/proxyapi/core-reception/v2/calendar-schedules
body: {clinic_ids:[...], specialties_ids:[specialty_id], online:false}
resp: data[] = [{date, is_available}]

POST /personal/proxyapi/core-reception/v2/schedules
body: {filters:{clinic_ids, specialty_ids:[specialty_id], online:false,
                date_visit:"YYYY-MM-DD"},
       paginate:{token:null, size:N}}
resp: data.items[] — расписания, data.paginate.token — следующая страница
```

Внимание: у календаря поле называется `specialties_ids`, у расписания —
`specialty_ids`.

Элемент расписания: `doctor{id,nom,prenom}`, `clinic{id,code,name,address}`,
`schedule_reception_types[].reception_type{id,web_name,price{price,
med_program_id},specialty{name}}`, `slots[]`:
`{id, start_local, start_utcz, schedule_id, reception_type_id,
cabinet_address}`.

Проверка слота:

```text
POST /personal/proxyapi/core-reception/v1/slots/by-ids
body: {schedule_id, reception_type_id, start_utcz}
resp: data.is_free (1/0)
```

Важно: сервер принимает только snake_case (`start_utcz` — ISO-строка вида
`2026-12-10T11:40:00`, не epoch). В бандле кабинета вызов собран через
camelCase-обёртку, но валидация API требует snake_case.

### Создание записи

```text
POST /personal/proxyapi/core-reception/v3/receptions
body: {
  "clinic_code": "<code из clinic>",
  "schedule_id": "<schedule_id слота>",
  "reception_type_id": "<reception_type_id слота>",
  "start_utcz": "<start_utcz слота>",
  "fm_clink_patient_id": "<price.med_program_id, если есть>",
  "referral_id": "<для записи по направлению>",
  "source_client": "web"
}
resp: data — приём
```

После создания кабинет опрашивает
`GET /personal/proxyapi/core-reception/v3/receptions/{id}`:

* `status.name = approved` — запись подтверждена;
* `reserved` — клиника ещё обрабатывает, кабинет повторяет опрос;
* `rejected` — отказ, причина в `errorInfo`.

Типовые ошибки: `AlreadyHaveReceptionWithDoctorThatDayError`,
`AlreadyHaveReceptionWithSpecialistThatDayError`, `SlotsOverlapInTimeError`.

### Отмена

```text
POST /personal/proxyapi/core-reception/v2/receptions/cancel
body: {"reception_id": "<id>"}
```

Если сервер отвечает HTTP 418 и `details.type = AlreadyCancelledError`,
запись уже отменена — это успех. Фактический статус подтверждается повторным
`GET /core-reception/v3/receptions/{id}`. Успешная отмена приходит как
`status.name = canceled_by_client` (бывают и другие `canceled_*`), поэтому
проверять нужно префикс `canceled`, а не только `canceled`.

## 6. Что осталось непроверенным

* Точные форматы ответов `/users/info/v3` и `referral/v2/referrals` на живом
  аккаунте: DTO собраны по именам полей из бандла и защищены от отсутствия
  ключей, но реальные данные не снимались.
* Запись на приём, отмена, оплата, аптека, домашний вызов, чат и телемедицина
  в MCP не реализованы: это мутирующие или непроверенные сценарии.
* Версионный гейт: `2.2.0` проходит для всех используемых read-only методов
  (подтверждено ответом `SESSION_TOKEN_ERROR` вместо ошибки версии). Если
  кабинет поднимет версию, константу `APP_VERSION` в `smartmed/client.py`
  нужно обновить.
* Поведение шлюза вне macOS и при отключённой проверке TLS не проверялось;
  отключать TLS запрещено правилами проекта.
