# Медкарта ЕМИАС: как устроена и как работает её API

Документ собран по production-бандлам сайтов и проверен живыми запросами.
Ничего не подбиралось «на глаз»: все пути, заголовки и имена полей взяты из
исходного JS-кода, который исполняет браузер.

---

## 1. Из чего состоит система

| Хост | Что это | Стек | API |
|---|---|---|---|
| `emias.info` | Портал записи к врачу (лендинг, расписание, клиники) | Next.js | `/api-eip`, `/api-portal`, `/api` |
| `lk.emias.mos.ru` | **«Моя электронная медицинская карта»** — то, что обычно называют «медкартой» | React SPA | **`/api/*`** |
| `app.emias.info` | Заглушка «скачайте мобильное приложение» | статика | — |
| `login.mos.ru` | SUDIR / ЕСИА — вход по учётной записи mos.ru | OAuth 2.0 | `/sps/oauth/ae` |

Медкарта (посещения, анализы, прививки, назначения) живёт **не** на
`emias.info`, а на `lk.emias.mos.ru`. Портал `emias.info` — это витрина и
запись; личный кабинет с документами — отдельное приложение.

Конфиг портала (`https://emias.info/aaa-cfg`) прямо говорит:

```js
APP_LK_API_BASE_PATH: "https://lk.emias.mos.ru"
APP_APPOINTMENT_API_BASE_PATH: "/api-eip"
APP_CLINICS_API_BASE_PATH: "/api-portal"
APP_SUDIR_BASE_URL: "https://login.mos.ru"
APP_SUDIR_CLIENT_ID: "emias.info.web"
```

Конфиг медкарты (`https://lk.emias.mos.ru/config/env.js`):

```js
REACT_APP_SUDIR_CLIENT_ID: 'lk.emias.mos.ru'
REACT_APP_SUDIR_HOST:        'https://login.mos.ru'
REACT_APP_SUDIR_URL:         'https://lk.emias.mos.ru'
```

---

## 2. Авторизация

### 2.1 Схема

```
┌────────────┐   1. открыть OAuth URL    ┌──────────────────┐
│  браузер   │ ────────────────────────▶ │  login.mos.ru    │
│            │                           │  (mos.ru / ЕСИА) │
│            │ ◀──────────────────────── │                  │
└─────┬──────┘   2. redirect ?code=...   └──────────────────┘
      │
      │ 3. POST /api/auth/1/login {code}
      ▼
┌──────────────────┐   4. attempt_token, delay_timeout
│ lk.emias.mos.ru  │ ──────────────────────────────▶ СМС / пуш
│      /api        │
│                  │ ◀────────────────────────────── 5. код
│                  │   6. POST /api/auth/1/confirm {otp_code, attempt_token}
│                  │ ──────────────────────────────▶ access_token + refresh_token
└──────────────────┘
```

OAuth-страница:

```
https://login.mos.ru/sps/oauth/ae
    ?client_id=lk.emias.mos.ru
    &scope=openid+profile+contacts
    &redirect_uri=https://lk.emias.mos.ru/auth
    &response_type=code
    &access_type=offline
```

После входа mos.ru редиректит на
`https://lk.emias.mos.ru/auth?code=<одноразовый код>`.

### 2.2 Ручки авторизации

База: `https://lk.emias.mos.ru/api`, префикс `/auth/1`.

| Метод | Путь | Тело | Ответ |
|---|---|---|---|
| `POST` | `/api/auth/1/login` | `{"code": "...", "redirectUrl": "https://lk.emias.mos.ru/auth"}` | `attempt_token`, `delay_timeout` (иногда сразу `access_token`) |
| `POST` | `/api/auth/1/confirm` | `{"otp_code": "12345", "attempt_token": "..."}` | `access_token`, `refresh_token`, `show_onboarding` |
| `POST` | `/api/auth/1/resend` | `{"attempt_token": "..."}` | новый `attempt_token` / таймер |
| `POST` | `/api/auth/1/refresh` | `{"refresh_token": "..."}` | `access_token`, `refresh_token` |
| `POST` | `/api/auth/1/logout` | — | — |
| `POST` | `/api/auth/1/guest` | — | гостевая сессия (без аккаунта) |

Особенности:

* авторизационные запросы идут **без** токена, но с заголовком
  `X-Environment: prod`;
* `confirm` возвращает snake_case (`access_token`), а гостевой refresh —
  camelCase (`accessToken`) — клиент поддерживает оба;
* токены в браузере лежат в `localStorage` под ключами
  `patient.web.v2.accessToken` и `patient.web.v2.refreshToken`.

