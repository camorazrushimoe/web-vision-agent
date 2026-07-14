# Web Vision Agent

A headless browser automation agent that uses computer vision and local LLMs instead of traditional DOM/JavaScript APIs. No Selenium, no Playwright — just a real Chromium browser, screenshots, and two vision models reasoning about what they see.

## How it works

The agent runs Chromium inside a virtual display (Xvfb), takes pixel-level screenshots, and interacts with pages through X11 input emulation (mouse clicks, keyboard input). Two local LLMs handle all the reasoning:

- **Gemma 4** — vision analysis: reads screenshots, understands page structure, detects popups, produces structured JSON summaries
- **UI-TARS-2B** — visual grounding: given a screenshot and a text description of an element (e.g. `"Accept cookies"`), returns its pixel coordinates

Both models are served locally via LM Studio and accessed through an OpenAI-compatible API.

## API

The agent exposes a FastAPI server on port `8080` with SSE streaming responses:

| Endpoint | Purpose |
|---|---|
| `POST /open` | Navigate to a URL, dismiss popups, analyze page structure |
| `POST /click` | Find a named element by description, click it, re-analyze |
| `POST /scan` | Scroll through the full page and produce a complete structural analysis |
| `POST /search` | Find the search field, type a query, submit, analyze results |
| `POST /content` | Analyze the main content area (products, articles, forum, etc.) |
| `GET /screenshot` | Returns the current screenshot as a PNG |
| `GET /state` | Current URL, busy/idle status, last analysis result |
| `GET /health` | Browser liveness check |

---

## Capabilities

Each capability is triggered by a specific API endpoint. The diagram below shows all capabilities, their trigger commands, and how they connect to each other.

```
 External agent / curl
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                        API  :8080                               │
│                                                                 │
│  POST /open ──────────► [popup-dismissal] ──► [input-detect]   │
│                              │                      │          │
│  POST /click ─────────► [popup-dismissal] ──► [input-detect]   │
│                              │                                  │
│  POST /scan ──────────► (full page scroll + structure)         │
│                                                                 │
│  POST /search ────────────────────────────► [search-form]      │
│                                                                 │
│  POST /content ───────────────────────────► [content-extract]  │
└─────────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
  [popup-dismissal]    [input-detect]      [content-extract]
  dismiss cookie        find & classify     understand what's
  banners, modals,      input fields and    on the page:
  overlays              forms on page       products, article,
                                            forum, etc.
         │                    │
         ▼                    ▼
    Gemma 4 +           Gemma 4 detects,
    UI-TARS-2B          UI-TARS-2B locates
    (detect + click)    (then POST /search
                         types & submits)
```

**Capability status:**

| Capability | Trigger | Status |
|---|---|---|
| Popup & overlay dismissal | auto on `/open`, `/click` | live |
| Input fields detection | auto on `/open`, `/click` | live |
| Search form interaction | `POST /search` | live |
| Page content extraction | `POST /content` | live |

---

### Popup & overlay dismissal

Detects and dismisses cookie banners, modals, and overlays automatically. Gemma 4 identifies whether a popup is present and reads the text on the close button; UI-TARS-2B then locates that button's pixel coordinates and clicks it. Runs up to 3 retry attempts after each navigation.

**Trigger:** automatic on every `/open` and `/click`  
**Source:** `page_analyzer.py` → `dismiss_popups()`

---

### Input fields interaction

Detects and classifies input fields visible on the page into two types: `search` (site search bar) and `form` (registration, login, feedback, etc.). Detection runs automatically alongside page structure analysis and adds an `input_fields` section to the response. Search forms can also be interacted with via `POST /search`.

**Trigger:** automatic on `/open` and `/click` (detection) · `POST /search` (interaction)  
**Spec:** `../capability-specs/input-fields-interaction.md`

---

### Page content extraction

Analyzes the main content area of the page — identifies content type (product list, article, forum, etc.), lists up to 10 visible items with labels and descriptions, reads and summarizes page text, and returns a list of clickable elements. Clicking any found element is done via the existing `POST /click`.

