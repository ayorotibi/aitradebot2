# AI Trading Bot — Alpaca + Web Dashboard

A standalone, cloud-deployable trading bot built around the strategy
described in Humbled Trader's "AI Trading Bot with Claude + IBKR" article
(S&P 500 premarket gap scan → momentum entry → stop-loss/take-profit exit),
re-architected to run unattended - on Render or a VPS - with a
password-protected web dashboard, on the Alpaca account you already have.

**Read this whole file before deploying, especially "Sharing one Alpaca
account with your other bot" and "Safety model."**

## What's here

```
common/       shared config, Postgres access layer, the baseline rule schema
bot/          the trading engine: scanner, strategy, risk, executor, scheduler
dashboard/    FastAPI + vanilla JS web dashboard (activity feed + rule editor)
docker-compose.yml   local dev: bot + dashboard + a bundled Postgres
render.yaml   Render Blueprint: the same two services + a managed Postgres
.env.example  copy to .env and fill in
```

There's no `ib-gateway/` service here on purpose — Alpaca's API is plain
REST/HTTPS, so there's no headless desktop-app container, no VNC, no 2FA
login dance to manage.

State lives in **Postgres**, not a local file — see "Why Postgres, not
SQLite" below. The bot and dashboard are two independent processes that
share nothing except that database, which is exactly what lets them run as
two separate Render services (see "Deploying to Render").

## Why Alpaca instead of Interactive Brokers