### 2.3 Заголовки обычных запросов

```
X-System-Name: LK
Content-Type: application/json
Accept: application/json
X-Access-JWT: <access_token>
```

Без токена вместо `X-Access-JWT` ставится `X-Environment: prod`.

Ответ без токена (проверено):

```http
GET /api/1/profile HTTP/1.1
→ 401
{"code": "Unauthorized", "title": null,
 "message": "X-Access-JWT header is not found", "payload": null}
```

---

## 3. Устройство API медкарты

### 3.1 Как собирается URL

```
https://lk.emias.mos.ru  +  /api  +  [ /auth ]  +  /<версия>  +  /<endpoint>
```

Версия по умолчанию — `/1`; у части ручек она переопределяется (`/2`, `/3`,
`/4`, `/v10` и т. п.). Версия — **часть пути**, а не query-параметр.

Примеры:

```
GET  /api/1/profile
GET  /api/4/documents/inspections?ehrId=...&shortDateFilter=half_year
GET  /api/2/receipt?ehrId=...&shortDateFilter=half_year
POST /api/2/self-appointment/available-resources
```

Query-параметры-массивы передаются повтором ключа:
`?testCodes=A&testCodes=B` (так это делает `URLSearchParams.append`).

Отдельная странность бандла: в паре мест версия передана без ведущего слеша —
например, `version:"2"` в `api-v2/documents-rq/get-medical-certificates.ts`.
При склейке `BASE_URL + version + endpoint` это даёт
`/api2/documents/medical-certificates`, а такой путь отдаёт HTML SPA
(`content-type: text/html`, проверено), т. е. вызов в UI просто сломан.
До API-слоя доходит только вариант без версии —
`/api/1/documents/medical-certificates`; он и указан в списке ниже.

### 3.2 Формат ошибок

```json
{
  "code": "Unauthorized",
  "title": null,
  "message": "Текст для пользователя",
  "payload": null
}
```

`payload` используется для деталей: например, у `OTPIsNotMatched` там лежат
`otpDelay` и `leftAttemptCount`, у `OTPTimerNotYetExpired` — `otpDelay`.

Коды, после которых клиент обновляет токен:
`AuthTokenExpired`, `TokenExpired`, `AccessTokenExpired`, `Unauthorized`,
`AuthTokenInvalid`.

### 3.3 Идентификаторы

* `ehrId` — id электронной медкарты пациента; почти все запросы требуют его
  в query или body. Совпадает с `profileId` в профиле.
* `documentId`, `resultsDocId`, `assignmentsDocId` — id документов.
* `lpuId` / `moId` — медицинская организация.
* `specialityId`, `inquiryPurposeId`, `referralId`, `availableResourceId` —
  параметры записи.

### 3.4 Периоды

`shortDateFilter` (в разных ручках также `dateFilter`):

| Значение | Смысл |
|---|---|
| `half_year` | полгода (значение по умолчанию в UI) |
| `year` | год |
| `all_time` | всё время |

`month` в текущем API **не работает** — `GET /documents/inspections` отвечает
`400 ValidationError`; принимаются только три значения выше (проверено).

---

## 4. Разделы медкарты

### 4.1 Профиль и доступ к картам

| Метод | Путь | Параметры |
|---|---|---|
| `POST` | `/api/1/get-profile` | `{"permissionTypes": ["PERSONAL","CHILD","RELATIVE"]}` |
| `GET` | `/api/2/profile` | `?ehrId=` (резервный вариант) |

Ответ `get-profile`:

```json
{
  "profile": {
    "profileId": "861c1228-34e6-44f6-ae39-49884ac72187",
    "firstName": "Иван", "lastName": "Иванов", "middleName": "Иванович",
    "birthDate": "1990-01-01", "gender": "MALE",
    "phone": "...", "email": "...", "policyNum": "...", "policyNumQr": "...",
    "permissionId": "...", "pictureCode": null, "updatedAt": 0
  },
  "relatives": [ { "...": "те же поля + permissionType = CHILD | RELATIVE" } ]
}
```

Проверено на живом API: `profileId` — **UUID**, а не число; у собственного
профиля `permissionType` отсутствует, у родственников есть (`CHILD`).
`email`/`phone` могут отсутствовать у детей.

`profile` — владелец аккаунта, `relatives` — дети и родственники, к которым
выдан доступ. Это и есть «данные о пациентах».

### 4.2 Счётчики разделов

```
GET /api/1/documents/sections?ehrId=<ehr>&type=<TYPE>
→ {"documentsCount": 12, "newDocumentsCount": 1, "lastActivity": "2025-..."}
```

