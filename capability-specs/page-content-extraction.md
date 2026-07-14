# Capability Spec: Page Content Extraction

## Назначение

Дать агенту возможность анализировать контент страницы — понимать что именно находится на странице, какого типа этот контент, перечислять видимые элементы и читать текст с помощью Gemma 4.

---

## Концепция

Агент смотрит на скриншот страницы и отвечает на вопрос: **"что здесь?"** — не структура навигации, а именно содержимое основной области контента. Это отдельная операция от `analyze_page_structure`.

---

## Новый эндпоинт

```
POST /content
```

Эндпоинт обязательно проходит через `run_with_timeout()` с `acquire/release` — как все остальные операции в `api.py`. (Риск 8)

Без параметров — анализирует текущее состояние страницы (только видимый viewport).

Опционально:
```json
{
  "full_page": true
}
```
Если `full_page: true` — сначала прокрутить страницу (как в `/scan`), потом анализировать всё.

---

## Типы контента

Gemma 4 определяет тип контента страницы из фиксированного списка:

| Тип | Описание |
|---|---|
| `product_list` | Список/сетка карточек товаров |
| `product_detail` | Страница одного товара (фото, описание, кнопка купить) |
| `article` | Длинный текст, статья, новость |
| `forum_thread_list` | Список тредов, тем, постов на форуме |
| `forum_thread` | Конкретный тред с комментариями |
| `contact_page` | Страница контактов (адреса, телефоны, карта) |
| `landing` | Лендинг / промо-страница |
| `dashboard` | Панель управления, личный кабинет |
| `empty` | Пустая страница или недостаточно контента |
| `unknown` | Не удалось определить |

---

## Структура ответа

```json
{
  "content_type": "product_list",
  "content_summary": "страница каталога, показаны карточки ноутбуков",
  "items_count": 12,
  "items": [
    {
      "index": 1,
      "label": "Ноутбук ASUS VivoBook 15",
      "description": "цена 45 000 ₽, рейтинг 4.5"
    },
    {
      "index": 2,
      "label": "MacBook Air M2",
      "description": "цена 110 000 ₽, в наличии"
    }
  ],
  "text_summary": "страница показывает 12 товаров в категории 'Ноутбуки'. Первый товар — ASUS VivoBook от 45 000 ₽, последний — MacBook Pro от 180 000 ₽.",
  "clickable_elements": [
    "карточка товара №1 — Ноутбук ASUS VivoBook 15",
    "карточка товара №2 — MacBook Air M2",
    "кнопка 'Следующая страница'",
    "фильтр 'по цене'"
  ]
}
```

Если `content_type` — `article`, то `items` пустой, но `text_summary` содержит краткий пересказ текста.

Если `content_type` — `product_detail`:
```json
{
  "content_type": "product_detail",
  "content_summary": "страница товара — ноутбук MacBook Air M2",
  "items_count": 1,
  "items": [
    {
      "index": 1,
      "label": "MacBook Air M2",
      "description": "большое фото, цена 110 000 ₽, описание характеристик, кнопка 'Купить'"
    }
  ],
  "text_summary": "страница описывает MacBook Air M2: процессор M2, 8GB RAM, SSD 256GB, цена 110 000 ₽. Присутствует кнопка добавления в корзину.",
  "clickable_elements": [
    "кнопка 'Купить'",
    "кнопка 'В корзину'",
    "вкладка 'Характеристики'",
    "вкладка 'Отзывы'"
  ]
}
```

---

## Алгоритм выполнения

```
POST /content
  │
  ├─ 1. take_screenshot()
  │
  ├─ 2. (если full_page: true)
  │       scroll_and_collect_screenshots()
  │       ограничение: максимум 3 скриншота передаётся в Gemma 4  (Риск 6)
  │       (больше — риск таймаута или OOM на Raspberry Pi)
  │
  ├─ 3. analyze_page_content(screenshots)
  │       Gemma 4 — новый промпт, отдельный от analyze_page_structure
  │       если вернул None → вернуть error_event, не падать с AttributeError (Риск 5)
  │       → структурированный JSON ответ
  │
  └─ 4. SSE result → вернуть структуру
```

