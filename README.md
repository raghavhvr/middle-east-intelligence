# Crisis Pulse

**Real-time consumer signal dashboard for MENA markets.**

Built for WPP MENA crisis reporting. Monitors behavioral and media signals across UAE, KSA, Kuwait, and Qatar — refreshed daily, zero infrastructure cost.

---

## Data Sources

| Source | What It Measures | Auth | Rate Limit |
|---|---|---|---|
| **Reddit (public JSON)** | Per-signal post volume across curated subreddits, normalised 0–100 | None | ~1 req/sec |
| **Google Trends RSS** | Trending search topics per market, crisis vs sport split | None | Unofficial |
| **GDELT v2 DOC API** | Per-signal article volume + sentiment tone, MENA geo-filtered | None | 1 req/5s |
| **Google News RSS** | Per-signal article count from MENA outlets (Al Arabiya, Arab News etc.) | None | ~2s between calls |
| **ACLED** | Conflict event intensity per market — battles, explosions, fatalities | Free researcher key | Generous |
| **Twitch API** | Live global gaming viewership and top titles | Free app registration | Token-based |

No paid APIs. No cloud server.

---

## What Changed in v3

| Change | Detail |
|---|---|
| **NewsAPI removed** | Replaced by GDELT — no rate cap, covers Arabic-language MENA sources, returns tone scores |
| **Guardian removed** | Replaced by Google News RSS — routes Al Arabiya, Arab News, Khaleej Times, Gulf News, The National through `news.google.com/rss/search?q=site:...` with no geo-blindspot |
| **ACLED added** | New `conflict` section in output — per-market event count, fatalities, intensity score (0–100), and top 5 events with actor names and locations |
| **ACLED boosts crisis signals** | `war_news`, `breaking_news`, `crisis_topics`, `fact_checking` scores are nudged upward when ACLED intensity is high (up to +25 points) |
| **GDELT tone in output** | Each signal now carries an `avg_tone` score (−100 to +100) in `news_volumes.gdelt` |
| **`gdelt_query` field added** | `signals_config.json` now has a `gdelt_query` per signal, optimised for GDELT's full-text article index |

---

## Markets

UAE · KSA · Kuwait · Qatar

---

## Architecture

```
GitHub Actions (daily 09:00 GST, timeout 90 min)
    └── scripts/collect.py
            ├── Reddit public JSON          (no key, ~10 min)
            ├── Google Trends RSS           (no key, instant)
            ├── GDELT v2 DOC API            (no key, ~60 min due to 5s rate limit)
            ├── Google News RSS / MENA      (no key, ~8 min)
            ├── ACLED conflict events       (free key, ~2 min)
            └── Twitch API                  (free key, instant)
                    ↓
            public/pulse_data.json          (committed to repo)
            public/pulse_history.json
                    ↓
            Vercel (auto-deploys on push)
                    ↓
            src/App.tsx
```

---

## Output Structure

```json
{
  "fetched_at": "...",
  "markets": {
    "UAE": { "gaming": 72.3, "inflation": 88.1, ... }
  },
  "news_volumes": {
    "gdelt": {
      "UAE": { "inflation": { "count": 47, "avg_tone": -3.2 } }
    },
    "gnews": {
      "UAE": { "inflation": { "count": 12, "titles": ["..."] } }
    }
  },
  "conflict": {
    "UAE": {
      "intensity": 64,
      "fatalities": 38,
      "event_count": 22,
      "events": [{ "date": "...", "type": "Explosion", "country": "Yemen",
                   "location": "Sanaa", "fatalities": 12, "actor1": "Houthis" }]
    }
  },
  "global": {
    "reddit": { "gaming": 100, "inflation": 84.2, ... },
    "rss_trends": { "UAE": { "crisis_pct": 30, "top_topics": [...] } },
    "twitch": { "total_viewers": 142000, "top_games": [...] }
  }
}
```

---

## Signal Config

Each signal in `public/signals_config.json` carries:

```json
{
  "label":        "Inflation",
  "gdelt_query":  "inflation cost living prices rising economic",
  "news":         "inflation prices cost of living rise",
  "reddit_subs":  ["Economics", "personalfinance", "Inflation"],
  "reddit_query": "inflation cost of living prices rising"
}
```

- `gdelt_query` — optimised keywords for GDELT's full-text article index
- `news` — legacy field, still used as fallback query
- `reddit_subs` — subreddits to search (public JSON, no auth)
- `reddit_query` — keyword string for Reddit search

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/raghavhvr/mena-crisis-intelligence.git
cd mena-crisis-intelligence
```

### 2. Install frontend dependencies

```bash
npm install
npm run dev
```

### 3. Configure secrets

The collector needs only 4 secrets (down from 6 in v2 — NewsAPI and Guardian removed):

```
ACLED_EMAIL=your.email@company.com
ACLED_KEY=your_acled_api_key
TWITCH_CLIENT_ID=your_client_id
TWITCH_CLIENT_SECRET=your_client_secret
```

**Getting your ACLED key:**
1. Register at [acleddata.com/register](https://acleddata.com/register)
2. Use your institutional email (WPP email qualifies as researcher access)
3. Once approved, find your key at [developer.acleddata.com](https://developer.acleddata.com)
4. Add `ACLED_EMAIL` and `ACLED_KEY` as GitHub repository secrets

GDELT and Google News RSS require no key.

### 4. Add GitHub secrets

Settings → Secrets and variables → Actions → New repository secret

| Secret | Where to get it |
|---|---|
| `ACLED_EMAIL` | Your registration email at acleddata.com |
| `ACLED_KEY` | developer.acleddata.com after approval |
| `TWITCH_CLIENT_ID` | dev.twitch.tv/console |
| `TWITCH_CLIENT_SECRET` | dev.twitch.tv/console |

### 5. Trigger the first run

Actions tab → Daily Pulse Refresh → Run workflow

The first run performs a 30-day backfill. **GDELT's 5-second rate limit means this takes ~60–75 minutes** — the workflow timeout is set to 90 minutes to accommodate this. Subsequent daily runs are faster (~20–30 min) as only today's data is fetched.

### 6. Deploy to Vercel

Connect the GitHub repo to Vercel, set framework to Vite, deploy. Vercel auto-redeploys on every push.

---

## Local Development

```bash
npm run dev                  # start Vite dev server (reads existing pulse_data.json)
python scripts/collect.py    # refresh data (requires ACLED_EMAIL + ACLED_KEY in .env)
```

---

## Requirements

**Frontend** — React 18, Vite, Recharts, TypeScript

**Collector** — Python 3.11+, `requests`, `python-dotenv`

---

## License

Internal tool — WPP MENA. Not for public redistribution.