Типы документов (enum из бандла):

```
ANALYZES, ANALYSIS_CLS, RESEARCH, DISABILITY_FORMS, MEDICAL_CERTIFICATES,
CARD_026, CONCLUSION, RECOMMENDATION_FOR_EXEMPT, EPICRISIS, RECEIPTS,
AMBULANCE, AMBULANCE_ECG, CONSILIUM, VACCINATIONS, IMMUNODIAGNOSTICS,
IMMUNOGLOBULINS, EXEMPTIONS, REFERRALS, APPOINTMENTS, HOSPITALIZATION,
HOSPITALISATION_REFERRALS, FREE_FOOD, CDD_APPOINTMENT_MESSAGES,
HOSPITALISATION_APPOINTMENTS, FACT_OF_HOSPITALIZATION, UPLOADED_DOCUMENTS,
PIN_APPOINTMENTS, ACTIVE_PROACTIVE_CALLS, INSPECTIONS_WITH_PROACTIVE_CALLS
```

### 4.3 Посещения врача (осмотры)

```
GET /api/4/documents/inspections?ehrId=<ehr>&shortDateFilter=<period>
```

Версия `/3` — старая схема, `/4` — текущая. Это и есть «посещения врача»:
дата, врач, специальность, диагноз, жалобы, назначения по итогам приёма.

Ответ `/4` (проверено):

```json
{
  "count": 3,
  "documents": [
    {
      "documentType": "INSPECTION",
      "date": "2021-02-06T16:25:17",
      "document": {
        "documentId": "...", "date": "...", "title": "Осмотр специалиста",
        "organisation": "...", "doctorSpecialization": "...",
        "doctorName": "...", "forTMK": null
      },
      "call": null
    }
  ]
}
```

У `/3` и `/1` элементы плоские (без обёртки `document`).

### 4.4 Анализы

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/1/documents/analyzes?ehrId=&shortDateFilter=&includeCovidTests=YES` | список анализов |
| `GET` | `/api/1/analysis/assignments?ehrId=&dateFilter=` | назначения на анализы (новая CLS-схема) |
| `POST` | `/api/1/analysis/details` | детали: `{ehrId, resultsDocId:[], assignmentsDocId:[]}` |
| `POST` | `/api/1/analysis/results` | динамика: `{ehrId, testCodes:[], startDate, endDate, dateFilter}` |
| `GET` | `/api/1/analysis/charts?ehrId=&testCodes=...` | данные для графиков |

`includeCovidTests` — `YES` / `NO`.

Ответ списка (проверено): `{"count": 5, "documents": [{"documentId": "...",
"title": "...", "date": "...", "isCovid": false, "code": "..."}]}`.

### 4.5 Прививки

```
GET /api/3/vaccinations?ehrId=<ehr>&isImmunodiagEnabled=true
```

Ответ:

```json
{
  "doneList": [ { "...": "сделанные прививки" } ],
  "tubList":  [ { "...": "реакции Манту / Диаскинтест" } ],
  "scans":    [ { "packageId": 1, "status": "inProcess" } ],
  "count": 42
}
```

Статусы загруженных вручную пакетов:

```
GET /api/1/vaccinations/get-package-statuses?ehrId=&vaccinationPackageIds=1&vaccinationPackageIds=2
→ {"vaccinationPackageStatuses": [{"packageId": 1, "status": "success|inProcess|notExist"}]}
```

Справочники и доп. разделы: `/api/1/vacc/exemptions`, `/vacc/immunoglobulins`,
`/vacc/rabies`, `/vacc/card/order`.

### 4.6 Лекарственные назначения (рецепты)

| Метод | Путь |
|---|---|
| `GET` | `/api/2/receipt?ehrId=&shortDateFilter=&documentId=` |
| `GET` | `/api/3/receipt/details?ehrId=&prescriptionNumber=` |
| `GET` | `/api/1/receipt/orders?ehrId=` |
| `GET` | `/api/1/receipt/aggregators` |

Плюс «аптечка» (pillbox) — отдельный модуль с расписанием приёма лекарств:
`/api/1/pillbox/medications`, `/pillbox/schedule`, `/pillbox/taking-course` и др.

### 4.7 Направления

```
GET /api/1/self-appointment/referrals?ehrId=<ehr>
GET /api/1/documents/hospitalisation-referrals?ehrId=<ehr>&dateFilter=to_new
```

Первое — направления на приём/исследование, по которым можно записаться.
Второе — направления на госпитализацию.

### 4.8 Текущие записи к врачу

```
GET /api/1/self-appointment/appointments?ehrId=<ehr>
```

Возвращает действующие приёмы: дата/время, специальность, врач, поликлиника,
кабинет, `id` приёма (нужен для отмены/переноса).

---

## 5. Запись к врачу

### 5.1 Шаги

```
GET  /api/1/self-appointment/lpu?ehrId=              — поликлиники
GET  /api/1/self-appointment/specialities?ehrId=     — специальности
POST /api/1/self-appointment/purposes                — поводы обращения
POST /api/2/self-appointment/available-resources     — свободное время
POST /api/2/self-appointment/reservation             — бронь слота
POST /api/1/self-appointment/reservation-confirm     — подтверждение
```

### 5.2 Свободное время

```http
POST /api/2/self-appointment/available-resources
Content-Type: application/json