**Trigger:** `POST /content`  
**Spec:** `../capability-specs/page-content-extraction.md`

---

## Running

```bash
# Build and start
docker compose up --build

# Navigate to a page
curl -X POST http://localhost:8080/open \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'

# Click an element
curl -X POST http://localhost:8080/click \
  -H "Content-Type: application/json" \
  -d '{"target": "Sign in button"}'

# Full page scan
curl -X POST http://localhost:8080/scan

# Search on the current page
curl -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query": "laptop"}'

# Analyze page content (current viewport)
curl -X POST http://localhost:8080/content

# Analyze full page content (scrolls through entire page)
curl -X POST http://localhost:8080/content \
  -H "Content-Type: application/json" \
  -d '{"full_page": true}'
```

## Configuration

All parameters are set via environment variables in `docker-compose.yml`:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_URL` | `192.168.31.195:1234` | Gemma 4 server address |
| `LLM_MODEL` | `gemma-4-e4b-it` | Vision model name |
| `GROUNDING_URL` | `192.168.31.195:1234` | UI-TARS server address |
| `GROUNDING_MODEL` | `ui-tars-2b-sft` | Grounding model name |
| `VNC_ENABLED` | `true` | Enable VNC on port 5900 for observation |
| `PAGE_LOAD_TIMEOUT` | `12` | Seconds to wait for page to stabilize |
| `MAX_POPUP_ATTEMPTS` | `3` | Popup dismiss retry count |
| `MAX_SCROLL_SECTIONS` | `4` | Pages to scroll during scan |

---

## Development & Testing

Tests live in `../tests/` and run locally without Docker, Xvfb, or real LLM servers.
All LLM calls and browser interactions are replaced with mocks.

### Setup (once)

```bash
# from the repo root (web-vision-agent/)
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

### Run tests

```bash
.venv/bin/pytest -v
```

Expected output: **41 passed**.

### Project structure

```
web-vision-agent/
├── app/                        # production code
│   ├── api.py                  # FastAPI endpoints
│   ├── page_analyzer.py        # high-level flows (open, click, scan, search, content)
│   ├── llm_client.py           # Gemma 4 + UI-TARS-2B calls
│   ├── browser_control.py      # X11 mouse/keyboard/screenshot primitives
│   └── entrypoint.py           # process manager (Xvfb, Chromium, uvicorn)
├── tests/
│   ├── conftest.py             # shared fixtures: mock_browser, mock_llm
│   ├── test_llm_client.py      # JSON parsing, detect_input_fields, analyze_page_content
│   ├── test_open_click.py      # open_page, click_element flows
│   ├── test_search_page.py     # search_page: result_type, fallbacks, error cases
│   └── test_content_page.py    # content_page: scroll, partial LLM response
├── capability-specs/           # technical specs for each capability
├── pytest.ini                  # asyncio_mode = auto, testpaths = tests
├── requirements-dev.txt        # test dependencies (pytest, Pillow, httpx, etc.)
├── requirements.txt            # production dependencies (inside Docker)
├── docker-compose.yml
└── Dockerfile
```

### What the tests cover

| File | What's tested |
|---|---|
| `test_llm_client.py` | `_parse_json_response` (clean JSON, markdown blocks, broken input), `detect_input_fields` (happy path, LLM returns None, malformed JSON, missing key), `analyze_page_content` (screenshot cap at 3, empty list) |
| `test_open_click.py` | `open_page` and `click_element` happy paths, `input_fields` always present in result, graceful handling of `None` from either LLM call |
| `test_search_page.py` | `result_type` logic (page_reload / content_updated / no_change), `url_before` captured before any browser action, fallback to Enter when submit button not found, all error paths |
| `test_content_page.py` | `full_page=False` (single screenshot), `full_page=True` (scroll + early stop at page end), scroll-to-top after scan, partial LLM response with missing keys |
