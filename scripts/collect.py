"""
Crisis Pulse — Multi-Source Data Collector v3
==============================================
Sources:
  1. Reddit           — per-signal post volume, public JSON, no key
  2. Google Trends RSS — per-market trending topics, crisis/sport split
  3. GDELT v2 DOC API  — per-signal article volume + tone, MENA geo-filtered, no key
  4. Google News RSS   — per-signal MENA-site article count, no key
  5. ACLED             — conflict event intensity per MENA country, free researcher key
  6. Twitch            — global live gaming viewership

GDELT replaces NewsAPI (no rate cap, covers Arabic-language MENA sources).
Google News RSS replaces Guardian (MENA outlets, no paywall, no geo-blindspot).
ACLED is a new dedicated conflict_intensity signal per market.

Writes:
  public/pulse_data.json    — rolling 7-day detail
  public/pulse_history.json — daily snapshot, appended + backfilled 30 days
"""

import os, json, time, logging, random, requests
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

MARKETS = {"AE": "UAE", "SA": "KSA", "KW": "Kuwait", "QA": "Qatar"}

# Geo search terms used across GDELT, Google News RSS, and blending
MARKET_GEO_TERMS = {
    "UAE":    "UAE OR Dubai OR \"Abu Dhabi\" OR Emirates",
    "KSA":    "\"Saudi Arabia\" OR Riyadh OR Jeddah OR KSA",
    "Kuwait": "Kuwait",
    "Qatar":  "Qatar OR Doha",
}

# GDELT uses simplified OR syntax (no quotes in the URL term list)
MARKET_GDELT_GEO = {
    "UAE":    "UAE Dubai Emirates",
    "KSA":    "\"Saudi Arabia\" Riyadh Jeddah KSA",
    "Kuwait": "Kuwait",
    "Qatar":  "Qatar Doha",
}

# MENA news sites piped through Google News RSS gn() trick
# These cover Al Arabiya, Gulf News, Arab News, Khaleej Times, The National
MENA_RSS_SITES = [
    "english.alarabiya.net",
    "gulfnews.com",
    "arabnews.com",
    "khaleejtimes.com",
    "thenationalnews.com",
]

# ACLED country names for each market + regional neighbours that affect sentiment
ACLED_COUNTRIES = {
    "UAE":    ["United Arab Emirates", "Yemen", "Iran"],
    "KSA":    ["Saudi Arabia", "Yemen", "Iraq"],
    "Kuwait": ["Kuwait", "Iraq", "Iran"],
    "Qatar":  ["Qatar", "Yemen", "Syria"],
}

SPORT_KW  = ["football","soccer","game","match","vs","ucl","league","cup","sport",
             "film","movie","music","cricket","ipl","nba","f1","basketball"]
CRISIS_KW = ["war","attack","crisis","shortage","price","inflation","ban",
             "sanction","protest","arrest","flood","earthquake","strike","conflict"]

BACKFILL_DAYS = 30

BASE_PATH    = Path(__file__).parent.parent
OUTPUT_PATH  = BASE_PATH / "public" / "pulse_data.json"
HISTORY_PATH = BASE_PATH / "public" / "pulse_history.json"
CONFIG_PATH  = BASE_PATH / "public" / "signals_config.json"

REDDIT_HEADERS = {"User-Agent": "CrisisPulse/3.0 (github-actions daily aggregator)"}
GDELT_BASE     = "https://api.gdeltproject.org/api/v2/doc/doc"
GNEWS_BASE     = "https://news.google.com/rss/search"


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception as e:
        log.error(f"Cannot load signals_config.json: {e}")
        raise

def flat_signals(config: dict) -> dict:
    out = {}
    now            = datetime.now(timezone.utc).date()
    ramadan_active = config.get("ramadan_active", False)
    ramadan_end    = config.get("ramadan_end", "")
    if ramadan_end:
        try:
            ramadan_active = ramadan_active and now <= datetime.fromisoformat(ramadan_end).date()
        except:
            pass
    for cat_key, cat in config["categories"].items():
        if cat.get("ramadan_only") and not ramadan_active:
            continue
        for sig_key, sig in cat["signals"].items():
            out[sig_key] = {**sig, "category": cat_key, "category_label": cat["label"],
                            "color": cat["color"], "icon": cat["icon"]}
    return out


# ── File helpers ──────────────────────────────────────────────────────────────

def safe_get(url, **kwargs):
    try:
        return requests.get(url, timeout=15, **kwargs)
    except Exception as e:
        log.warning(f"  Request failed: {e}")
        return None

def load_existing() -> dict:
    try:
        if OUTPUT_PATH.exists():
            d = json.loads(OUTPUT_PATH.read_text())
            if d.get("categories"):
                return d
    except:
        pass
    return {}

def load_history() -> list:
    try:
        if HISTORY_PATH.exists():
            return json.loads(HISTORY_PATH.read_text())
    except:
        pass
    return []

