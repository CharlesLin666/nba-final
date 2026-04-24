import os
import re
import json
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_TO = os.environ.get("GMAIL_TO", GMAIL_USER)
STAKE_TWD = int(os.environ.get("STAKE_TWD", "1000"))
SENT_ALERTS_FILE = "sent_alerts.json"
MANUAL_ODDS_FILE = "manual_odds.json"
POLYMARKET_SLUGS = ["nba", "mlb", "tennis"]
POLYMARKET_BASE = "https://gamma-api.polymarket.com/events?active=true&closed=false&order=startTime&ascending=true&limit=100&tag_slug={slug}"

COOLDOWN_HOURS = 2

# 測試模式：門檻降低，測試完畢後改回正式門檻
TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
if TEST_MODE:
    MIN_PROB   = 0.50   # 正式: 0.80
    MIN_EDGE   = 0.00   # 正式: 0.06
    MIN_VOLUME = 1000   # 正式: 100000
    MAX_HOURS_TO_START = 72  # 正式: 6
else:
    MIN_PROB   = 0.80
    MIN_EDGE   = 0.06
    MIN_VOLUME = 100000
    MAX_HOURS_TO_START = 6

# NBA 球隊名稱對照表（Polymarket 短名 → 正規化）
TEAM_ALIASES = {
    # NBA
    "hawks": "hawks", "atlanta": "hawks",
    "celtics": "celtics", "boston": "celtics",
    "nets": "nets", "brooklyn": "nets",
    "hornets": "hornets", "charlotte": "hornets",
    "bulls": "bulls", "chicago": "bulls",
    "cavaliers": "cavaliers", "cavs": "cavaliers", "cleveland": "cavaliers",
    "mavericks": "mavericks", "mavs": "mavericks", "dallas": "mavericks",
    "nuggets": "nuggets", "denver": "nuggets",
    "pistons": "pistons", "detroit": "pistons",
    "warriors": "warriors", "golden state": "warriors",
    "rockets": "rockets", "houston": "rockets",
    "pacers": "pacers", "indiana": "pacers",
    "clippers": "clippers", "la clippers": "clippers",
    "lakers": "lakers", "los angeles lakers": "lakers", "la lakers": "lakers",
    "grizzlies": "grizzlies", "memphis": "grizzlies",
    "heat": "heat", "miami": "heat",
    "bucks": "bucks", "milwaukee": "bucks",
    "timberwolves": "timberwolves", "wolves": "timberwolves", "minnesota": "timberwolves",
    "pelicans": "pelicans", "new orleans": "pelicans",
    "knicks": "knicks", "new york": "knicks",
    "thunder": "thunder", "okc": "thunder", "oklahoma": "thunder",
    "magic": "magic", "orlando": "magic",
    "76ers": "76ers", "sixers": "76ers", "philadelphia": "76ers",
    "suns": "suns", "phoenix": "suns",
    "trail blazers": "trail blazers", "blazers": "trail blazers", "portland": "trail blazers",
    "kings": "kings", "sacramento": "kings",
    "spurs": "spurs", "san antonio": "spurs",
    "raptors": "raptors", "toronto": "raptors",
    "jazz": "jazz", "utah": "jazz",
    "wizards": "wizards", "washington": "wizards",
    # MLB
    "yankees": "yankees", "new york yankees": "yankees",
    "red sox": "red sox", "boston red sox": "red sox",
    "dodgers": "dodgers", "los angeles dodgers": "dodgers",
    "cubs": "cubs", "chicago cubs": "cubs",
    "astros": "astros", "houston astros": "astros",
    "braves": "braves", "atlanta braves": "braves",
    "mets": "mets", "new york mets": "mets",
    "cardinals": "cardinals", "st. louis": "cardinals",
    "giants": "giants", "san francisco giants": "giants",
    "phillies": "phillies", "philadelphia phillies": "phillies",
    "rays": "rays", "tampa bay": "rays",
    "blue jays": "blue jays", "toronto blue jays": "blue jays",
}

# ── 系列賽過濾 ──────────────────────────────────────────────────────────────
# 標題或子市場問題中含有這些關鍵字 → 系列賽事件，應跳過
SERIES_KEYWORDS = [
    "series", "advance", "who will win the series",
    "conference finals", "conference semifinals",
    "first round", "nba finals winner", "nba champion",
    "who wins the series", "nba finals", "eastern conference",
    "western conference",
]

# 若標題含有 "Game N"（例如 "Game 2"），則確定是單場賽事 → 保留
SINGLE_GAME_RE = re.compile(r'\bgame\s+\d+\b', re.IGNORECASE)

# ── 二元市場驗證 ─────────────────────────────────────────────────────────────
# outcomePrices[0] + outcomePrices[1] 必須在 [0.95, 1.05] 之間
BINARY_SUM_TOLERANCE = 0.05

