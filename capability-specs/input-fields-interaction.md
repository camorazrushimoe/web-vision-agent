# Capability Spec: Input Fields Interaction

## Назначение

Дать агенту возможность видеть формы и поля ввода на странице, понимать их назначение и взаимодействовать с ними через поиск.

---

## Два типа форм

Агент классифицирует все найденные поля ввода на два типа:

| Тип | Описание | Примеры |
|---|---|---|
| `search` | Поисковые поля — предназначены для поиска по сайту | поиск по сайту, строка поиска товаров |
| `form` | Всё остальное — формы с несколькими полями | регистрация, авторизация, обратная связь, комментарий, заявка |

---

## Шаг 1 — Детектирование форм при анализе страницы

### Архитектурное решение (Риск 1)

`detect_input_fields()` реализуется как **отдельный LLM-вызов**, независимый от `analyze_page_structure()`. Существующий промпт `analyze_page_structure` не изменяется — это исключает любой риск сломать `/open` и `/click`.

В `open_page()` и `click_element()` оба вызова запускаются **параллельно**:

```python
structure, input_fields = await asyncio.gather(
    llm_client.analyze_page_structure(screenshot),
    llm_client.detect_input_fields(screenshot),
)
```

Если `detect_input_fields` вернул `None` — это не ошибка, просто в ответ добавляется `"input_fields": []`. Основной `structure` при этом не затрагивается. (Риск 5)

### Новый метод `detect_input_fields()` в `llm_client.py`

**Параметры:** температура `0.1`, `max_tokens=512` (небольшой объём — только поля, не вся страница).

**Промпт:**

```
You are analyzing a webpage screenshot to find input fields and forms.

Detect all visible input fields and forms. For each found element return:
- type: "search" (single search field) or "form" (multi-field form)
- label: short name in the page language
- description: precise visual description for locating this element —
  include: position on page (top/center/bottom), placeholder text if visible,
  color/style of the field, text of the nearby submit button or icon.

Example description: "white search field at the top center of the page,
placeholder 'Search...', with a magnifying glass icon button on the right"

If no input fields are visible, return: {"input_fields": []}

Return JSON only:
{
  "input_fields": [
    {"type": "search", "label": "...", "description": "..."},
    {"type": "form", "label": "...", "description": "..."}
  ]
}
```

### Зачем подробное описание (Риск 2)

Описание из `description` передаётся в UI-TARS-2B вместо generic-строки `"search input field"`. Это критично: UI-TARS обучен находить элементы по конкретным визуальным признакам. Чем точнее описание — тем выше вероятность правильного попадания.

Пример: вместо `"search input field"` передаём:
`"white search field at the top center of the page, placeholder 'Search...', with a magnifying glass icon button on the right"`

### Обработка `None` от `detect_input_fields` (Риск 5)

```python
structure, fields_result = await asyncio.gather(
    llm_client.analyze_page_structure(screenshot),
    llm_client.detect_input_fields(screenshot),
)

# structure=None — критическая ошибка, завершаем (как сейчас)
if structure is None:
    yield error_event(...)
    return

# fields_result=None — некритично, продолжаем с пустым списком
input_fields = (fields_result or {}).get("input_fields", [])
```

### Отображение пользователю

В SSE result добавляется поле `input_fields`:

```json
{
  "type": "result",
  "current_url": "...",
  "structure": {...},
  "summary": "...",
  "input_fields": [
    {
      "type": "search",
      "label": "site search",
      "description": "white search field at the top center, magnifying glass icon on the right"
    },
    {
      "type": "form",
      "label": "contact form",
      "description": "form in the center of the page with fields: name, phone, message, and a blue 'Send' button"
    }
  ]
}
```

Если `input_fields` пуст — поле всё равно присутствует в ответе как `[]`. Это валидный кейс.

---

## Шаг 2 — Работа с формой поиска

### Новый эндпоинт

```
POST /search
```

Эндпоинт обязательно проходит через `run_with_timeout()` с `acquire/release` — как все остальные операции в `api.py`. (Риск 8)

### Параметры запроса

```json
{
  "query": "текст запроса для поиска"
}
```

### Алгоритм выполнения

