# mandarin-app-fetch

Unified API server (FastAPI, port 8089) that scrapes Chinese social media posts
given a URL and returns structured JSON. It is the **social media scraping backend**
for Jun's openclaw test-two bot — the ozaiya plugin calls `http://host.docker.internal:8089/fetch?url=...`.

```
User (chat) → test-two bot → ozaiya plugin → mandarin-app-fetch (:8089)
                                                      ↓
                                            Playwright / Docker CDP → target site
```

## Endpoints

- `GET /fetch?url=...` — Scrape a social media post, return structured JSON
- `GET /search?platform=xueqiu&q=...` — Keyword search (Xueqiu only)
- `GET /extract?url=...` — LLM summary via Doubao or Yuanbao (for WeChat articles etc.)
- `POST /cookies/{service}` — Update cookies (raw string or Playwright JSON), hot-reloads live sessions
- `GET /cookies/status` — Cookie status for all services
- `POST /v1/chat/completions` — OpenAI-compatible Doubao chat via CDP
- `GET /v1/models` — List available Doubao models

## Supported Platforms

| Platform       | Method                                      |
|----------------|---------------------------------------------|
| 小红书 XHS     | Docker CDP → Playwright fallback            |
| 抖音 Douyin    | Playwright in-page fetch → Docker CDP       |
| B站 Bilibili   | Playwright → `__INITIAL_STATE__` parsing    |
| 微博 Weibo     | Playwright → mobile API (m.weibo.cn)        |
| 雪球 Xueqiu    | Playwright → same-origin fetch (WAF bypass) |
| 头条 Toutiao   | Playwright → web API → DOM fallback         |
| 闲鱼 Goofish   | Docker CDP (goofish-browser container)      |

## How It Works

1. Launches headless Chromium via Playwright (with anti-detection stealth patches)
2. Navigates to the target URL as a real browser
3. Calls the site's internal API from within the page context (same-origin bypasses CORS/WAF)
4. Parses response into a normalized `SocialMediaPost` schema
5. Per-platform rate limiting prevents abuse

Goofish uses a separate approach: a CDP extraction script (`goofish-cdp-extract.js`)
runs inside Jun's `goofish-browser` Docker container which has a logged-in Chrome session.

## Key Files

- `main.py` — FastAPI app, platform registry, rate limiting, all endpoints
- `cookie_manager.py` — Unified cookie storage, parsing, status, seed/migrate logic
- `url_parser.py` — URL pattern detection and ID extraction per platform
- `browser_manager.py` — Playwright browser/context lifecycle, cookie injection
- `platforms/*.py` — Per-platform scraper implementations
- `goofish-cdp-extract.js` — CDP script for Goofish (runs inside Docker container)
- `models.py` — `SocialMediaPost`, `Author`, `Comment` schemas
- `extractors/` — LLM-based content extractors (Doubao, Yuanbao)
- `doubao-cdp-chat.js` — CDP script for Doubao chat (runs inside Docker container)
- `llm_service/` — Doubao LLM service via CDP (Chrome DevTools Protocol)
  - `config.py` — Configuration (CDP container, model mapping, API key)
  - `provider.py` — Chat completion logic, OpenAI-compatible responses
  - `cdp_client.py` — Runs doubao-cdp-chat.js inside Docker via `docker exec`
  - `sse_utils.py` — SSE formatting helpers for OpenAI-compatible output
- `.env.example` — Template for required environment variables

## Jun's Infrastructure (192.168.1.181)

- SSH: `jun@192.168.1.181`
- Docker binary: `/Applications/Docker.app/Contents/Resources/bin/docker`
- **test-two bot**: agent (port 18820), browser (noVNC: 6090), desktop (55007)
  - Config: `~/.openclaw-test-two/openclaw.json`
  - Docker compose: `~/.openclaw-test-two/docker/docker-compose.yml`
  - Browser data: `~/openclaw-local/browser-data/test-two-chrome`
- **goofish bot**: agent (port 18840), browser (noVNC: 16090)
  - Config: `~/.openclaw-goofish/openclaw.json`
  - Browser data: `~/openclaw-local/browser-data/goofish-chrome`

## Cookie Management

All file-based cookies are stored uniformly in **Playwright JSON format** under
`~/.mandarin-app-fetch/{service}-cookies.json`, managed by `cookie_manager.py`.

- **Update cookies:** `POST /cookies/{service}` — accepts raw header string
  (`Content-Type: text/plain`) or Playwright JSON array (`application/json`).
  Hot-reloads into the live Playwright context for scraping platforms.
  Yuanbao reads fresh from file each request.
- **Check status:** `GET /cookies/status` — shows cookie presence, count, last
  update time, and source for all services.
- **Startup seeding:** `DOUBAO_COOKIE_1` env var is persisted to file on first
  startup only. After that, use the API to update.
- **Migration:** Old `yuanbao-cookies.txt` is auto-migrated to JSON format on
  first startup.
- **Out of scope:** Docker CDP cookies (Goofish) are live Chrome sessions managed
  via noVNC — not file-based.

## Development Notes

- Chrome DevTools Protocol rejects WebSocket with non-localhost Host headers.
  Browser containers need `--remote-allow-origins=*` flag for cross-container CDP access.
- **Never recreate browser containers without persistent volume mounts** — loses all
  logged-in sessions.
- Weibo "Sina Visitor System" = missing login session, not IP block. Works via
  same-origin fetch from a browser tab on weibo.com.
- **LLM service** uses CDP (Chrome DevTools Protocol) to call Doubao's chat API
  through a logged-in Docker Chrome session. The browser's Argus SDK handles
  `a_bogus`/`msToken` signing automatically. Requires a Docker container with
  Chrome logged into doubao.com (default: `test-two-browser`).
  Same approach as Goofish: `docker exec` runs a Node.js CDP script inside the container.
- Git remote: `git@github.com:tangojet/mandarin-app-fetch.git`
