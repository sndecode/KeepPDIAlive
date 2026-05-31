#!/usr/bin/env python3
"""
notify.py
---------
Sends a cheeky Telegram alert about the PDI keepalive run.

Usage:
    python notify.py success
    python notify.py failure

How "days left" works:
    GitHub Actions is stateless, so we don't store the last login ourselves.
    Instead we ask GitHub for the most recent SUCCESSFUL run of this workflow,
    treat that as the last real login, and compute:
        days_left = 10 - (now - last_success)
    On success the timer just reset, so days_left = 10.
    On failure we compute the real number and escalate the tone as it shrinks.

Env vars:
    TELEGRAM_TOKEN     - bot token
    TELEGRAM_CHAT_ID   - your chat id
    GH_TOKEN           - GitHub token (the workflow passes the built-in one)
"""

import os
import sys
import glob
import json
import random
import datetime
import urllib.request

import requests

WINDOW_DAYS = 10                       # ServiceNow reclaim window
WORKFLOW_FILE = "keep-pdi-alive.yml"   # used to scope the run-history query
GITHUB_API = "https://api.github.com"


# --------------------------------------------------------------------------
# Cheeky message banks (English). One is picked at random each run so it
# never feels robotic.
# --------------------------------------------------------------------------
SUCCESS = [
    "✅ PDI is alive and kicking. 10 days of freedom unlocked — go write some glide script or take a nap. 😎",
    "✅ Logged in, timer reset. Your PDI will outlive your motivation this week. 🟢",
    "✅ Another silent rescue done. Instance safe for 10 more days. You're welcome. 🦸",
    "✅ Boom — signed in, instance secured. ServiceNow didn't even see me coming. 🥷",
    "✅ PDI status: immortal (for 10 days). Now go touch some grass. 🌱",
    "✅ Mission accomplished. 10-day clock back to full. Sleep easy, boss. 🛌",
]

FAIL_CHILL = [   # ~7-9 days left
    "⚠️ Login attempt failed — but chill, you've still got ~{d} days of buffer. I'll retry next run. 🛟",
    "⚠️ Hiccup while logging in. No panic, ~{d} days left on the clock. Probably just a flaky page. 🤞",
    "⚠️ Couldn't get in this time. Plenty of runway left (~{d} days). I'm keeping an eye on it. 👀",
]

FAIL_NUDGE = [   # ~3-6 days left
    "🟠 Login failed again and you're down to ~{d} days. Don't ignore this one — maybe log in manually soon. 🙏",
    "🟠 Still can't get in. ~{d} days before reclaim. Officially nudge territory now. ⏰",
    "🟠 Another miss. ~{d} days on the clock. Not red yet, but don't sleep on it, bro. 👇",
]

FAIL_PANIC = [   # < 3 days left
    "🚨 BROOO only ~{d} days left and I STILL can't log in. Go sign in manually RIGHT NOW or your PDI is toast. ⚰️",
    "🔴 RED ALERT: ~{d} days to reclaim and my automation is choking. Drop everything and log in yourself. 🆘",
    "🚨 Not a drill — ~{d} days left, login failing. Save your PDI manually before it's gone forever. 💀",
]

FAIL_UNKNOWN = [
    "⚠️ Login failed and I couldn't read your last successful login from GitHub history. Check the screenshot + log, and log in manually to be safe. 🤔",
]


# --------------------------------------------------------------------------
# GitHub run-history -> days left
# --------------------------------------------------------------------------
def last_success_time(repo, token):
    """Return the datetime of the most recent successful run, or None."""
    if not repo or not token:
        return None
    url = (f"{GITHUB_API}/repos/{repo}/actions/workflows/"
           f"{WORKFLOW_FILE}/runs?status=success&per_page=10")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "pdi-keepalive",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print("WARN: GitHub API query failed:", e)
        return None

    for run in data.get("workflow_runs", []):
        ts = run.get("updated_at") or run.get("created_at")
        if ts:
            return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return None


def compute_days_left(repo, token):
    last = last_success_time(repo, token)
    if last is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    days_since = (now - last).total_seconds() / 86400.0
    return max(0.0, WINDOW_DAYS - days_since)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def tg_message(token, chat_id, text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        print("sendMessage:", r.status_code)
    except Exception as e:
        print("sendMessage failed:", e)


def tg_photo(token, chat_id, path, caption=""):
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=60,
            )
        print("sendPhoto:", r.status_code)
    except Exception as e:
        print("sendPhoto failed:", e)


def pick_screenshot():
    if os.path.exists("failure.png"):
        return "failure.png"
    debugs = sorted(glob.glob("debug-*.png"), key=os.path.getmtime, reverse=True)
    return debugs[0] if debugs else None


# --------------------------------------------------------------------------
def main():
    status = (sys.argv[1] if len(sys.argv) > 1 else "success").lower()
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not token or not chat:
        print("ERROR: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing.")
        sys.exit(1)

    if status == "success":
        tg_message(token, chat, random.choice(SUCCESS))
        return

    # ---- failure path ----
    days_left = compute_days_left(repo, gh_token)
    if days_left is None:
        msg = random.choice(FAIL_UNKNOWN)
    else:
        d = int(days_left)  # floor -> conservative (shows fewer days)
        if days_left > 6:
            msg = random.choice(FAIL_CHILL).format(d=d)
        elif days_left >= 3:
            msg = random.choice(FAIL_NUDGE).format(d=d)
        else:
            msg = random.choice(FAIL_PANIC).format(d=d)
        print(f"days_left={days_left:.2f} -> shown as {d}")

    tg_message(token, chat, msg)

    img = pick_screenshot()
    if img:
        tg_photo(token, chat, img, caption=f"Last screenshot: {img}")
    else:
        tg_message(token, chat, "(No screenshot was captured for this run.)")


if __name__ == "__main__":
    main()