You already have an Alpaca account and found IBKR's registration process
(identity verification, funding, sometimes multi-day approval, plus TWS/
Gateway's own headless-login friction) too much overhead to repeat. Alpaca
solves that: generating a new API key pair for this bot is a 30-second
self-serve action in your existing account's dashboard — no new brokerage
registration at all.

The trade-off: **on live trading, this bot shares one account with your
other bot.** That introduces a real risk (one bot interfering with the
other's positions or capital) that the original two-broker design avoided
entirely by using a separate IBKR account. The rest of this README, and
several pieces of the code, exist specifically to manage that risk rather
than ignore it.

Paper trading is a different story: **Alpaca allows up to 3 separate paper
accounts per login**, each fully independent. Use a second one exclusively
for this bot (see "Deploying" below) and testing is genuinely isolated —
no shared positions, nothing for the conflict-check code to even need to
catch. That isolation currently stops at paper trading, though - live
accounts are still one per account type per login as of this writing.
Alpaca has a live "sub-accounts" feature in beta (expected sometime in
2026) that would eventually extend the same separation to live trading -
worth checking on before you go live, since it could remove the
shared-account trade-off entirely.

## Sharing one Alpaca account with your other bot (live trading)

This section applies once you go live - see "Deploying" below for why
paper testing doesn't need it. Alpaca's own support forum is direct about
this: a single live account has no built-in sub-account isolation between
strategies, and you can't hold simultaneous long and short positions in
the same symbol in one account. Three safeguards are built into this bot
to make sharing safe:

1. **Every order is tagged.** `bot/executor.py` gives every order a
   `client_order_id` starting with `BOT_ORDER_TAG` (default
   `aitradingbot`), so this bot's activity is always distinguishable from
   your other bot's in Alpaca's own order history — the tagging approach
   Alpaca's support recommends for exactly this situation.
2. **This bot never trusts "do I have a position here" from the broker
   alone.** It keeps its own record of what it opened (`common/db.py`).
   Before entering a new symbol, `bot/executor.py` checks whether Alpaca
   already shows a position there; if so, and this bot's own database
   didn't open it, that position belongs to your other bot, and this bot
   refuses to touch it — logged clearly on the dashboard's Logs tab. When
   closing a position (stop-loss/take-profit aside, which Alpaca manages
   directly per-order), it closes exactly the share count this bot's own
   records show, via a plain order — never Alpaca's "close entire
   position for this symbol" endpoint, which would also liquidate the
   other bot's shares if you both hold the same ticker.
3. **Capital allocation cap.** The Rules tab has a "Capital allocation"
   field (`rules.risk.capital_allocation_usd`). Set it to a dollar figure
   and this bot will never have more than that much deployed at once,
   position-sized as a % of *that* number rather than your full account
   equity. Leave it blank and this bot will size positions off the whole
   account's equity, which is very likely not what you want on a shared
   account — set it before you go live.

None of this eliminates the shared-account risk entirely (a hard crash
mid-order, an API outage, or a bug could still cause a rare edge case) —
it reduces it to roughly the same level of care you'd apply running two
independent scripts against the same account by hand.

## What's different from the original three articles, and why

| Original approach | Here | Why |
|---|---|---|
| TradingView Desktop app driven via a remote-debugging port (Part 1) | Dropped entirely. Signals come from Alpaca's market data API (premarket gap scan + breakout bars) | Automating TradingView's Electron app isn't a supported API and doesn't run headless in the cloud without a fragile virtual display |
| Interactive Brokers (TWS/Gateway, Part 2) | Alpaca REST API | No new account registration, no headless-login/2FA container to babysit — see above for the trade-off this introduces |
| Windows Task Scheduler, 11 jobs | A single time-window loop inside the bot process (`bot/scheduler.py`) | Portable to any Linux host/container; no OS-specific scheduler |
| Claude Code CLI as the live runtime | Claude Code is a dev tool for *writing* this code (as it was here); the deployed bot is plain Python with no LLM in the hot path | Claude Code is built for interactive development, not for sitting unattended in a cron loop |
| Local files (`trades.csv`, `open_positions.json`) | Postgres, shared by both bot and dashboard over the network | Gives the dashboard something to query live, and lets the two run as independent, separately-deployable services (e.g. on Render) rather than needing a shared disk |
| Rules in a static `rules.json` you hand-edit | Rules stored in the DB, edited from the dashboard, hot-reloaded every cycle | That's the control panel you asked for |

The underlying strategy logic (S&P 500 universe, gap % filter, position
sizing %, stop-loss/take-profit %, max positions) is the same baseline as
the article — see `common/rules_schema.py` for the exact defaults, all
editable from the dashboard's Rules tab.

## Safety model

This places real orders through a real brokerage account once you flip it
to live mode. Several deliberate layers exist so that doesn't happen by
accident:

1. **Paper by default.** `rules.mode` starts as `"paper"`.
2. **A second, server-side gate.** Even if `rules.mode` is set to `"live"`
   from the dashboard, the bot only ever connects with live API keys and
   places live orders if `ALLOW_LIVE_TRADING=true` is also set in `.env`
   on the host (`common/config.py`'s `effective_mode`) — a change the
   dashboard cannot make, on purpose. You have to deliberately edit the
   server and restart the bot container to arm live trading. Until you
   do, the bot silently keeps trading on paper even if the dashboard says
   "Live."
3. **A confirmation string.** The dashboard's "Switch to Live Mode" button
   requires a browser confirmation and sends `{"confirm": "CONFIRM"}` —
   not just a toggle click.
4. **A kill switch**, manual (dashboard button) or automatic (trips itself
   when `max_daily_loss_pct` of this bot's *allocated capital* is
   breached — `bot/risk.py`). While engaged, the bot keeps scanning and
   logging but places no new orders.
5. **Max open positions, per-trade position sizing, and the capital
   allocation cap** are enforced every cycle, not just at signal time.
6. **The symbol-conflict check** described above, specific to sharing one
   account with another bot.

None of this is a substitute for watching the paper account behave the way
you expect, for a while, before ever touching the live gate.

## Why Postgres, not SQLite

Earlier versions of this project used a single SQLite file on a shared
Docker volume, which works fine as long as the bot and dashboard run on
the same machine. It stops working the moment they're two separate
services with no shared disk - which is exactly the case on Render: a
persistent disk there is attachable to only one service, full stop
("you can't access a service's disk from any other service," per Render's
own docs). Postgres solves this the way it's meant to be solved - a real
network-reachable database both services connect to - and it costs
nothing in code complexity, since `common/db.py` exposes the same
functions either way. `docker-compose.yml` bundles a local Postgres
container for VPS/local deployment so the architecture is identical in
both places; you're not testing against one database engine and deploying
against another.

## Deploying

Two paths, pick one. **Render** (below) is a managed platform - no server
to patch, TLS and process supervision handled for you, deploys on every
git push, at roughly the same monthly cost as a VPS. The **VPS** path
gives you a full Linux box if you'd rather have that level of control.

### Deploying to Render

1. **Push this project to a GitHub repo.** It's already a git repository
   with everything committed (`git log` shows one commit) - you just need
   to point it at GitHub:
   ```bash
   # On github.com: create a new EMPTY repository (no README/.gitignore/license)
   # then, from this project's folder:
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```
2. **Generate your dashboard password hash now, before you start the
   Render setup** - you'll need to paste it in during step 4. Run this
   somewhere you have Python (your own machine is fine - this never
   leaves it):
   ```bash
   python3 -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
   ```
   Save that output string; that's `DASHBOARD_PASSWORD_HASH`, not your
   plain password.
3. **In Render:** New → Blueprint → connect the GitHub repo you just
   pushed. Render reads `render.yaml` and shows you three resources it's
   about to create: a Postgres database (`aitradingbot-db`), a Background
   Worker (`aitradingbot-bot`), and a Web Service (`aitradingbot-dashboard`).
4. **Fill in the environment variables Render prompts for** (these are
   marked `sync: false` in `render.yaml`, meaning Render asks for them
   rather than storing them in the file):
   - On the bot worker: `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_SECRET_KEY`
     (your dedicated paper account's keys), and `TELEGRAM_BOT_TOKEN`/
     `TELEGRAM_CHAT_ID` if you want alerts. Leave `ALPACA_LIVE_API_KEY`/
     `ALPACA_LIVE_SECRET_KEY` blank for now.
   - On the dashboard: `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD_HASH`
     (the hash from step 2, not your plain password).
   - `DATABASE_URL` on both, and `DASHBOARD_SECRET_KEY`, are filled in
     automatically - you won't see prompts for those.
5. **Click Deploy.** Render builds both Docker images and provisions the
   database. First build typically takes a few minutes; watch each
   service's Logs tab in the Render dashboard.
6. **Open the dashboard.** Render gives the web service a URL like
   `https://aitradingbot-dashboard.onrender.com` (find it on the service's
   page). Log in, and set a **capital allocation** on the Rules tab before
   anything else.
7. **Watch it run.** The worker's Logs tab is your Activity/Scans tab's
   raw feed in real time - useful for the first few sessions especially.
8. **Cost check:** as configured, this is a paid deployment (`0.5c-512mb`
   on both worker and web service, plus the cheapest paid Postgres) since
   workers have no free tier and free Postgres expires after 30 days -
   roughly $19-20/mo total. To trim it, you can edit the dashboard
   service's plan to `free` in `render.yaml` (it'll sleep after 15 min
   idle and take ~1 min to wake on your next visit - fine for occasional
   checking, less fine if you want it always instantly responsive).
9. **Going live later:** add `ALPACA_LIVE_API_KEY`/`ALPACA_LIVE_SECRET_KEY`
   and set `ALLOW_LIVE_TRADING=true` on the bot worker's environment
   variables in the Render dashboard, then manually redeploy that service
   for the change to take effect. Use the dashboard's Live Mode switch
   deliberately, as described in "Safety model" above.
10. **Future updates:** `git push` to the branch Render is watching
    triggers an automatic redeploy of both services.

### Deploying to a VPS

1. **Get a small always-on VPS.** A $5–20/mo box (DigitalOcean, Linode,
   Lightsail, a small EC2 instance) with Docker and Docker Compose
   installed is enough — this is much lighter than the IBKR version since
   there's no gateway container to keep logged in.
2. **Create a dedicated paper account for this bot.** Alpaca allows up to
   3 paper accounts per login (Account menu → paper account switcher →
   create a new one) - use a second one exclusively for this bot rather
   than the same paper account your other bot already uses. That gives
   you a genuinely isolated test: separate positions, separate P&L,
   nothing to reconcile against the other bot. Generate this new paper
   account's API key pair (app.alpaca.markets → API Keys, with that
   account selected) for `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_SECRET_KEY`.
   Later, when you're ready for live, generate a Live key pair the normal
   way - live accounts don't have the same multi-account allowance (see
   "Sharing one Alpaca account" above), so that step still shares your
   existing live account.
3. Copy this whole project to the VPS, then:
   ```bash
   cp .env.example .env
   # edit .env: ALPACA_PAPER_API_KEY/SECRET, DASHBOARD_USERNAME,
   # DASHBOARD_PASSWORD_HASH (see the comment in .env.example for how to
   # generate it), DASHBOARD_SECRET_KEY, TELEGRAM_* if wanted.
   # Leave ALLOW_LIVE_TRADING=false for now.

   docker compose up -d --build
   ```
4. Open `http://<your-vps-ip>:8080`, log in, and set a **capital
   allocation** on the Rules tab before doing anything else — this is the
   one setting that most directly protects your other bot's capital.
5. Watch the Activity/Scans/Logs tabs during a market session. Adjust
   rules from the Rules tab as you learn how it behaves. Look specifically
   for any "skipping to avoid interfering" log lines — that's the
   conflict check doing its job, and it's worth understanding *why* it
   fired each time.
6. **Put the dashboard behind HTTPS and a reverse proxy** (Caddy or nginx
   with Let's Encrypt is the simplest route) before you rely on it from
   outside a private network or VPN — right now `docker-compose.yml`
   exposes port 8080 directly, protected only by the login, which is fine
   over a VPN/SSH tunnel but not ideal directly on the public internet
   over plain HTTP.
7. Only after you're satisfied with paper behavior: generate live API
   keys, set `ALPACA_LIVE_API_KEY`/`ALPACA_LIVE_SECRET_KEY` and
   `ALLOW_LIVE_TRADING=true` in `.env`, restart
   (`docker compose up -d`), and use the dashboard's Live Mode switch
   deliberately.

## Pattern Day Trader protection

This baseline strategy (gap-and-go entries, same-day exits via
stop-loss/take-profit) can rack up day trades quickly. In a margin account
under $25,000 equity, US regulations limit you to 3 day trades per rolling
5 business days (the PDT rule) before the account gets flagged and
restricted — and because this account is **shared**, that restriction
would hit your other bot too, not just this one.

`bot/risk.py`'s `under_day_trade_limit()` reads `daytrade_count` and
`pattern_day_trader` directly from Alpaca's account object (it already
tracks both account-wide) and refuses to open new positions once the
account is at 3 day trades with equity under $25k, or already flagged.
This isn't a dashboard toggle — it's a regulatory guardrail, not a
strategy preference, so it can't be switched off from the UI. The
dashboard's header shows a "Day trades (5d): N/3" pill whenever equity is
under $25k so you can see it coming.

If your allocated capital will regularly bump into this, consider a cash
account instead (different settlement mechanics, no PDT limit, but no
same-day reuse of proceeds either) or design the rules to trade less
frequently.

## Where an LLM could fit, if you want one

The deployed bot's hot path (scan → signal → order) is intentionally plain,
deterministic Python — cheap, fast, and predictable across dozens of
candidates every cycle. If you want Claude's judgment in the loop (reading
news, weighing a setup qualitatively), the natural seam is **after** the
quantitative scan narrows the field to a handful of candidates and
**before** `executor.py` fires: call the Anthropic API directly from
`bot/strategy.py` with the day's short candidate list and let it veto/
approve, rather than asking an LLM to evaluate hundreds of tickers. This
project doesn't include that call yet — a natural next step once you're
comfortable with the plain-rules baseline.