```
POST /search {"query": "ноутбуки"}
  │
  ├─ 0. Сохранить url_before = get_current_url()           ← до любых действий (Риск 7)
  │
  ├─ 1. take_screenshot()  →  screenshot_before
  │       сохраняем для сравнения после поиска
  │
  ├─ 2. detect_input_fields(screenshot_before)
  │       Gemma 4 — находит поле поиска и возвращает подробное описание
  │       если поле не найдено → вернуть {"error": "search_field_not_found"}
  │
  ├─ 3. find_element_coordinates(screenshot, description)
  │       UI-TARS-2B — получает description из шага 2 (не generic!)  (Риск 2)
  │       если not found → вернуть {"error": "search_field_not_found"}
  │
  ├─ 4. click_at(x, y)
  │       кликаем в поле поиска мышью
  │
  ├─ 5. press_key("ctrl+a")                                ← очищаем поле (Риск 3)
  │       выделяем весь текст в поле если там что-то было
  │
  ├─ 6. type_text(query)
  │       вводим текст с клавиатуры через xdotool type
  │
  ├─ 7. take_screenshot()  →  screenshot_typed             ← верификация ввода (Риск 3)
  │       скриншот чтобы убедиться что текст появился в поле
  │       (передаётся в SSE как progress — пользователь видит что происходит)
  │
  ├─ 8. find_element_coordinates(screenshot_typed, "submit button near search field")
  │       UI-TARS-2B ищет кнопку отправки поиска
  │       если found → click_at(x, y)
  │       если not found → press_key("Return")  (fallback)
  │
  ├─ 9. wait_for_page_load()                               ← ждём ПЕРЕД get_current_url (Риск 4)
  │       ждём стабилизации страницы
  │
  ├─ 10. get_current_url()  →  url_after                  ← только после wait (Риск 4)
  │       Ctrl+L → Escape не прерывает загрузку т.к. она уже завершена
  │
  ├─ 11. take_screenshot()  →  screenshot_after
  │       финальный скриншот результатов
  │
  ├─ 12. Определить result_type:                           (Риск 7)
  │       url_changed = (url_after != url_before)
  │       pixel_diff = pixel_difference(screenshot_before, screenshot_after)
  │       если url_changed → "page_reload"
  │       если not url_changed и pixel_diff > 10% → "content_updated"
  │       если pixel_diff < 10% → "no_change"
  │
  └─ 13. analyze_page_structure(screenshot_after)
          Gemma 4 анализирует результаты поиска
          → SSE result
```

### Анализ результата поиска

```json
{
  "type": "result",
  "search_result": {
    "query": "ноутбуки",
    "url_before": "https://example.com/",
    "url_after": "https://example.com/search?q=ноутбуки",
    "url_changed": true,
    "result_type": "page_reload",
    "pixel_diff_pct": 87.3,
    "summary": "страница перезагрузилась, показаны результаты поиска по запросу 'ноутбуки'"
  },
  "structure": {...},
  "input_fields": [...],
  "current_url": "https://example.com/search?q=ноутбуки"
}
```

`result_type` — одно из:
- `page_reload` — URL изменился, страница перезагрузилась
- `content_updated` — URL не изменился, но pixel_diff > 10% (AJAX-поиск без перезагрузки)
- `no_change` — ничего не изменилось, поиск возможно не сработал

### Fallback-сценарии (Риск 4)

| Ситуация | Действие |
|---|---|
| Поле поиска не найдено Gemma 4 | Вернуть `{"error": "search_field_not_found"}` |
| UI-TARS не нашёл координаты поля | Вернуть `{"error": "search_field_not_found"}` |
| Кнопка поиска не найдена | Нажать Enter вместо клика |
| `result_type = "no_change"` | Вернуть результат с пометкой — поиск возможно не сработал |
| Страница не стабилизировалась за таймаут | Продолжить, вернуть `result_type` по pixel_diff |

---

## Зависимости от существующего кода

| Что нужно | Где | Статус |
|---|---|---|
| `find_element_coordinates()` | `llm_client.py` | есть |
| `click_at()` | `browser_control.py` | есть |
| `type_text()` | `browser_control.py:77` | есть |
| `press_key()` | `browser_control.py:85` | есть (для Ctrl+A, Enter) |
| `wait_for_page_load()` | `browser_control.py` | есть |
| `get_current_url()` | `browser_control.py` | есть |
| `pixel_difference()` | `browser_control.py:172` | есть |
| `analyze_page_structure()` | `llm_client.py` | есть, не изменяем |
| `detect_input_fields()` | `llm_client.py` | новый метод |
| `asyncio.gather()` в open_page/click_element | `page_analyzer.py` | изменяем |
| `search_page()` flow | `page_analyzer.py` | новая функция |
| `POST /search` endpoint | `api.py` | новый эндпоинт |
| `run_with_timeout()` для /search | `api.py` | обязательно |

---

## Что НЕ входит в эту капабилити

- Заполнение форм типа `form` (регистрация, обратная связь) — только детектирование и информирование
- Работа с dropdown, checkbox, radio button — за рамками текущей спецификации
- Мульти-шаговые формы (wizard) — за рамками текущей спецификации