{
  "ehrId": "861c1228-34e6-44f6-ae39-49884ac72187",
  "specialityId": 12,          // опционально
  "lpuId": 10000300,           // опционально
  "referralId": 555,           // опционально
  "inquiryPurposeId": 3,       // опционально
  "withSchedule": true
}
```

Ответ (структура, которую разбирает UI):

```json
{
  "data": {
    "availableResources": [
      {
        "id": 98765,
        "muName": "ГБУЗ ГП № 5",
        "address": "ул. ...",
        "referralType": { "consultation": { "doctorFullName": "...", "doctorPosition": "..." } },
        "schedule": [
          {
            "date": "2026-11-12",
            "slots": [ {"startTime": "2026-11-12T09:00:00", "endTime": "2026-11-12T09:20:00", "isBusy": false} ],
            "complexResource": [ {"id": 111, "room": {"number": "12", "lpuId": 10000300}} ],
            "complexResourceSchedule": [ {"complexResourceId": 111, "slots": [ ... ]} ]
          }
        ]
      }
    ]
  }
}
```

Есть две схемы слотов: плоская (`schedule[].slots`) и «комплексная»
(`schedule[].complexResourceSchedule[].slots` с `complexResourceId`). Клиент
поддерживает обе.

### 5.3 Бронь и подтверждение

```http
POST /api/2/self-appointment/reservation
{
  "ehrId": "861c1228-34e6-44f6-ae39-49884ac72187",
  "availableResourceId": 98765,
  "complexResourceId": 0,
  "startTime": "2026-11-12T09:00:00",
  "endTime": "2026-11-12T09:20:00",
  "referralId": null,
  "receptionTypeId": null,
  "inquiryPurposeId": 3
}
→ { "reservationId": 424242, ... }
```

```http
POST /api/1/self-appointment/reservation-confirm
{
  "ehrId": "861c1228-34e6-44f6-ae39-49884ac72187",
  "reservationId": 424242,
  "availableResourceId": 98765,
  "specialityId": 12,
  "receptionTypeId": null,
  "inquiryPurposeId": 3
}
→ { "appointmentId": 777777, ... }
```

Отмена брони — `/api/1/self-appointment/reservation-cancel`,
отмена уже созданного приёма — `/api/1/self-appointment/cancel`
(`{"ehrId": ..., "appointmentId": ...}`).

Альтернативный «прямой» путь (используется при переносе/повторной записи):

```http
POST /api/2/self-appointment/appointment
{
  "ehrId": "861c1228-34e6-44f6-ae39-49884ac72187",
  "appointmentId": 0,
  "availableResourceId": 98765,
  "complexResourceId": 0,
  "startTime": "...",
  "endTime": "...",
  "receptionTypeId": null
}
```

### 5.3.1 Жалобы обязательны (`422 NoComplaint`)

`reservation-confirm` **не создаст запись**, пока не заполнены «актуальные
жалобы»: сервер отвечает

```
422 {"code": "NoComplaint", "message": "Актуальные жалобы отсутствуют"}
```

Это проверено на живом API: `reservation` проходит, `reservation-confirm`
падает. Порядок, который делает UI:

1. `POST /api/1/interview/create-dialog` `{ehrId, specialityId}`
   → `{code: "2UtvkT", complaintId: 2288885773}`;
2. `POST /api/1/self-appointment/interview/get-questions`
   `{ehrId, tagCodes: [], isDrzNeeded: true}` → дерево жалоб:
   `{"questions": [{"tagCode": 3, "tagTitle": "Головокружение",
   "syntheticTagCode": null, "questionsForTag": [...]}]}`;
   у вопросов есть `type` (`MULTIPLE_CHOICE`, `TEXT_ANSWER`, …), `answers[]`
   и ветвление через `nextQuestionCode` / `stepNumber` / `stepToFinish`;
3. `POST /api/1/self-appointment/interview/save-answers`
   `{ehrId, hash: code, complaintsJson: {tagSurvey: [...]}}`, где элемент:

   ```json
   {
     "tagCode": 3, "tagTitle": "Головокружение", "tagInputText": null,
     "childTagCode": null, "childTagTitle": null, "childTagInputText": null,
     "syntheticTagCode": null,
     "patientAnswer": [{
       "stepNo": 1, "questionCode": "30", "questionTitle": "...",
       "isPtv": false, "isDrz": false, "answerToDoctor": "* ...",
       "answerCode": "3001", "answerTitle": "...", "answerInputText": null,
       "answerNumeric": null, "answerBloodPressure": []
     }]
   }
   ```

4. только после этого повторять `reservation-confirm`.

В скрипте это реализовано интерактивно: `book --yes` при `NoComplaint`
предлагает выбрать жалобы и ответить на вопросы (`emias/interview.py`).
Проверить без реальных жалоб нельзя — ответы попадают в медкарту, поэтому
автотестами покрыт только разбор дерева, а не отправка.

### 5.4 Прочие виды записи

| Путь | Что |
|---|---|
| `/api/1/self-appointment/pin/*` | запись по направлению (PIN) |
| `/api/1/self-appointment/hospital/*` | запись в стационар |
| `/api/1/self-appointment/complex-reception/*` | комплексный приём |
| `/api/1/self-appointment/waiting-list/request` | лист ожидания |
| `/api/1/self-appointment/interview/*` | опросы перед приёмом |
| `/api/1/self-appointment/purposes` | поводы обращения |
| `/api/1/self-appointment/memos` | памятки к приёму |
| `/api/1/self-appointment/shift` | перенос приёма |

---

## 6. Полный список эндпоинтов

Список собран уже не по минифицированному бандлу, а по `main.js.map`
(production source map) — там лежат исходники TypeScript. Путь указан целиком,
вместе с версией; динамические сегменты — в фигурных скобках. Часть служебных
ручек (`/pillbox/*`, `/tmk/*`, `/onco/*`, `/free-food/*`) в скрипте не
используется, но приведена для полноты.

<details>
<summary>Развернуть</summary>

```
AUTH
POST   /api/auth/1/confirm
POST   /api/auth/1/guest/refresh
POST   /api/auth/1/login
POST   /api/auth/1/logout
POST   /api/auth/1/refresh
POST   /api/auth/1/resend

ПРОФИЛЬ И СПРАВОЧНИКИ
GET    /api/1/auth-logs
POST   /api/1/get-profile
GET    /api/1/profile/extended/address
PUT    /api/1/profile/extended/address
PUT    /api/1/profile/extended/address/manual
GET    /api/1/profile/extended/address/search
POST   /api/1/profile/extended/address/search
GET    /api/1/profile/extended/attachments
POST   /api/1/profile/extended/attachments
GET    /api/1/profile/extended/contacts
PUT    /api/1/profile/extended/contacts
GET    /api/1/profile/extended/main
GET    /api/1/profile/notification
POST   /api/1/profile/notification
GET    /api/1/profile/notifications
GET    /api/1/profile/notifications-check
PUT    /api/1/profile/picture
GET    /api/1/reference/dictionary
GET    /api/1/reference/moscow-metro-stations
GET    /api/2/profile

МЕДКАРТА — ДОКУМЕНТЫ
POST   /api/1/convert-html-to-pdf
GET    /api/1/document/covid-certificates/pdf
GET    /api/1/document/covid-certificates/view
GET    /api/1/document/dicom-request
POST   /api/1/document/laboratory-test
GET    /api/1/document/official-archive
GET    /api/1/document/officialPDF
GET    /api/1/document/pdf
GET    /api/1/document/qr
POST   /api/1/document/research/ctg/image
GET    /api/1/document/scan
GET    /api/1/documents/ambulance
GET    /api/1/documents/analyzes
GET    /api/1/documents/card026
GET    /api/1/documents/complex-print/document
POST   /api/1/documents/complex-print/request
GET    /api/1/documents/complex-print/status
GET    /api/1/documents/consilium
GET    /api/1/documents/covid-certificates
GET    /api/1/documents/epicrisis
GET    /api/1/documents/hospitalisation-referrals
GET    /api/1/documents/medical-certificates
GET    /api/1/documents/recommendations-for-exempt
GET    /api/1/documents/recruiting-committee
GET    /api/1/documents/research
GET    /api/1/documents/sections
PUT    /api/1/documents/sections/view
GET    /api/1/documents/uploads/{documentId}/content/{id}
GET    /api/1/ehr-pdf
POST   /api/1/ehr-pdf
GET    /api/1/onco/documents/basic-observation-plans
GET    /api/2/document/qr
GET    /api/2/document/research/ecg/image
GET    /api/2/document/research/ecg/pdf
GET    /api/2/documents/conclusion
GET    /api/2/documents/medical-certificates
GET    /api/3/document
GET    /api/3/documents/conclusions
GET    /api/3/documents/inspections
GET    /api/4/documents/inspections

АНАЛИЗЫ (CLS)
GET    /api/1/analysis/assignments
GET    /api/1/analysis/charts
POST   /api/1/analysis/details
POST   /api/1/analysis/results

ПРИВИВКИ
GET    /api/1/vacc/card/order
POST   /api/1/vacc/card/order
GET    /api/1/vacc/card/order/pdf
GET    /api/1/vacc/exemptions
GET    /api/1/vacc/immunoglobulins
GET    /api/1/vacc/rabies
GET    /api/1/vaccinations/get-package-statuses
PATCH  /api/1/vaccinations/package/{id}
DELETE /api/1/vaccinations/uploads
POST   /api/1/vaccinations/uploads
GET    /api/2/vacc/exemption
GET    /api/3/vaccinations

НАЗНАЧЕНИЯ / РЕЦЕПТЫ
GET    /api/1/receipt/aggregators
GET    /api/1/receipt/orders
POST   /api/1/receipt/pharmacies/rate
GET    /api/2/receipt
GET    /api/3/receipt/details

АПТЕЧКА (PILLBOX)
GET    /api/1/pillbox/directory/skip-reason
GET    /api/1/pillbox/directory/termination-reason
GET    /api/1/pillbox/drug-info
DELETE /api/1/pillbox/medication
GET    /api/1/pillbox/medication
POST   /api/1/pillbox/medication
POST   /api/1/pillbox/medication/end
GET    /api/1/pillbox/medications
POST   /api/1/pillbox/reaction
GET    /api/1/pillbox/regime
POST   /api/1/pillbox/regime
GET    /api/1/pillbox/schedule
POST   /api/1/pillbox/schedule
GET    /api/1/pillbox/schedule-calendar
GET    /api/1/pillbox/schedule-project
GET    /api/1/pillbox/search-drugs
GET    /api/1/pillbox/search-trade-names
GET    /api/1/pillbox/slots
POST   /api/1/pillbox/taking-adhoc
GET    /api/1/pillbox/taking-adhoc-history
POST   /api/1/pillbox/taking-course

ЗАПИСЬ К ВРАЧУ
POST   /api/1/interview/create-dialog
GET    /api/1/interview/get-complaint
GET    /api/1/interview/get-complaint-appointment
GET    /api/1/self-appointment/appointments
POST   /api/1/self-appointment/available-resources-for-li
POST   /api/1/self-appointment/available-resources/schedule
POST   /api/1/self-appointment/cancel
POST   /api/1/self-appointment/client-path/get-next-step
POST   /api/1/self-appointment/client-path/get-status
POST   /api/1/self-appointment/complex-reception/appointment
POST   /api/1/self-appointment/complex-reception/shift
POST   /api/1/self-appointment/get-restriction-message
POST   /api/1/self-appointment/hospital/appointment
GET    /api/1/self-appointment/hospital/appointments
POST   /api/1/self-appointment/hospital/available-resources
POST   /api/1/self-appointment/hospital/cancel
GET    /api/1/self-appointment/hospital/referrals
POST   /api/1/self-appointment/hospital/shift
GET    /api/1/self-appointment/imemo
GET    /api/1/self-appointment/interview/count
GET    /api/1/self-appointment/interview/get-evaluation-tags
GET    /api/1/self-appointment/interview/get-past-evaluation
POST   /api/1/self-appointment/interview/get-questions
GET    /api/1/self-appointment/interview/get-tags
GET    /api/1/self-appointment/interview/get-visits-for-evaluation
POST   /api/1/self-appointment/interview/notification-status
POST   /api/1/self-appointment/interview/save-answers
POST   /api/1/self-appointment/interview/save-evaluation
GET    /api/1/self-appointment/lpu
POST   /api/1/self-appointment/memos
POST   /api/1/self-appointment/pin/cancel
POST   /api/1/self-appointment/pin/get-available-resources-schedule
GET    /api/1/self-appointment/pin/lpu-list
GET    /api/1/self-appointment/pin/print-data
GET    /api/1/self-appointment/pin/reference-types
GET    /api/1/self-appointment/pin/registration-types
POST   /api/1/self-appointment/pin/shift
POST   /api/1/self-appointment/purposes
GET    /api/1/self-appointment/referrals
POST   /api/1/self-appointment/reservation
POST   /api/1/self-appointment/reservation-cancel
POST   /api/1/self-appointment/reservation-confirm
POST   /api/1/self-appointment/save-kvd-questionnarie
POST   /api/1/self-appointment/shift
POST   /api/1/self-appointment/slots-unavailability-feedback
GET    /api/1/self-appointment/specialities
GET    /api/1/self-appointment/survey/appointment-purpose
POST   /api/1/self-appointment/survey/get-tags
POST   /api/1/self-appointment/survey/interview-pack
POST   /api/1/self-appointment/survey/save-answer
POST   /api/1/self-appointment/waiting-list/request
GET    /api/1/self-appointment/waiting-list/request/check-availability
POST   /api/2/self-appointment/appointment
POST   /api/2/self-appointment/available-resources
POST   /api/2/self-appointment/available-resources/schedule
POST   /api/2/self-appointment/client-path/get-additional-survey
POST   /api/2/self-appointment/client-path/save-additional-survey
POST   /api/2/self-appointment/pin/appointment
GET    /api/2/self-appointment/pin/appointments
GET    /api/2/self-appointment/pin/reference-types
POST   /api/2/self-appointment/reservation

ДНЕВНИКИ, ОПРОСЫ, ПРОФИЛАКТИКА
GET    /api/1/ambulance-ecg/list
GET    /api/1/ambulance-ecg/pdf
GET    /api/1/cdd-appointment-messages
GET    /api/1/checkup/current
GET    /api/1/checkup/current/status
GET    /api/1/checkup/is-active-park
GET    /api/1/checkup/moscow/aging-profile
GET    /api/1/checkup/moscow/clinics-info
GET    /api/1/checkup/moscow/functional-tests
GET    /api/1/checkup/moscow/general-results
GET    /api/1/checkup/moscow/get-longevity-survey
GET    /api/1/checkup/moscow/longevity-survey-list
POST   /api/1/checkup/moscow/save-functional-test
POST   /api/1/checkup/moscow/save-longevity-survey
GET    /api/1/checkup/moscow/survey-results
POST   /api/1/checkup/risk-factors-request
GET    /api/1/child-medical-examinations
GET    /api/1/child-medical-examinations/history
GET    /api/1/child-medical-examinations/not-organized
GET    /api/1/child-medical-examinations/not-planned
GET    /api/1/child-medical-examinations/organized
GET    /api/1/diaries/charts
POST   /api/1/diaries/get-list
POST   /api/1/diaries/mfc/get-documents
GET    /api/1/diaries/mfc/list
GET    /api/1/dispensary-observation
GET    /api/1/dispensary-observation/past-events
GET    /api/1/dispensary-observation/plan
GET    /api/1/dispensary-observation/plan-with-past-events
GET    /api/1/dispensary-observation/status
GET    /api/1/embed/byKey
POST   /api/1/form/checkup-common
POST   /api/1/form/checkup-extended
POST   /api/1/form/checkup-manshealth
GET    /api/1/form/checkup-moscow
POST   /api/1/form/consent-for-tmk
POST   /api/1/form/save-checkup-moscow
GET    /api/1/forms/child-allergy-history
POST   /api/1/forms/child-allergy-history
GET    /api/1/forms/history/version/self-control
GET    /api/1/forms/history/version/self-control/last-date
POST   /api/1/forms/self-control
GET    /api/1/free-food/benefit
POST   /api/1/free-food/list/available
POST   /api/1/free-food/list/current
PATCH  /api/1/free-food/milk-shop
POST   /api/1/free-food/milk-shops
POST   /api/1/free-food/orders
POST   /api/1/free-food/qr-codes
PATCH  /api/1/free-food/schedule
POST   /api/1/free-food/schedule
GET    /api/1/newborn-reanimation-broadcast/active-streams
GET    /api/1/newborn-reanimation-broadcast/consent
GET    /api/1/newborn-reanimation-broadcast/key
GET    /api/1/onco
GET    /api/1/onco/check-for-active-program
POST   /api/1/onco/get-documents
GET    /api/1/onco/get-odn-combined-program
GET    /api/1/ordering-certificates/027/request/check-availability
GET    /api/1/ordering-certificates/check-section
POST   /api/1/ordering-certificates/create-order
GET    /api/1/smart-hospitalization
POST   /api/1/smart-hospitalization/feedback/choice
POST   /api/1/smart-hospitalization/feedback/rate
POST   /api/1/smart-hospitalization/referral-request
GET    /api/1/smart-hospitalization/short-referral-request
POST   /api/1/smart-hospitalization/survey
GET    /api/1/survey
GET    /api/1/survey/answers
POST   /api/1/survey/answers
GET    /api/1/survey/count-answer
POST   /api/1/survey/create-fact-process
GET    /api/1/survey/history
GET    /api/1/survey/interview
GET    /api/1/tmk/access-status
POST   /api/1/tmk/access-status
POST   /api/1/tmk/conference-status-request
GET    /api/1/tmk/initialize
POST   /api/1/tmk/log
GET    /api/1/tmk/proactive-consultation/check-tmk-availability
GET    /api/1/tmk/proactive-consultation/list
POST   /api/1/tmk/proactive-consultation/request
GET    /api/1/tmk/proactive-consultation/request/check-availability
GET    /api/1/tmk/settings
GET    /api/2/checkup/current
GET    /api/2/child-medical-examinations
GET    /api/2/diaries/charts
POST   /api/2/feedback
GET    /api/2/form/checkup
GET    /api/2/forms/history/version/child-allergy-history
GET    /api/2/forms/history/version/life
GET    /api/2/ordering-certificates/catalog
DELETE /api/3/diaries
GET    /api/3/diaries
POST   /api/3/diaries
PUT    /api/3/diaries
GET    /api/3/diaries/privacy
POST   /api/3/diaries/privacy
GET    /api/3/form/checkup
GET    /api/4/forms/life-anamnesis
POST   /api/4/forms/life-anamnesis

ПРОЧЕЕ
GET    /api/1/chatbot/activities
POST   /api/1/chatbot/activities
GET    /api/1/chatbot/cases
GET    /api/1/disability-form
POST   /api/1/onboarding-scenarios/get-details
POST   /api/1/onboarding-scenarios/get-list
POST   /api/1/onboarding-scenarios/get-list-updates
```

</details>

---

## 7. Ограничения

1. **API неофициальное и недокументированное.** Версии ручек (`/1`…`/4`)
   меняются без предупреждения; ответы могут отличаться от описанных.
   Всё, что помечено как «структура из UI», проверяется только запуском с
   реальным аккаунтом.
2. **Только чтение — безопасно; запись — изменяет реальные данные.**
   `book` и `cancel` создают/отменяют настоящую запись в поликлинике.
   Скрипт требует явный `--yes`.
3. **Персональные данные.** Токены и выгрузки — медицинская тайна. Файл
   сессии пишется с правами `0600`; не коммитьте `*.json` и папку `out/`.
4. **Не автоматизируйте вход паролем.** Пароль вводится только на
   `login.mos.ru`; скрипт принимает лишь одноразовый `code` и OTP.
5. **Уважайте нагрузку.** Не стоит опрашивать `available-resources` чаще,
   чем раз в несколько секунд — это живая запись в поликлинику.
6. **Условия использования.** Автоматический доступ к ЛК может нарушать
   правила mos.ru/ЕМИАС. Используйте на свой риск и только для своей учётной
   записи.

---

## 8. Как перепроверить изложенное

```bash
# 1. Конфиг портала
curl -s https://emias.info/aaa-cfg | head -c 2000

# 2. Конфиг медкарты
curl -s https://lk.emias.mos.ru/config/env.js

# 3. Source map медкарты — самый информативный артефакт
#    (путь к main.js меняется — берите актуальный из index.html)
curl -s https://lk.emias.mos.ru/ | grep -o 'static/js/main\.[a-f0-9]*\.chunk\.js'
curl -s https://lk.emias.mos.ru/static/js/main.<hash>.chunk.js.map -o main.js.map
python3 - <<'EOF'
import json, re
m = json.load(open('main.js.map'))
for src, code in zip(m['sources'], m['sourcesContent'] or []):
    for hit in re.finditer(r"endpoint:\s*['\"]([^'\"]+)", code or ''):
        print(src, hit.group(1))
EOF

# 4. Проверка, что ручка жива (без токена всегда 401, но не 404)
curl -i https://lk.emias.mos.ru/api/1/documents/sections
curl -i -X POST https://lk.emias.mos.ru/api/auth/1/login \
     -H 'Content-Type: application/json' -d '{"code":"x"}'
```