## Known limitations / what I'd tackle next

- Single bot instance assumed - `common/db.py` doesn't do anything to
  coordinate multiple bot processes writing at once (Postgres itself
  handles the concurrency safely; the application logic doesn't assume
  more than one bot process exists).
- No automated tests yet — the logic is straightforward enough to read,
  and this project's own smoke tests exercised the risk/allocation math
  and the conflict-check logic before delivery, but add proper unit tests
  before trusting it further with real money.
- The breakout strategy is a simple N-day-high check — a starting point,
  not a tuned strategy.
- Market data (premarket scan + breakout bars) comes from Alpaca's IEX feed,
  which is what free/paper accounts are entitled to - it's real-time but
  single-exchange, so prices can differ slightly from full consolidated-tape
  (SIP) data. The scanner batches requests but there's no backoff/retry logic
  yet if Alpaca itself rate-limits.
- No automated backtesting harness. Test a rule change against historical
  data before trusting it, not just in the dashboard.
- The symbol-conflict check queries Alpaca once per candidate per cycle;
  fine at this scale, but worth batching if you widen the universe a lot.

## Disclaimer

This is software engineering, not financial advice. Automated trading with
real money carries real risk of loss, independent of how well the code is
written — and sharing one account between two automated strategies adds a
layer of operational risk beyond that. Start on paper, keep the capital
allocation small and explicit, and don't rely on any single circuit
breaker.
