# mandarin-app-fetch

Unified API server for scraping Chinese social media posts. Given a URL, returns structured JSON with post content, author info, media, and comments.

Built with FastAPI + Playwright headless browser with anti-detection stealth patches.

## Supported Platforms

| Platform | Chinese Name | Method |
|----------|-------------|--------|
| XHS | 小红书 | Docker CDP / Playwright |
| Douyin | 抖音 | Playwright in-page fetch / Docker CDP |
| Bilibili | B站 | Playwright + `__INITIAL_STATE__` parsing |
| Weibo | 微博 | Playwright + mobile API |
| Xueqiu | 雪球 | Playwright + same-origin fetch (WAF bypass) |
| Toutiao | 头条 | Playwright + web API / DOM fallback |
| Goofish | ��鱼 | Docker CDP (logged-in Chrome session) |

## Quick Start

```bash
# Clone
git clone git@github.com:tangojet/mandarin-app-fetch.git
cd mandarin-app-fetch

# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Configure (optional — needed for Doubao LLM endpoints)
cp .env.example .env
# Edit .env with your Doubao cookie + device fingerprint

# Run
./run.sh
# Server starts on http://localhost:8089
```

## API Endpoints

### Scraping

```bash
# Fetch a post
curl "http://localhost:8089/fetch?url=https://www.xiaohongshu.com/explore/..."

# Search (Xueqiu only)
curl "http://localhost:8089/search?platform=xueqiu&q=tesla"

# LLM summary (WeChat articles via Yuanbao, others via Doubao)
curl "http://localhost:8089/extract?url=https://mp.weixin.qq.com/s/..."
```

### Cookie Management

```bash
# Check cookie status for all services
curl http://localhost:8089/cookies/status

# Update cookies — paste raw Cookie header from DevTools
curl -X POST -H "Content-Type: text/plain" \
  -d 'sessionid=abc123; csrf_token=xyz' \
  http://localhost:8089/cookies/xhs

# Or use Playwright JSON array format
curl -X POST -H "Content-Type: application/json" \
  -d '[{"name":"sid","value":"abc","domain":".xhs.com","path":"/"}]' \
  http://localhost:8089/cookies/xhs
```

Cookies are stored in `~/.mandarin-app-fetch/{service}-cookies.json` and hot-reloaded into live browser sessions without restart.

### Doubao LLM (OpenAI-compatible)

```bash
# Chat completion
curl -X POST http://localhost:8089/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-pro-chat","messages":[{"role":"user","content":"hello"}]}'

# List models
curl http://localhost:8089/v1/models
```

### Utility

```bash
curl http://localhost:8089/health        # Health check
curl http://localhost:8089/platforms      # List supported platforms
curl http://localhost:8089/rate-limits    # Current rate limit status
```

## How It Works

1. Receives a URL via the `/fetch` endpoint
2. Detects the platform from the URL pattern
3. Launches headless Chromium with stealth patches (anti-detection)
4. Navigates to the target URL as a real browser
5. Calls the site's internal API from within the page context (same-origin bypasses CORS/WAF)
6. Parses the response into a normalized `SocialMediaPost` schema
7. Returns structured JSON with content, author, media URLs, and comments

## Project Structure

```
main.py                  # FastAPI app, endpoints, rate limiting
cookie_manager.py        # Unified cookie storage and management
browser_manager.py       # Playwright browser lifecycle
url_parser.py            # URL pattern detection per platform
models.py                # SocialMediaPost, Author, Comment schemas
platforms/               # Per-platform scraper implementations
  xhs.py, douyin.py, bilibili.py, weibo.py,
  xueqiu.py, toutiao.py, goofish.py
extractors/              # LLM-based content extractors
  doubao.py, yuanbao.py
doubao_service/          # Integrated Doubao LLM service
  config.py, provider.py, playwright_mgr.py,
  sessions.py, sse_utils.py
```

## Configuration

Copy `.env.example` to `.env` and fill in values. The Doubao LLM endpoints require:

- `DOUBAO_COOKIE_1` — Cookie string from doubao.com (used for initial seeding only; update via API after)
- `DOUBAO_DEVICE_ID`, `DOUBAO_FP`, `DOUBAO_TEA_UUID`, `DOUBAO_WEB_ID` — Device fingerprint from a real request's URL parameters

Scraping platforms work without configuration but benefit from logged-in cookies for accessing restricted content.

## License

Private repository.
