# 🌐 Web Vision Agent

> A self-contained sub-agent that browses the web the way a human does — using eyes, a mouse, and a keyboard. Runs locally. Costs nothing per request.

---

## What is this?

**Web Vision Agent** is a Docker container you install once on your machine (or a Raspberry Pi), and your AI agent can use it as a tool to browse the internet.

You send it a task. It opens a real browser, looks at the page, figures out what's there, and reports back. No scraping APIs. No paid services. Just vision and input emulation.

Think of it as giving your agent a pair of eyes and hands for the web.

---

### 🧠 Runs on local LLMs — completely free to use

There are no API calls to OpenAI, Anthropic, or any paid service. Web Vision Agent uses two small open-source models that run on your own hardware via [LM Studio](https://lmstudio.ai):

- **Gemma 4** — looks at screenshots and understands what's on the page
- **UI-TARS-2B** — finds exact pixel coordinates of any element you describe

Once set up, every browse, every search, every page read costs you **nothing**.

---

### 👁️ Sees pages like a human, not like a scraper

Most browser automation tools (Selenium, Playwright) work by reading the page's HTML code. Web Vision Agent does not touch the DOM at all.

Instead it:
1. Opens a real Chromium browser
2. Takes a screenshot
3. Sends that screenshot to a vision model
4. The model *looks at the image* and describes what it sees

This means it works on any website — including heavily JavaScript-rendered pages, single-page apps, and sites that actively block scrapers.

---

### 🖱️ Controls the browser like a real user

When the agent needs to click something or type text, it doesn't inject JavaScript. It moves the actual mouse cursor to the right pixel and physically clicks. It types through the keyboard.

Websites see a real human-like interaction. No bot detection triggers. No `document.querySelector`.

---

### 📦 One container, zero configuration headaches

Everything is packaged into a single Docker container:

- Virtual display (Xvfb)
- Real Chromium browser
- FastAPI server on port `8080`
- Optional VNC on port `5900` so you can watch what the agent is doing in real time

```bash
docker compose up --build
```

That's it. Your agent can now call `http://localhost:8080` to browse the web.

---

### 🍓 Tested on Raspberry Pi 5 (8GB)

The full stack runs on a Raspberry Pi 5 with 8GB RAM. LLMs are served from a separate machine on the local network via LM Studio. If you have a spare Pi, you have a dedicated web-browsing sub-agent running 24/7.

---

## How to use it with your agent

Web Vision Agent is designed to be a **sub-agent** — a black box your main agent calls when it needs to interact with a website.

Your agent sends a request → Web Vision Agent does the work → returns structured results.

```
Your Agent
    │
    ├── "open this URL and tell me what you see"
    ├── "search for laptops"
    ├── "what products are on this page?"
    └── "click the Sign In button"
         │
         ▼
  Web Vision Agent  :8080
         │
         ▼
  structured JSON result
```

---

## Core behavior

Every time the agent opens or navigates to a page, it automatically:

- **Sees the page** — takes a screenshot, sends it to Gemma 4
- **Understands the layout** — identifies navigation menus, content areas, forms
- **Dismisses popups** — cookie banners, modals, overlays are handled without you asking
- **Reports back** — returns a structured summary of what it found

This is the baseline. The agent always knows what's on the screen.

---

## Built-in Capabilities

On top of the core vision layer, Web Vision Agent has **capabilities** — purpose-built tools for interacting with specific parts of a page. You don't need to describe how to find a search box or what to do with it. The capability handles that internally.

```
┌─────────────────────────────────────────────────────────┐
│                  Web Vision Agent                        │
│                                                          │
│  Core: opens page · takes screenshot · reads layout      │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │                  Capabilities                     │   │
│  │                                                   │   │
│  │  🚫 Popup dismissal      🔍 Input field detection │   │
│  │  🔎 Search interaction   📄 Content extraction    │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### 🚫 Popup & overlay dismissal
**Trigger:** automatic on every page open and click

Detects cookie banners, modals, and overlays. Reads the text on the close button, finds it on screen, clicks it. Retries up to 3 times. You never have to think about popups.

---

### 🔍 Input field detection
**Trigger:** automatic on every page open and click

Every time the agent looks at a page, it also scans for input fields and classifies them:
- `search` — a search bar for the site
- `form` — a registration form, login, feedback form, etc.

This gets included in every response automatically, so your agent always knows what it can interact with.

---

### 🔎 Search interaction
**Trigger:** `POST /search {"query": "your search term"}`

The agent finds the search field on the current page (using the vision model's description to locate it precisely), clears any existing text, types your query, finds and clicks the search button — or presses Enter as a fallback. Then it waits for the page to settle and tells you what happened:

- `page_reload` — URL changed, classic search
- `content_updated` — URL stayed the same but the page changed (AJAX search)
- `no_change` — nothing happened, search may have failed

---

### 📄 Page content extraction
**Trigger:** `POST /content` or `POST /content {"full_page": true}`

Instead of just describing the page structure, this capability focuses on **what's actually there**. It identifies the content type and lists what it sees:

- 🛍️ E-commerce page → lists product names, prices, ratings (up to 10 items)
- 📰 Article → summarizes the text it reads from the screenshot
- 💬 Forum → lists thread titles and metadata
- 📋 Contact page → extracts addresses, phones, links

`full_page: true` scrolls through the entire page before analyzing.

---

## A simple example

Your agent wants to find the cheapest laptop on an e-commerce site:

```
1. POST /open   {"url": "https://shop.example.com"}
   → sees homepage, finds search bar, dismisses cookie banner automatically

2. POST /search {"query": "laptop"}
   → types "laptop", clicks search, page reloads with results

3. POST /content
   → returns: content_type=product_list, items=[{name, price}, ...]

4. POST /click  {"target": "cheapest laptop card"}
   → navigates to product page

5. POST /content
   → returns: content_type=product_detail, text_summary="MacBook Air M2, $999..."
```

Five calls. No HTML parsing. No CSS selectors. No JavaScript injection.

---

## Quick start

```bash
# Clone and start
git clone https://github.com/camorazrushimoe/web-vision-agent
docker compose up --build

# Your agent is now available at
curl http://localhost:8080/health
```

Edit `docker-compose.yml` to point `LLM_URL` and `GROUNDING_URL` to your LM Studio instances.

Full API documentation and development guide: [`app/README.md`](app/README.md)
