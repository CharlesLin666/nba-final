import os
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
POLYMARKET_API = "https://gamma-api.polymarket.com/events?tag_id=100639&active=true&closed=false&order=startTime&ascending=true&limit=50"

COOLDOWN_HOURS = 2
MIN_PROB = 0.80
MIN_EDGE = 0.06
MIN_VOLUME = 100000
MAX_HOURS_TO_START = 6


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
    try:
        resp = requests.get(POLYMARKET_API, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[Polymarket] Fetch error: {e}")
        return []


def parse_team_name(title):
    """Try to extract home/away teams from market title like 'Team A vs Team B'."""
    for sep in [" vs ", " VS ", " v ", " V "]:
        if sep in title:
            parts = title.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return None, None


def match_manual_odds(home, away, manual_odds):
    """Find matching entry in manual_odds.json (case-insensitive partial match)."""
    home_l = home.lower()
    away_l = away.lower()
    for entry in manual_odds:
        mh = entry.get("home", "").lower()
        ma = entry.get("away", "").lower()
        if (home_l in mh or mh in home_l) and (away_l in ma or ma in away_l):
            return entry
        if (away_l in mh or mh in away_l) and (home_l in ma or ma in home_l):
            # Swapped
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


def main():
    now_str = datetime.now(timezone.utc).isoformat()
    print(f"=== NBA Arbitrage Scan @ {now_str} ===\n")

    manual_odds = load_json(MANUAL_ODDS_FILE, [])
    sent_alerts = load_json(SENT_ALERTS_FILE, {})
    events = fetch_polymarket_events()

    if not events:
        print("No events fetched from Polymarket.")
        return

    alerts_triggered = 0

    for event in events:
        title = event.get("title", "")
        start_time = event.get("startTime") or event.get("startDate") or ""
        markets = event.get("markets", [])

        home, away = parse_team_name(title)
        if not home or not away:
            print(f"[SKIP] Cannot parse teams from: '{title}'")
            continue

        hours_left = hours_until(start_time) if start_time else None

        # Gather outcome probabilities and volumes from markets
        best_prob = None
        best_outcome = None
        total_volume = 0.0

        for market in markets:
            try:
                prob = float(market.get("lastTradePrice") or market.get("outcomePrices", [0])[0] or 0)
            except (TypeError, IndexError, ValueError):
                prob = 0.0
            try:
                vol = float(market.get("volume") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            total_volume += vol
            outcome = market.get("question") or market.get("outcome") or ""
            if best_prob is None or prob > best_prob:
                best_prob = prob
                best_outcome = outcome

        if best_prob is None:
            best_prob = 0.0

        # Try event-level volume if markets didn't have it
        if total_volume == 0:
            try:
                total_volume = float(event.get("volume") or 0)
            except (TypeError, ValueError):
                total_volume = 0.0

        print(f"Game : {title}")
        print(f"  Start   : {start_time}  ({f'{hours_left:.1f}h away' if hours_left is not None else 'unknown'})")
        print(f"  Best prob: {best_prob*100:.1f}%  ({best_outcome})")
        print(f"  Volume  : ${total_volume:,.0f}")

        # Look up Taiwan lottery odds
        manual = match_manual_odds(home, away, manual_odds)
        if manual:
            # Determine which side polymarket favours and get implied edge
            # Implied fair prob from lottery odds (1/odds, simplified)
            home_implied = 1 / manual["home_odds"]
            away_implied = 1 / manual["away_odds"]
            lottery_prob = home_implied if best_prob >= 0.5 else away_implied
            edge = best_prob - lottery_prob
            print(f"  Lottery : home {manual['home_odds']} / away {manual['away_odds']}  => edge {edge*100:.1f}%")
        else:
            edge = 0.0
            print(f"  Lottery : no match in {MANUAL_ODDS_FILE}")

        # Check all 4 conditions
        cond_prob   = best_prob >= MIN_PROB
        cond_edge   = edge >= MIN_EDGE
        cond_volume = total_volume >= MIN_VOLUME
        cond_time   = hours_left is not None and 0 <= hours_left <= MAX_HOURS_TO_START

        print(f"  Conditions: prob>={MIN_PROB*100:.0f}%={cond_prob}  edge>={MIN_EDGE*100:.0f}%={cond_edge}  "
              f"vol>=${MIN_VOLUME:,}={cond_volume}  hours<={MAX_HOURS_TO_START}={cond_time}")

        if cond_prob and cond_edge and cond_volume and cond_time:
            alert_key = f"{title}_{start_time}"
            last_sent = sent_alerts.get(alert_key)
            now_utc = datetime.now(timezone.utc)

            if last_sent:
                last_dt = datetime.fromisoformat(last_sent)
                if (now_utc - last_dt) < timedelta(hours=COOLDOWN_HOURS):
                    print(f"  [COOLDOWN] Alert already sent {(now_utc-last_dt).seconds//60} min ago, skipping.\n")
                    continue

            # Compute suggested stake
            win_amount = round(STAKE_TWD * (manual["home_odds"] if best_prob >= 0.5 else manual["away_odds"]) - STAKE_TWD, 0) if manual else 0
            side = "主隊" if best_prob >= 0.5 else "客隊"
            msg = (
                f"\n🏀 NBA套利警報\n"
                f"比賽：{title}\n"
                f"開賽：{start_time}（{hours_left:.1f}小時後）\n"
                f"建議：下注{side}\n"
                f"Polymarket勝率：{best_prob*100:.1f}%\n"
                f"優勢：{edge*100:.1f}%\n"
                f"市場規模：${total_volume:,.0f}\n"
                f"投注金額：{STAKE_TWD} TWD（預估獲利 {win_amount:.0f} TWD）"
            )
            print(f"  *** ALERT TRIGGERED ***\n{msg}\n")
            send_gmail(msg)
            sent_alerts[alert_key] = now_utc.isoformat()
            alerts_triggered += 1
        else:
            print(f"  [NO ALERT] Conditions not met.\n")

    save_json(SENT_ALERTS_FILE, sent_alerts)
    print(f"\n=== Scan complete. {alerts_triggered} alert(s) triggered. ===")


if __name__ == "__main__":
    main()
