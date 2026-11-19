# Today's Brief

A phone-first morning newspaper built from RSS and delivered as a single HTML
attachment. It tracks developing stories, lets readers compare coverage across
outlets, and keeps the briefing readable offline.

![Today's Brief wordmark](docs/todays-brief-card-1600.webp)

## Try it offline

<img src="docs/sample-phone.webp" width="230" align="right" alt="World section of the sample issue, showing the outlet framing controls">

No account or API key needed. The demo uses fictional stories and bundled
illustrations.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 demo.py
```

Open `_site/demo.html` in a browser. On a phone, use the browser rather than
relying on Telegram's attachment preview.

<br clear="right">

## How it works

1. **Collect and rank.** Python ingests RSS feeds, clusters repeat coverage, and
   uses DeepSeek to classify stories.
2. **Track and compare.** SQLite records distinct days for each story. Exact
   article links take precedence; ambiguous headline matches start fresh.
   Outlet labels describe editorial positioning, not individual articles.
3. **Package and deliver.** Plain HTML, CSS, and JavaScript ship as one file with
   the issue data and available assets embedded. Original articles and photos
   that couldn’t be embedded still need a connection.

A failed build preserves the last complete issue. Telegram delivery retries
failed parts without resending parts already acknowledged.

## Run a live issue

Requires Python 3.12+, Linux, Bash, `flock`, GNU `timeout`, and a funded DeepSeek
API key. Set `DEEPSEEK_API_KEY` in your environment or a private
`~/.config/todays-brief/env` file. For Telegram, also set `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`. Start a chat with the bot and run
`python3 telegram.py --find-chat` to find the chat ID.

```sh
./deliver.sh        # build the issue and send it if Telegram is configured
./install-timer.sh  # optional: schedule delivery at 07:00 local time
```

The timer reads the env file; keep it private (`chmod 600`). For a manual build,
export the variables first. For an optional front-page image, run
`pip install -r requirements-browser.txt` and `playwright install chromium`.

## Checks

```sh
pip install -r requirements-dev.txt
playwright install chromium
python3 -m unittest discover -v
```

The tests use fixtures and the offline demo; no live API calls are required.