# 兩側差距超過此值時，提示可能有傷兵/即時新聞
LARGE_DISCREPANCY_THRESHOLD = 0.15

# 台灣時間偏移 UTC+8
TWD_OFFSET = timedelta(hours=8)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_gmail(message):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("[Gmail] GMAIL_USER or GMAIL_APP_PASSWORD not set, skipping notification.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "🏀 NBA套利警報"
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_TO
        msg.attach(MIMEText(message, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())
        print(f"[Gmail] Notification sent to {GMAIL_TO}.")
    except Exception as e:
        print(f"[Gmail] Failed: {e}")


def fetch_polymarket_events():
    all_events = []
    for slug in POLYMARKET_SLUGS:
        url = POLYMARKET_BASE.format(slug=slug)
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            events = resp.json()
            print(f"[Polymarket] {slug.upper()}: {len(events)} events fetched")
            all_events.extend(events)
        except Exception as e:
            print(f"[Polymarket] Fetch error ({slug}): {e}")
    return all_events


def parse_team_name(title):
    """Try to extract home/away teams from market title like 'Team A vs Team B'."""
    # Strip leading context like "NBA Playoffs: Who Will Win Series? - "
    clean = re.sub(r'^.*[-–]\s*', '', title).strip()
    for sep in [" vs. ", " vs ", " VS ", " v. ", " v ", " V "]:
        if sep in clean:
            parts = clean.split(sep, 1)
            home = re.sub(r"\s*[''']?\s*$", "", parts[0]).strip()
            away = re.sub(r"\s*[''']?\s*$", "", parts[1]).strip()
            if home and away:
                return home, away
    return None, None


def normalize_name(name):
    """Normalize team/player name using alias table, fallback to lowercase."""
    n = name.lower().strip()
    if n in TEAM_ALIASES:
        return TEAM_ALIASES[n]
    for alias, canonical in TEAM_ALIASES.items():
        if alias in n or n in alias:
            return canonical
    return n


def match_manual_odds(home, away, manual_odds):
    """Find matching entry in manual_odds.json using normalized names."""
    home_n = normalize_name(home)
    away_n = normalize_name(away)
    for entry in manual_odds:
        mh = normalize_name(entry.get("home", ""))
        ma = normalize_name(entry.get("away", ""))
        if home_n == mh and away_n == ma:
            return entry
        if home_n == ma and away_n == mh:
            return {"home": entry["away"], "away": entry["home"],
                    "home_odds": entry["away_odds"], "away_odds": entry["home_odds"]}
    return None


def hours_until(start_time_str):
    """Parse ISO8601 start time and return hours until game."""
    try:
        dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = dt - now
        return delta.total_seconds() / 3600
    except Exception:
        return None


def is_series_event(event):
    """
    Return True if this Polymarket event is a series/playoff-advancement market,
    NOT a single game. Should be called before parse_team_name.

    Logic (order matters):
    1. If title contains 'Game N' (e.g. 'Game 2') → single game, return False.
    2. If title contains any SERIES_KEYWORDS → series, return True.
    3. If any market question contains SERIES_KEYWORDS → series, return True.
    4. Otherwise → keep (return False).
    """
    title = event.get("title", "").lower()

    # Step 1: Explicit single-game marker overrides everything
    if SINGLE_GAME_RE.search(title):
        return False

    # Step 2: Series keyword in event title
    for kw in SERIES_KEYWORDS:
        if kw in title:
            return True

    # Step 3: Series keyword in any sub-market question
    for market in event.get("markets", []):
        question = (market.get("question") or "").lower()
        for kw in SERIES_KEYWORDS:
            if kw in question:
                return True

    return False


def find_binary_game_market(markets):
    """
    Find the best single-game binary winner market from a list of Polymarket markets.

    Qualification:
    - Exactly 2 outcome prices
    - Both prices in (0.01, 0.99) — rejects degenerate series-winner prices like 0.989
    - Prices sum to 1.0 ± BINARY_SUM_TOLERANCE
    - Market question does not contain SERIES_KEYWORDS

    Returns the highest-volume qualifying market as:
        {"home_prob": float, "away_prob": float, "volume": float,
         "question": str, "url": str or None}
    or None if no qualifying market found.
    """
    best = None
    best_volume = -1.0

    for market in markets:
        # Parse outcomePrices (may be a JSON string or already a list)
        raw_prices = market.get("outcomePrices")
        if raw_prices is None:
            continue
        if isinstance(raw_prices, str):
            try:
                prices = json.loads(raw_prices)
            except (ValueError, TypeError):
                continue
        else:
            prices = raw_prices

        if len(prices) != 2:
            continue

        try:
            p0 = float(prices[0])
            p1 = float(prices[1])
        except (ValueError, TypeError):
            continue

        # Reject degenerate prices (e.g. series winner at 0.989 / 0.011)
        if not (0.01 < p0 < 0.99 and 0.01 < p1 < 0.99):
            continue

        # Prices must sum to ~1.0 (same binary market)
        total = p0 + p1
        if not (1.0 - BINARY_SUM_TOLERANCE <= total <= 1.0 + BINARY_SUM_TOLERANCE):
            continue

        # Reject if the market question itself is a series question
        question = (market.get("question") or "").lower()
        is_series_q = any(kw in question for kw in SERIES_KEYWORDS)
        if is_series_q:
            continue

        try:
            vol = float(market.get("volume") or 0)
        except (ValueError, TypeError):
            vol = 0.0

        if vol > best_volume:
            best_volume = vol
            best = {
                "home_prob": p0,
                "away_prob": p1,
                "volume": vol,
                "question": market.get("question", ""),
                "url": market.get("url") or None,
            }

    return best


def compute_edge(market_result, manual):
    """
    Compute arbitrage edge for both home and away sides.

    home_edge = Polymarket_home_prob - (1 / home_lottery_odds)
    away_edge = Polymarket_away_prob - (1 / away_lottery_odds)

    Both are computed independently. The side with the larger edge is the
    recommended bet. A positive edge means Polymarket believes the team
    is more likely to win than the lottery odds imply.
    """
    home_poly = market_result["home_prob"]
    away_poly = market_result["away_prob"]

    home_lottery = 1.0 / manual["home_odds"]
    away_lottery = 1.0 / manual["away_odds"]

    home_edge = home_poly - home_lottery
    away_edge = away_poly - away_lottery

    if home_edge >= away_edge:
        best_side = "home"
        best_edge = home_edge
    else:
        best_side = "away"
        best_edge = away_edge

    return {
        "home_poly_prob":    home_poly,
        "away_poly_prob":    away_poly,
        "home_lottery_prob": home_lottery,
        "away_lottery_prob": away_lottery,
        "home_edge":         home_edge,
        "away_edge":         away_edge,
        "best_edge":         best_edge,
        "best_side":         best_side,
        "large_discrepancy": abs(best_edge) > LARGE_DISCREPANCY_THRESHOLD,
    }


def format_alert(title, start_time, hours_left, edge_result, manual, market_result, stake_twd):
    """Build the full alert message with TWD time, side-by-side odds, and warnings."""
    # Convert to Taiwan local time (UTC+8)
    try:
        dt_utc = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        dt_twd = dt_utc + TWD_OFFSET
        time_str = dt_twd.strftime("%Y-%m-%d %H:%M TWD")
    except Exception:
        time_str = start_time

    is_home = edge_result["best_side"] == "home"
    side_zh   = "主隊" if is_home else "客隊"
    side_name = manual["home"] if is_home else manual["away"]
    bet_odds  = manual["home_odds"] if is_home else manual["away_odds"]
    win_amount = round(stake_twd * bet_odds - stake_twd, 0)

    url_line = f"Polymarket連結：{market_result['url']}\n" if market_result.get("url") else ""
    discrepancy_line = "⚠️ 大幅差距 - 可能有傷兵/即時新聞\n" if edge_result["large_discrepancy"] else ""

    msg = (
        f"\n🏀 NBA套利警報\n"
        f"比賽：{title}\n"
        f"開賽：{time_str}（{hours_left:.1f}小時後）\n"
        f"\n"
        f"── Polymarket 勝率 ──\n"
        f"主隊 {manual['home']}：{edge_result['home_poly_prob']*100:.1f}%\n"
        f"客隊 {manual['away']}：{edge_result['away_poly_prob']*100:.1f}%\n"
        f"\n"
        f"── 台彩隱含機率 ──\n"
        f"主隊 {manual['home']}：{edge_result['home_lottery_prob']*100:.1f}%  (賠率 {manual['home_odds']})\n"
        f"客隊 {manual['away']}：{edge_result['away_lottery_prob']*100:.1f}%  (賠率 {manual['away_odds']})\n"
        f"\n"
        f"── 優勢分析 ──\n"
        f"主隊優勢：{edge_result['home_edge']*100:+.1f}%\n"
        f"客隊優勢：{edge_result['away_edge']*100:+.1f}%\n"
        f"\n"
        f"建議：下注{side_zh}（{side_name}），優勢 {edge_result['best_edge']*100:+.1f}%\n"
        f"市場規模：${market_result['volume']:,.0f}\n"
        f"投注金額：{stake_twd} TWD（預估獲利 {win_amount:.0f} TWD）\n"
        f"{url_line}"
        f"{discrepancy_line}"
        f"市場題目：{market_result['question']}"
    )
    return msg


def main():
    now_str = datetime.now(timezone.utc).isoformat()
    print(f"=== NBA Arbitrage Scan @ {now_str} ===\n")

    manual_odds  = load_json(MANUAL_ODDS_FILE, [])
    sent_alerts  = load_json(SENT_ALERTS_FILE, {})
    events       = fetch_polymarket_events()

    if not events:
        print("No events fetched from Polymarket.")
        return

    alerts_triggered = 0

    for event in events:
        title      = event.get("title", "")
        start_time = event.get("startTime") or event.get("startDate") or ""
        markets    = event.get("markets", [])

        # ── GATE 1: Reject series/advancement events ──────────────────────────
        if is_series_event(event):
            print(f"[SERIES SKIP] {title}")
            continue

        # ── GATE 2: Parse team names ───────────────────────────────────────────
        home, away = parse_team_name(title)
        if not home or not away:
            print(f"[SKIP] Cannot parse teams from: '{title}'")
            continue

        # ── GATE 3: Find a valid single-game binary market ────────────────────
        market_result = find_binary_game_market(markets)
        if market_result is None:
            print(f"[SKIP] No valid binary game market: '{title}'")
            continue

        # ── GATE 4: Time filter ────────────────────────────────────────────────
        hours_left = hours_until(start_time) if start_time else None
        if hours_left is None:
            print(f"[SKIP] No parseable start time: '{title}'")
            continue

        cond_time = 0 <= hours_left <= MAX_HOURS_TO_START

        # ── GATE 5: Lottery odds lookup ───────────────────────────────────────
        manual = match_manual_odds(home, away, manual_odds)

        # ── Diagnostics printout (always shown) ───────────────────────────────
        print(f"Game : {title}")
        print(f"  Teams   : {home} vs {away}")
        print(f"  Start   : {start_time}  ({hours_left:.1f}h away)")
        print(f"  Market  : '{market_result['question']}'")
        print(f"  Poly    : home={market_result['home_prob']*100:.1f}%  away={market_result['away_prob']*100:.1f}%")
        print(f"  Volume  : ${market_result['volume']:,.0f}")

        if not manual:
            print(f"  Lottery : no match in {MANUAL_ODDS_FILE}")
            print(f"  [NO ALERT] No lottery match.\n")
            continue

        # ── GATE 6: Edge & threshold conditions ───────────────────────────────
        edge_result = compute_edge(market_result, manual)

        print(f"  Lottery : home {manual['home_odds']} ({edge_result['home_lottery_prob']*100:.1f}%) "
              f"/ away {manual['away_odds']} ({edge_result['away_lottery_prob']*100:.1f}%)")
        print(f"  Edge    : home {edge_result['home_edge']*100:+.1f}%  away {edge_result['away_edge']*100:+.1f}%")

        best_prob    = max(market_result["home_prob"], market_result["away_prob"])
        best_edge    = edge_result["best_edge"]
        total_volume = market_result["volume"]

        cond_prob   = best_prob    >= MIN_PROB
        cond_edge   = best_edge    >= MIN_EDGE
        cond_volume = total_volume >= MIN_VOLUME

        print(f"  Conditions: prob>={MIN_PROB*100:.0f}%={cond_prob}  "
              f"edge>={MIN_EDGE*100:.0f}%={cond_edge}  "
              f"vol>=${MIN_VOLUME:,}={cond_volume}  "
              f"hours<={MAX_HOURS_TO_START}={cond_time}")

        if not (cond_prob and cond_edge and cond_volume and cond_time):
            print(f"  [NO ALERT] Conditions not met.\n")
            continue

        # ── Cooldown check ─────────────────────────────────────────────────────
        alert_key = f"{title}_{start_time}"
        last_sent = sent_alerts.get(alert_key)
        now_utc   = datetime.now(timezone.utc)

        if last_sent:
            last_dt = datetime.fromisoformat(last_sent)
            if (now_utc - last_dt) < timedelta(hours=COOLDOWN_HOURS):
                elapsed_min = (now_utc - last_dt).seconds // 60
                print(f"  [COOLDOWN] Alert already sent {elapsed_min} min ago, skipping.\n")
                continue

        # ── Send alert ─────────────────────────────────────────────────────────
        msg = format_alert(title, start_time, hours_left, edge_result, manual, market_result, STAKE_TWD)
        print(f"  *** ALERT TRIGGERED ***\n{msg}\n")
        send_gmail(msg)
        sent_alerts[alert_key] = now_utc.isoformat()
        alerts_triggered += 1

    save_json(SENT_ALERTS_FILE, sent_alerts)
    print(f"\n=== Scan complete. {alerts_triggered} alert(s) triggered. ===")


if __name__ == "__main__":
    main()