def save_history(h: list):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(h, indent=2))


# ── Source 1: Reddit (unchanged from v2) ─────────────────────────────────────

def fetch_reddit_signal(subreddits: list, query: str, days: int = 1) -> int:
    cutoff    = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()
    total     = 0
    for sub in subreddits[:3]:
        url    = f"https://www.reddit.com/r/{sub}/search.json"
        params = {"q": query, "sort": "new",
                  "t": "week" if days <= 7 else "month",
                  "limit": 100, "restrict_sr": 1}
        r = safe_get(url, headers=REDDIT_HEADERS, params=params)
        if not r or r.status_code != 200:
            r2 = safe_get(f"https://www.reddit.com/r/{sub}/new.json",
                          headers=REDDIT_HEADERS, params={"limit": 100})
            if r2 and r2.status_code == 200:
                try:
                    posts = r2.json().get("data", {}).get("children", [])
                    kws   = query.lower().split()
                    for p in posts:
                        d = p.get("data", {})
                        if d.get("created_utc", 0) < cutoff_ts:
                            continue
                        text = (d.get("title","") + " " + d.get("selftext","")).lower()
                        if any(kw in text for kw in kws):
                            total += 1
                except:
                    pass
            time.sleep(0.6)
            continue
        try:
            for p in r.json().get("data", {}).get("children", []):
                if p.get("data", {}).get("created_utc", 0) >= cutoff_ts:
                    total += 1
        except:
            pass
        time.sleep(0.6)
    return total

def fetch_reddit_all_signals(signals: dict, days: int = 1) -> dict:
    log.info(f"\nReddit ({days}-day window)...")
    raw = {}
    for sig_key, cfg in signals.items():
        subs  = cfg.get("reddit_subs", ["all"])
        query = cfg.get("reddit_query") or cfg.get("news", sig_key)
        count = fetch_reddit_signal(subs, query, days=days)
        raw[sig_key] = count
        log.info(f"  {'OK' if count else '--'} {sig_key}: {count} posts")
        time.sleep(0.3)
    max_val = max(raw.values(), default=1) or 1
    return {k: round(v / max_val * 100, 1) for k, v in raw.items()}