### Обработка `None` от `analyze_page_content` (Риск 5)

```python
analysis = await llm_client.analyze_page_content(screenshots)

if analysis is None:
    yield error_event(
        "analysis_failed",
        "LLM failed to analyze page content",
        current_url,
        f"Check if LLM server at {llm_client.LLM_URL} is responding",
    )
    return

# Только после проверки на None используем .get()
content_type = analysis.get("content_type", "unknown")
items = analysis.get("items", [])
```

### SSE-стрим

```
event: status
data: {"stage": "screenshot", "message": "taking screenshot"}

event: status
data: {"stage": "analyzing", "message": "analyzing page content with vision model"}

event: result
data: {"content_type": "product_list", "items_count": 12, ...}
```

---

## Промпт-инструкция для Gemma 4

Новый метод `analyze_page_content()` в `llm_client.py`.

**Параметры:** температура `0.1`, `max_tokens=2048`.

```
You are analyzing the main content area of a webpage screenshot.

Your task:
1. Identify the content type from this list:
   product_list, product_detail, article, forum_thread_list, forum_thread,
   contact_page, landing, dashboard, empty, unknown
2. Count and list visible content items (products, posts, articles, etc.). List up to 10 items.
3. For each item: short label and brief description (price, rating, date — whatever is visible)
4. Identify clickable elements in the content area
5. Write a short text summary (2-3 sentences) describing what you see,
   including any readable text on the page

Return JSON only:
{
  "content_type": "...",
  "content_summary": "...",
  "items_count": N,
  "items": [{"index": 1, "label": "...", "description": "..."}],
  "text_summary": "...",
  "clickable_elements": ["...", "..."]
}
```

### Ограничение на количество скриншотов (Риск 6)

При `full_page: true` передаём в Gemma 4 максимум **3 скриншота**.

Каждый скриншот 1920×1080 занимает ~2–3 MB в base64. Отправка 4+ скриншотов одним запросом на Raspberry Pi с LM Studio может вызвать:
- таймаут (`LLM_TIMEOUT=30`)
- OOM на стороне LM Studio

Лимит 3 скриншота уже применяется в существующем `analyze_full_page()` (`llm_client.py:166`) — применяем ту же логику.

---

## Связь с существующим /click

Когда `analyze_page_content` возвращает список элементов (например 10 карточек товаров), пользователь может кликнуть на конкретный элемент через уже существующий эндпоинт:

```bash
# Посмотреть контент
POST /content

# Кликнуть на найденный элемент по описанию
POST /click
{"target": "карточка товара MacBook Air M2"}
```

`/content` только анализирует и описывает. Навигация — через `/click`.

---

## Чтение текста страницы

Текст читает Gemma 4 с визуального скриншота — она видит текст как человек и пересказывает его в `text_summary`. Это не точный OCR, но достаточно для понимания смысла.

Ограничения:
- Мелкий шрифт (< 10px) может быть нечитаем
- Длинные статьи анализируются поэкранно (с `full_page: true`)
- Возвращается **пересказ**, а не дословное копирование текста

---

## Зависимости от существующего кода

| Что нужно | Где | Статус |
|---|---|---|
| `take_screenshot()` | `browser_control.py` | есть |
| `scroll_down()`, `scroll_to_top()` | `browser_control.py` | есть |
| `pixel_difference()` | `browser_control.py:172` | есть (для определения конца страницы) |
| `analyze_page_content()` | `llm_client.py` | новый метод |
| `content_page()` flow | `page_analyzer.py` | новая функция |
| `POST /content` endpoint | `api.py` | новый эндпоинт |
| `run_with_timeout()` для /content | `api.py` | обязательно (Риск 8) |

---

## Что НЕ входит в эту капабилити

- Клик по найденным элементам — это `/click`
- Заполнение форм — это Input Fields Interaction
- Сравнение контента между двумя страницами — за рамками текущей спецификации
- Точное побуквенное извлечение текста (OCR) — используется только пересказ Gemma 4