def fetch_reddit_range(signals: dict, days: int = 30) -> dict:
    log.info(f"\nReddit backfill ({days} days)...")
    now, result = datetime.now(timezone.utc), {sig: {} for sig in signals}
    for sig_key, cfg in signals.items():
        subs  = cfg.get("reddit_subs", ["all"])
        query = cfg.get("reddit_query") or cfg.get("news", sig_key)
        for w in range((days // 7) + 1):
            w_days = min(7, days - w * 7)
            if w_days <= 0:
                break
            count   = fetch_reddit_signal(subs, query, days=w_days + w * 7)
            per_day = round(count / max(w_days, 1))
            for d in range(w_days):
                day = now - timedelta(days=w * 7 + d + 1)
                result[sig_key][day.strftime("%Y%m%d")] = per_day
            time.sleep(0.5)
    all_vals = [v for sd in result.values() for v in sd.values()]
    max_val  = max(all_vals, default=1) or 1
    for sig_key in result:
        result[sig_key] = {k: round(v / max_val * 100, 1) for k, v in result[sig_key].items()}
    return result


# ── Source 2: Google Trends RSS (unchanged) ───────────────────────────────────

def fetch_rss(geo: str) -> dict:
    r = safe_get(f"https://trends.google.com/trending/rss?geo={geo}",
                 headers={"User-Agent": "Mozilla/5.0"})
    if not r or r.status_code != 200:
        return {}
    try:
        root   = ET.fromstring(r.text)
        topics = [i.find("title").text or ""
                  for i in root.findall(".//item") if i.find("title") is not None]
        total  = len(topics) or 1
        return {
            "sport_entertainment_pct": round(sum(1 for t in topics if any(k in t.lower() for k in SPORT_KW)) / total * 100),
            "crisis_pct":              round(sum(1 for t in topics if any(k in t.lower() for k in CRISIS_KW)) / total * 100),
            "top_topics":              topics[:10],
        }
    except Exception as e:
        log.warning(f"  RSS parse error [{geo}]: {e}")
        return {}


# ── Source 3: GDELT v2 DOC API ────────────────────────────────────────────────
#
# Replaces NewsAPI. No key required. 5-second minimum between requests.
# Uses artlist mode to count matching articles per signal × market in 24h.
# Tone score (-100 to +100) is averaged across articles as a bonus signal.

def fetch_gdelt_signal(signal_query: str, market: str, timespan: str = "24h") -> dict:
    """
    Returns { count: int, avg_tone: float } for a signal+market pair.
    signal_query: the signal's gdelt_query field (e.g. "inflation prices cost living")
    market: one of UAE / KSA / Kuwait / Qatar
    """
    geo   = MARKET_GDELT_GEO.get(market, market)
    query = f"({signal_query}) ({geo})"
    params = {
        "query":      query,
        "mode":       "artlist",
        "timespan":   timespan,
        "maxrecords": 250,
        "format":     "json",
        "sourcelang": "english",
    }
    time.sleep(5.5)  # GDELT requires ≥5s between requests
    r = safe_get(GDELT_BASE, params=params)
    if not r or r.status_code != 200:
        log.warning(f"  GDELT {r.status_code if r else 'timeout'} [{market}/{signal_query[:20]}]")
        return {"count": 0, "avg_tone": 0.0}
    try:
        articles = r.json().get("articles", [])
        if not articles:
            return {"count": 0, "avg_tone": 0.0}
        tones = [float(a.get("tone", 0)) for a in articles if a.get("tone") is not None]
        return {
            "count":    len(articles),
            "avg_tone": round(sum(tones) / len(tones), 2) if tones else 0.0,
        }
    except Exception as e:
        log.warning(f"  GDELT parse error: {e}")
        return {"count": 0, "avg_tone": 0.0}


def fetch_gdelt_all(signals: dict, timespan: str = "24h") -> dict:
    """
    Returns {
      market: { sig_key: { count, avg_tone } }
    }
    GDELT's 5s rate limit means this takes ~signals × markets × 5.5s ≈ 12 min for full run.
    We batch by market to allow partial success.
    """
    log.info(f"\nGDELT ({timespan} window)...")
    result = {m: {} for m in MARKETS.values()}
    ok     = False

    for market_name in MARKETS.values():
        for sig_key, cfg in signals.items():
            query = cfg.get("gdelt_query") or cfg.get("news", sig_key)
            data  = fetch_gdelt_signal(query, market_name, timespan)
            result[market_name][sig_key] = data
            if data["count"]:
                ok = True
                log.info(f"  OK {market_name}/{sig_key}: {data['count']} articles, tone={data['avg_tone']}")
            else:
                log.info(f"  -- {market_name}/{sig_key}: 0 articles")

    return result if ok else {}


def fetch_gdelt_backfill(signals: dict, days: int = 30) -> dict:
    """
    Backfill using GDELT's timelinevol mode — returns daily article volume per signal per market.
    Returns { market: { sig_key: { 'YYYYMMDD': count } } }
    """
    log.info(f"\nGDELT backfill ({days} days)...")
    result = {m: {s: {} for s in signals} for m in MARKETS.values()}
    timespan = f"{days}d"

    for market_name in MARKETS.values():
        for sig_key, cfg in signals.items():
            query  = cfg.get("gdelt_query") or cfg.get("news", sig_key)
            geo    = MARKET_GDELT_GEO.get(market_name, market_name)
            params = {
                "query":    f"({query}) ({geo})",
                "mode":     "timelinevol",
                "timespan": timespan,
                "format":   "json",
            }
            time.sleep(5.5)
            r = safe_get(GDELT_BASE, params=params)
            if not r or r.status_code != 200:
                continue
            try:
                tl = r.json().get("timeline", [])
                if not tl or not tl[0].get("data"):
                    continue
                for entry in tl[0]["data"]:
                    # date format: "2026 Mar 20"
                    try:
                        dt      = datetime.strptime(entry["date"], "%Y %b %d")
                        day_key = dt.strftime("%Y%m%d")
                        result[market_name][sig_key][day_key] = round(entry.get("value", 0))
                    except:
                        pass
                log.info(f"  OK {market_name}/{sig_key}: {len(result[market_name][sig_key])} days")
            except Exception as e:
                log.warning(f"  GDELT backfill error {market_name}/{sig_key}: {e}")

    return result


# ── Source 4: Google News RSS (MENA sites) ────────────────────────────────────
#
# Replaces Guardian. Uses news.google.com/rss/search to proxy MENA news outlets
# that would otherwise block Vercel/GitHub Actions IPs.
# Returns article count per signal × market, also captures article titles
# for summary generation.

def fetch_gnews_signal(signal_query: str, market: str, days: int = 7) -> dict:
    """
    Count articles about signal_query in market-relevant MENA news sites
    from the last `days` days.
    Returns { count: int, titles: [str] }
    """
    geo   = MARKET_GEO_TERMS.get(market, market)
    when  = f"when:{days}d"
    total = 0
    titles = []

    # 1. Broad market × signal query across all Google News
    broad_query = f"{signal_query} {geo} {when}"
    params = {"q": broad_query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    r = safe_get(GNEWS_BASE, params=params)
    if r and r.status_code == 200:
        try:
            root  = ET.fromstring(r.text)
            items = root.findall(".//item")
            total += len(items)
            for item in items[:5]:
                t = item.find("title")
                if t is not None and t.text:
                    titles.append(t.text)
        except Exception as e:
            log.warning(f"  GNews parse error [{market}/{signal_query[:20]}]: {e}")
    time.sleep(2)

    # 2. MENA-specific sites via site: operator (picks up Al Arabiya, Arab News etc.)
    for site in MENA_RSS_SITES[:2]:  # 2 sites to keep rate manageable
        site_query = f"site:{site} {signal_query} {when}"
        r2 = safe_get(GNEWS_BASE, params={"q": site_query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        if r2 and r2.status_code == 200:
            try:
                root2 = ET.fromstring(r2.text)
                items2 = root2.findall(".//item")
                total += len(items2)
                for item in items2[:3]:
                    t = item.find("title")
                    if t is not None and t.text:
                        titles.append(t.text)
            except:
                pass
        time.sleep(2)

    return {"count": total, "titles": titles[:8]}


def fetch_gnews_all(signals: dict, days: int = 7) -> dict:
    """
    Returns { market: { sig_key: { count, titles } } }
    """
    log.info(f"\nGoogle News RSS ({days}-day window, MENA outlets)...")
    result = {m: {} for m in MARKETS.values()}
    ok     = False

    for market_name in MARKETS.values():
        for sig_key, cfg in signals.items():
            query = cfg.get("gdelt_query") or cfg.get("news", sig_key)
            data  = fetch_gnews_signal(query, market_name, days)
            result[market_name][sig_key] = data
            if data["count"]:
                ok = True
                log.info(f"  OK {market_name}/{sig_key}: {data['count']} articles")
            else:
                log.info(f"  -- {market_name}/{sig_key}")

    return result if ok else {}


# ── Source 5: ACLED ───────────────────────────────────────────────────────────
#
# Free researcher API. Returns conflict events (battles, explosions, protests)
# per country with fatality counts, geo-coordinates, and actor names.
# Used to build a conflict_intensity score per market, and populates
# a new result["conflict"] section with event-level detail.
#
# Auth: OAuth2 password grant — token fetched fresh each run, no static key needed.
# Register at: https://acleddata.com/register
# Env vars: ACLED_EMAIL, ACLED_PASSWORD

ACLED_TOKEN_URL = "https://acleddata.com/oauth/token"
ACLED_BASE      = "https://api.acleddata.com/acled/read"
ACLED_FIELDS    = "event_date,event_type,sub_event_type,country,location,latitude,longitude,fatalities,actor1,actor2,notes"


def get_acled_token(email: str, password: str) -> str:
    """
    Exchange email + password for a Bearer token using ACLED's OAuth2 password grant.
    Returns the access_token string, or "" on failure.
    """
    if not email or not password:
        return ""
    try:
        r = requests.post(
            ACLED_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "username":   email,
                "password":   password,
                "grant_type": "password",
                "client_id":  "acled",
            },
            timeout=15,
        )
        if r.status_code == 200:
            token = r.json().get("access_token", "")
            if token:
                log.info("  ACLED token obtained")
                return token
        log.warning(f"  ACLED token error {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log.warning(f"  ACLED token request failed: {e}")
    return ""


def fetch_acled_market(market: str, token: str, days: int = 7) -> dict:
    """
    Returns { events: [...], fatalities: int, event_count: int, intensity: float (0-100) }
    Queries the primary country + high-influence neighbours (see ACLED_COUNTRIES).
    """
    if not token:
        return {}

    countries  = ACLED_COUNTRIES.get(market, [market])
    since_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    all_events = []
    headers    = {"Authorization": f"Bearer {token}"}

    for country in countries:
        params = {
            "country":          country,
            "event_date":       since_date,
            "event_date_where": ">=",
            "limit":            100,
            "fields":           ACLED_FIELDS,
        }
        r = safe_get(ACLED_BASE, headers=headers, params=params)
        if not r or r.status_code != 200:
            log.warning(f"  ACLED {r.status_code if r else 'timeout'} [{market}/{country}]")
            continue
        try:
            data = r.json()
            if data.get("success"):
                events = data.get("data", [])
                all_events.extend(events)
                log.info(f"  OK ACLED {country}: {len(events)} events")
        except Exception as e:
            log.warning(f"  ACLED parse error [{country}]: {e}")
        time.sleep(1)

    if not all_events:
        return {"events": [], "fatalities": 0, "event_count": 0, "intensity": 0.0}

    fatalities  = sum(int(e.get("fatalities", 0)) for e in all_events)
    event_count = len(all_events)

    # Intensity: fatalities weighted 3x + event count, normalised 0-100
    # ~50 fatalities + 20 events ≈ 50 intensity
    raw_intensity = min(100, round((fatalities * 3 + event_count) / 2))

    sorted_events = sorted(all_events,
                           key=lambda e: (int(e.get("fatalities", 0)), e.get("event_date", "")),
                           reverse=True)
    top_events = [{
        "date":       e.get("event_date"),
        "type":       e.get("event_type"),
        "country":    e.get("country"),
        "location":   e.get("location"),
        "fatalities": int(e.get("fatalities", 0)),
        "actor1":     e.get("actor1"),
        "notes":      (e.get("notes", "") or "")[:120],
    } for e in sorted_events[:5]]

    return {
        "events":      top_events,
        "fatalities":  fatalities,
        "event_count": event_count,
        "intensity":   raw_intensity,
    }


def fetch_acled_all(email: str, password: str, days: int = 7) -> dict:
    """
    Gets a fresh OAuth token then queries all markets.
    Returns { market: { events, fatalities, event_count, intensity } }
    """
    if not email or not password:
        log.warning("  ACLED_EMAIL or ACLED_PASSWORD not set — skipping")
        return {}

    log.info(f"\nACLED ({days}-day window)...")
    token = get_acled_token(email, password)
    if not token:
        log.warning("  ACLED auth failed — skipping")
        return {}

    result = {}
    for market_name in MARKETS.values():
        data = fetch_acled_market(market_name, token, days)
        if data:
            result[market_name] = data
            log.info(f"  OK {market_name}: intensity={data.get('intensity')} "
                     f"({data.get('event_count')} events, {data.get('fatalities')} fatalities)")
    return result


# ── RSS blend into scores (unchanged, extended for ACLED) ────────────────────

CRISIS_SIGNALS = {"breaking_news","crisis_topics","war_news","fact_checking"}
SPORT_SIGNALS  = {"gaming","streaming","humour"}

def blend_signals_into_scores(market_scores: dict, rss_data: dict,
                               acled_data: dict, signals: dict) -> dict:
    """
    Applies two boosts on top of the Reddit base scores:
    1. RSS crisis/sport percentages nudge relevant signals (as before)
    2. ACLED conflict intensity nudges crisis/war signals — more strongly
    """
    blended = {m: dict(sigs) for m, sigs in market_scores.items()}

    for market_name in MARKETS.values():
        rss          = rss_data.get(market_name, {})
        crisis_pct   = rss.get("crisis_pct", 0)
        sport_pct    = rss.get("sport_entertainment_pct", 0)
        acled        = acled_data.get(market_name, {})
        acled_inten  = acled.get("intensity", 0)

        for sig_key in signals:
            base = blended.get(market_name, {}).get(sig_key)
            if base is None:
                continue

            boost = 0
            if sig_key in CRISIS_SIGNALS:
                # RSS crisis contributes up to +15; ACLED intensity up to +25
                boost = crisis_pct * 0.15 + acled_inten * 0.25
            elif sig_key in SPORT_SIGNALS:
                boost = sport_pct * 0.10

            if boost:
                blended[market_name][sig_key] = min(100, round(base + boost, 1))

    return blended


# ── Backfill ──────────────────────────────────────────────────────────────────

def backfill(signals: dict, acled_email: str, acled_password: str) -> list:
    log.info(f"\nBackfilling {BACKFILL_DAYS} days...")
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=BACKFILL_DAYS)

    # Reddit: weekly buckets
    reddit_range = fetch_reddit_range(signals, days=BACKFILL_DAYS)

    # GDELT: daily timeline per signal per market (takes time due to 5s rate limit)
    gdelt_range = fetch_gdelt_backfill(signals, days=BACKFILL_DAYS)

    # ACLED: single 30-day pull per market (no daily bucketing needed — use flat per-day)
    acled_30d = {}
    if acled_email and acled_password:
        acled_token = get_acled_token(acled_email, acled_password)
        if acled_token:
            for market_name in MARKETS.values():
                data = fetch_acled_market(market_name, acled_token, days=BACKFILL_DAYS)
                if data:
                    acled_30d[market_name] = data

    # Normalise GDELT counts across all signals/markets/days
    all_gdelt_counts = [
        v
        for m_dict in gdelt_range.values()
        for s_dict in m_dict.values()
        for v in s_dict.values()
    ]
    gdelt_max = max(all_gdelt_counts, default=1) or 1

    records = []
    for days_ago in range(BACKFILL_DAYS, 0, -1):
        day     = now - timedelta(days=days_ago)
        day_str = day.strftime("%Y-%m-%d")
        day_key = day.strftime("%Y%m%d")
        record  = {
            "date": day_str, "markets": {}, "news_volumes": {},
            "conflict": {}, "twitch_viewers": 0,
        }

        for market_name in MARKETS.values():
            record["markets"][market_name] = {}
            for sig_key in signals:
                reddit_score = reddit_range.get(sig_key, {}).get(day_key)
                gdelt_count  = gdelt_range.get(market_name, {}).get(sig_key, {}).get(day_key, 0)
                gdelt_norm   = round(gdelt_count / gdelt_max * 100, 1)
                # Blend Reddit (60%) + GDELT (40%)
                if reddit_score is not None:
                    record["markets"][market_name][sig_key] = round(reddit_score * 0.6 + gdelt_norm * 0.4, 1)
                else:
                    record["markets"][market_name][sig_key] = gdelt_norm or None

        for sig_key in signals:
            gdelt_avg = sum(
                gdelt_range.get(m, {}).get(sig_key, {}).get(day_key, 0)
                for m in MARKETS.values()
            ) / len(MARKETS)
            record["news_volumes"][sig_key] = round(gdelt_avg)

        # ACLED intensity is the same across the 30-day window (no daily bucketing)
        for market_name in MARKETS.values():
            if market_name in acled_30d:
                record["conflict"][market_name] = {
                    "intensity":   acled_30d[market_name].get("intensity", 0),
                    "fatalities":  acled_30d[market_name].get("fatalities", 0),
                    "event_count": acled_30d[market_name].get("event_count", 0),
                }

        records.append(record)

    log.info(f"Backfill done — {len(records)} records")
    return records


# ── Today snapshot ────────────────────────────────────────────────────────────

def append_today(history: list, pulse: dict, signals: dict) -> list:
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history  = [r for r in history if r.get("date") != today]
    gdelt    = pulse.get("news_volumes", {}).get("gdelt", {})
    gnews    = pulse.get("news_volumes", {}).get("gnews", {})
    reddit   = pulse.get("global", {}).get("reddit", {})
    conflict = pulse.get("conflict", {})

    snap = {
        "date": today, "markets": {}, "news_volumes": {},
        "news_volumes_by_market": {}, "conflict": conflict,
        "twitch_viewers": pulse.get("global", {}).get("twitch", {}).get("total_viewers", 0),
    }

    # Compute per-market signal scores: Reddit (60%) + GDELT count norm (40%)
    all_gdelt_counts = [
        gdelt.get(m, {}).get(s, {}).get("count", 0)
        for m in MARKETS.values() for s in signals
    ]
    gdelt_max = max(all_gdelt_counts, default=1) or 1

    for market_name in MARKETS.values():
        snap["markets"][market_name] = {}
        snap["news_volumes_by_market"][market_name] = {}
        for sig_key in signals:
            reddit_score = reddit.get(sig_key)
            gdelt_count  = gdelt.get(market_name, {}).get(sig_key, {}).get("count", 0)
            gnews_count  = gnews.get(market_name, {}).get(sig_key, {}).get("count", 0)
            total_news   = gdelt_count + gnews_count
            gdelt_norm   = round(gdelt_count / gdelt_max * 100, 1)

            snap["news_volumes_by_market"][market_name][sig_key] = total_news

            if reddit_score is not None:
                snap["markets"][market_name][sig_key] = round(reddit_score * 0.6 + gdelt_norm * 0.4, 1)
            else:
                snap["markets"][market_name][sig_key] = gdelt_norm or None

    # Global news volume rollup
    for sig_key in signals:
        snap["news_volumes"][sig_key] = sum(
            gdelt.get(m, {}).get(sig_key, {}).get("count", 0) +
            gnews.get(m, {}).get(sig_key, {}).get("count", 0)
            for m in MARKETS.values()
        )

    history.append(snap)
    log.info(f"Appended {today}. Total: {len(history)} records")
    return history


# ── Main ──────────────────────────────────────────────────────────────────────

def collect():
    acled_email    = os.getenv("ACLED_EMAIL", "")
    acled_password = os.getenv("ACLED_PASSWORD", "")
    twitch_id     = os.getenv("TWITCH_CLIENT_ID", "")
    twitch_secret = os.getenv("TWITCH_CLIENT_SECRET", "")

    config  = load_config()
    signals = flat_signals(config)
    log.info(f"{len(signals)} signals | {len(config['categories'])} categories")

    existing  = load_existing()
    history   = load_history()
    now       = datetime.now(timezone.utc)
    date_labels = [(now - timedelta(days=7 - i)).strftime("%b %d") for i in range(8)]

    # Backfill check
    existing_dates = {r["date"] for r in history}
    missing = [(now - timedelta(days=d)).strftime("%Y-%m-%d")
               for d in range(BACKFILL_DAYS, 0, -1)
               if (now - timedelta(days=d)).strftime("%Y-%m-%d") not in existing_dates]
    if missing:
        log.info(f"Missing {len(missing)} days — running backfill")
        backfilled = backfill(signals, acled_email, acled_password)
        bf_by_date = {r["date"]: r for r in backfilled}
        history    = [r for r in history if r["date"] not in bf_by_date]
        history    = sorted(history + list(bf_by_date.values()), key=lambda r: r["date"])
        save_history(history)
        log.info(f"History: {len(history)} records after backfill")
    else:
        log.info(f"History complete — {len(history)} records")

    result = {
        "fetched_at":     now.isoformat(),
        "dates":          date_labels,
        "categories":     {},
        "markets":        {m: dict(existing.get("markets",{}).get(m,{})) for m in MARKETS.values()},
        "global":         {},
        "news_volumes":   {},
        "conflict":       {},
        "sources_live":   [],
        "sources_failed": [],
        "ramadan_active": config.get("ramadan_active", False),
    }
    for cat_key, cat in config["categories"].items():
        result["categories"][cat_key] = {
            "label": cat["label"], "icon": cat["icon"], "color": cat["color"],
            "hypothesis": cat.get("hypothesis",""),
            "ramadan_only": cat.get("ramadan_only", False),
            "signals": list(cat["signals"].keys()),
        }

    # ── Reddit ────────────────────────────────────────────────────────────────
    reddit_scores = fetch_reddit_all_signals(signals, days=1)
    if reddit_scores:
        result["global"]["reddit"] = reddit_scores
        result["sources_live"].append("reddit")
        for market_name in MARKETS.values():
            for sig_key, score in reddit_scores.items():
                if not result["markets"][market_name].get(sig_key):
                    result["markets"][market_name][sig_key] = score
    else:
        result["sources_failed"].append("reddit")

    # ── Google Trends RSS ─────────────────────────────────────────────────────
    log.info("\nGoogle Trends RSS...")
    rss_data = {}
    for geo, market_name in MARKETS.items():
        time.sleep(random.uniform(1, 2))
        trends = fetch_rss(geo)
        if trends:
            rss_data[market_name] = trends
            log.info(f"  OK {market_name}: sport={trends['sport_entertainment_pct']}% crisis={trends['crisis_pct']}%")
        else:
            log.warning(f"  -- {market_name}")
    if rss_data:
        result["global"]["rss_trends"] = rss_data
        result["sources_live"].append("google_rss")
    else:
        result["sources_failed"].append("google_rss")

    # ── GDELT ─────────────────────────────────────────────────────────────────
    gdelt_vols = fetch_gdelt_all(signals, timespan="24h")
    if gdelt_vols:
        result["news_volumes"]["gdelt"] = gdelt_vols
        result["sources_live"].append("gdelt")
    else:
        result["sources_failed"].append("gdelt")

    # ── Google News RSS (MENA outlets) ────────────────────────────────────────
    gnews_vols = fetch_gnews_all(signals, days=7)
    if gnews_vols:
        result["news_volumes"]["gnews"] = gnews_vols
        result["sources_live"].append("google_news_rss")
    else:
        result["sources_failed"].append("google_news_rss")

    # ── ACLED ─────────────────────────────────────────────────────────────────
    acled_data = fetch_acled_all(acled_email, acled_password, days=7)
    if acled_data:
        result["conflict"] = acled_data
        result["sources_live"].append("acled")
    else:
        result["sources_failed"].append("acled")

    # ── Blend RSS + ACLED into market scores ──────────────────────────────────
    result["markets"] = blend_signals_into_scores(
        result["markets"], rss_data, acled_data, signals
    )

    # ── Twitch ────────────────────────────────────────────────────────────────
    log.info("\nTwitch...")
    if twitch_id and twitch_secret:
        try:
            t = requests.post("https://id.twitch.tv/oauth2/token",
                              data={"client_id": twitch_id, "client_secret": twitch_secret,
                                    "grant_type": "client_credentials"}, timeout=10)
            if t.status_code == 200:
                token   = t.json()["access_token"]
                s       = requests.get("https://api.twitch.tv/helix/streams", params={"first": 20},
                                       headers={"Client-Id": twitch_id, "Authorization": f"Bearer {token}"}, timeout=10)
                streams = s.json().get("data", []) if s.status_code == 200 else []
                games: dict = {}
                for st in streams:
                    g = st.get("game_name","Unknown")
                    games[g] = games.get(g,0) + st["viewer_count"]
                top = sorted(games.items(), key=lambda x: x[1], reverse=True)[:5]
                twitch_data = {
                    "total_viewers": sum(st["viewer_count"] for st in streams),
                    "top_games": [{"name": g, "viewers": v} for g,v in top],
                }
                result["global"]["twitch"] = twitch_data
                result["sources_live"].append("twitch")
                log.info(f"  OK {twitch_data['total_viewers']:,} viewers")
        except Exception as e:
            log.warning(f"  Twitch error: {e}")
            result["sources_failed"].append("twitch")
    else:
        result["sources_failed"].append("twitch")

    log.info(f"\nLive:   {result['sources_live']}")
    log.info(f"Failed: {result['sources_failed']}")
    return result, signals, config


# ── Market Summaries ──────────────────────────────────────────────────────────

def generate_market_summary(market: str, data: dict, config: dict) -> str:
    categories = config.get("categories", {})
    gdelt_raw  = data.get("news_volumes", {}).get("gdelt", {})
    gnews_raw  = data.get("news_volumes", {}).get("gnews", {})
    reddit_raw = data.get("global", {}).get("reddit", {})
    rss        = data.get("global", {}).get("rss_trends", {}).get(market, {})
    conflict   = data.get("conflict", {}).get(market, {})
    is_ramadan = bool(data.get("ramadan_active") and config.get("ramadan_active"))

    all_signals = {}
    for ck, cat in categories.items():
        if cat.get("ramadan_only") and not is_ramadan:
            continue
        for sk in cat.get("signals", {}).keys():
            gdelt  = gdelt_raw.get(market, {}).get(sk, {}).get("count", 0)
            gnews  = gnews_raw.get(market, {}).get(sk, {}).get("count", 0)
            rd     = reddit_raw.get(sk, 0) if reddit_raw else 0
            all_signals[sk] = {"score": gdelt + gnews + rd, "cat": ck, "cat_label": cat["label"]}

    ranked      = sorted(all_signals.items(), key=lambda x: x[1]["score"], reverse=True)
    top2        = ranked[:2]
    cat_scores  = {}
    for sk, info in all_signals.items():
        cat_scores[info["cat"]] = cat_scores.get(info["cat"],0) + info["score"]
    top_cat_key   = max(cat_scores, key=cat_scores.get) if cat_scores else ""
    top_cat_label = categories.get(top_cat_key, {}).get("label","")
    sport_pct     = rss.get("sport_entertainment_pct",0)
    crisis_pct    = rss.get("crisis_pct",0)
    trend_str     = ", ".join(rss.get("top_topics",[])[:3]) or None

    def sl(sk): return sk.replace("_"," ")

    # Conflict context from ACLED
    conflict_note = ""
    if conflict and conflict.get("intensity",0) > 20:
        fat = conflict.get("fatalities",0)
        cnt = conflict.get("event_count",0)
        top_event = conflict.get("events",[{}])[0]
        region = top_event.get("country","the region")
        conflict_note = (f" ACLED data shows {cnt} conflict events and {fat} fatalities "
                        f"in {region} and neighbouring countries over the past 7 days, "
                        f"driving elevated crisis awareness scores.")

    mood  = "crisis-driven" if crisis_pct > sport_pct else "entertainment-led"
    p1    = (f"Consumer attention in {market} is concentrated around **{sl(top2[0][0])}** and "
             f"**{sl(top2[1][0])}**, which together dominate the signal landscape. "
             f"The overall mood is {mood}, with {top_cat_label} emerging as the strongest category."
             + (" Ramadan is amplifying late-night activity and iftar-related consumption." if is_ramadan else "")
             + conflict_note)

    drivers = []
    if crisis_pct >= 20: drivers.append(f"elevated regional crisis coverage ({crisis_pct}% of trending topics)")
    if sport_pct  >= 20: drivers.append(f"strong sports and entertainment engagement ({sport_pct}%)")
    if conflict.get("intensity",0) > 30:
        drivers.append(f"active conflict in the region ({conflict.get('event_count',0)} ACLED-tracked events)")
    if is_ramadan:        drivers.append("the Ramadan consumption cycle shifting peak hours to evenings")
    if trend_str:         drivers.append(f"trending conversations around {trend_str}")
    if not drivers:       drivers.append("a mix of seasonal and regional factors")
    p2 = (f"This pattern is being driven by {'; '.join(drivers)}. "
          f"The {sl(top2[0][0])} signal in particular reflects the current media environment "
          f"across MENA, with audiences actively tracking developing stories alongside daily life.")

    if crisis_pct >= 20 or conflict.get("intensity",0) > 40:
        action = (f"avoid hard promotional messaging this week — contextual and empathy-led "
                  f"creatives will perform better alongside {sl(top2[0][0])} content environments")
    elif is_ramadan:
        action = (f"activate Ramadan prime time (9-11pm) — iftar-moment sponsorships and "
                  f"evening digital placements capture peak {sl(top2[1][0])} engagement")
    else:
        action = (f"lean into {top_cat_label.lower()} environments — the {sl(top2[0][0])} signal "
                  f"suggests audiences are primed for discovery content over hard sell this week")
    p3 = f"For media planners in {market}: {action}."

    return f"{p1}\n\n{p2}\n\n{p3}"


def generate_all_summaries(data: dict, config: dict) -> dict:
    log.info("\nGenerating market summaries...")
    summaries = {}
    for market in MARKETS.values():
        try:
            text = generate_market_summary(market, data, config)
            summaries[market] = text
            log.info(f"  OK {market}")
        except Exception as e:
            log.warning(f"  -- {market}: {e}")
    return summaries


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  Crisis Pulse — Multi-Source Collector v3")
    log.info(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 50)

    try:
        data, signals, config = collect()
    except Exception as e:
        log.error(f"Collection failed: {e}")
        existing = load_existing()
        if existing:
            existing["fetched_at"] = datetime.now(timezone.utc).isoformat()
            existing["error"]      = str(e)
            data, signals, config  = existing, {}, {}
        else:
            raise

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(data, indent=2))
    log.info("pulse_data.json written")

    if signals:
        history = load_history()
        history = append_today(history, data, signals)
        save_history(history)
        log.info("pulse_history.json written")
