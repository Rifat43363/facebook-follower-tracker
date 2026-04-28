import os
import re
import time
import random
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# =========================
# CONFIGURATION
# =========================

WEB_APP_URL = os.environ.get("WEB_APP_URL")
SECRET_KEY = os.environ.get("SECRET_KEY")

HEADLESS = True

MIN_DELAY_SECONDS = 5
MAX_DELAY_SECONDS = 10
PAGE_TIMEOUT_MS = 30000
LARGE_CHANGE_PERCENT = 20

DHAKA_TZ = ZoneInfo("Asia/Dhaka")

# How much Facebook page feed to inspect for recent posts.
MAX_ARTICLES_TO_CHECK = 20
SCROLL_ROUNDS = 4
SCROLL_WAIT_MS = 2500


# =========================
# GOOGLE SHEET CONNECTION
# =========================

def get_rows_from_google_sheet():
    response = requests.get(
        WEB_APP_URL,
        params={
            "action": "getRows",
            "secret": SECRET_KEY
        },
        timeout=60
    )

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Apps Script error: {data.get('error')}")

    return data.get("rows", [])


def send_results_to_google_sheet(results):
    response = requests.post(
        WEB_APP_URL,
        json={
            "secret": SECRET_KEY,
            "action": "updateResults",
            "results": results
        },
        timeout=120
    )

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Apps Script update error: {data.get('error')}")

    return data


# =========================
# BASIC CLEANING
# =========================

def normalize_url(url):
    url = str(url or "").strip()

    if not url:
        return ""

    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    return url


def clean_text(text):
    return " ".join(str(text or "").split())


def parse_existing_count(value):
    if value is None:
        return None

    text = str(value).strip().replace(",", "")

    if not text:
        return None

    match = re.search(r"\d+", text)

    if not match:
        return None

    return int(match.group(0))


def convert_compact_number(number_text, suffix):
    number_text = str(number_text).replace(",", "").strip()
    value = float(number_text)

    suffix = str(suffix or "").lower()

    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    elif suffix == "b":
        value *= 1_000_000_000

    return int(round(value))


# =========================
# FOLLOWER COUNT EXTRACTION
# =========================

def extract_follower_count(page_text):
    if not page_text:
        return None, ""

    text = clean_text(page_text)

    patterns = [
        r"([\d][\d,\.]*)\s*([KkMmBb]?)\s+followers\b",
        r"([\d][\d,\.]*)\s*([KkMmBb]?)\s+people\s+follow\s+this",
        r"Followed\s+by\s+([\d][\d,\.]*)\s*([KkMmBb]?)"
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            number_text = match.group(1)
            suffix = match.group(2)
            count = convert_compact_number(number_text, suffix)
            raw_text = match.group(0)
            return count, raw_text

    return None, ""


def detect_failure_reason(page_text):
    lower_text = str(page_text or "").lower()

    if "captcha" in lower_text or "security check" in lower_text:
        return "Blocked/Captcha"

    if "log in to facebook" in lower_text or "you must log in" in lower_text:
        return "Login required"

    if "this page isn't available" in lower_text or "content isn't available" in lower_text:
        return "Page unavailable"

    return "Follower count not found"


# =========================
# POST ACTIVITY EXTRACTION
# =========================

def parse_facebook_time(article_text):
    """
    Returns:
      {
        "found": bool,
        "within_24h": bool,
        "raw_time": str
      }

    This is heuristic because Facebook changes timestamp display frequently.
    It works best with public English Facebook page layouts.
    """

    text = clean_text(article_text)
    now = datetime.now(DHAKA_TZ)

    # Just now
    if re.search(r"\bjust now\b", text, re.IGNORECASE):
        return {
            "found": True,
            "within_24h": True,
            "raw_time": "Just now"
        }

    # Minutes ago: 5m, 5 min, 5 minutes
    match = re.search(
        r"\b(\d{1,3})\s*(m|min|mins|minute|minutes)\b",
        text,
        re.IGNORECASE
    )
    if match:
        minutes = int(match.group(1))
        return {
            "found": True,
            "within_24h": minutes <= 1440,
            "raw_time": match.group(0)
        }

    # Hours ago: 5h, 5 hr, 5 hours
    match = re.search(
        r"\b(\d{1,2})\s*(h|hr|hrs|hour|hours)\b",
        text,
        re.IGNORECASE
    )
    if match:
        hours = int(match.group(1))
        return {
            "found": True,
            "within_24h": hours <= 24,
            "raw_time": match.group(0)
        }

    # Day format: 1d, 2d
    # Conservative rule: do not count 1d as last 24h because it may be older than 24h.
    match = re.search(r"\b(\d{1,2})\s*d\b", text, re.IGNORECASE)
    if match:
        return {
            "found": True,
            "within_24h": False,
            "raw_time": match.group(0)
        }

    # Today at 4:30 PM
    match = re.search(
        r"\bToday\s+at\s+(\d{1,2}):(\d{2})\s*([AP]M)\b",
        text,
        re.IGNORECASE
    )
    if match:
        return {
            "found": True,
            "within_24h": True,
            "raw_time": match.group(0)
        }

    # Yesterday at 4:30 PM
    match = re.search(
        r"\bYesterday\s+at\s+(\d{1,2}):(\d{2})\s*([AP]M)\b",
        text,
        re.IGNORECASE
    )
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        am_pm = match.group(3).upper()

        if am_pm == "PM" and hour != 12:
            hour += 12
        if am_pm == "AM" and hour == 12:
            hour = 0

        yesterday_dt = (now - timedelta(days=1)).replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0
        )

        within_24h = (now - yesterday_dt) <= timedelta(hours=24)

        return {
            "found": True,
            "within_24h": within_24h,
            "raw_time": match.group(0)
        }

    # Month day at time: April 27 at 4:30 PM
    month_names = (
        "January|February|March|April|May|June|July|August|"
        "September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )

    match = re.search(
        rf"\b({month_names})\s+(\d{{1,2}})\s+at\s+(\d{{1,2}}):(\d{{2}})\s*([AP]M)\b",
        text,
        re.IGNORECASE
    )
    if match:
        raw_time = match.group(0)

        # We mark it as found, but only treat it as 24h if it also contains Today/Yesterday logic above.
        # This avoids unsafe assumptions across time zones and year boundaries.
        return {
            "found": True,
            "within_24h": False,
            "raw_time": raw_time
        }

    return {
        "found": False,
        "within_24h": False,
        "raw_time": ""
    }


def classify_post_type(article_locator, article_text):
    """
    Returns one of:
      reel_video
      static_image
      other

    This is heuristic. Facebook markup changes often.
    """

    text_lower = str(article_text or "").lower()

    try:
        html = article_locator.inner_html(timeout=3000).lower()
    except Exception:
        html = ""

    combined = text_lower + " " + html

    video_indicators = [
        "/reel/",
        "reel",
        "/videos/",
        "/watch/",
        "<video",
        "video"
    ]

    image_indicators = [
        "/photo/",
        "/photos/",
        "photos",
        "photo",
        "image"
    ]

    for indicator in video_indicators:
        if indicator in combined:
            return "reel_video"

    for indicator in image_indicators:
        if indicator in combined:
            return "static_image"

    return "other"


def scroll_page_for_posts(page):
    for _ in range(SCROLL_ROUNDS):
        try:
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(SCROLL_WAIT_MS)
        except Exception:
            break


def extract_post_activity(page):
    """
    Attempts to count recent public posts from visible Facebook feed articles.

    Returns:
      {
        postsLast24h,
        staticPostsLast24h,
        reelsVideosLast24h,
        otherPostsLast24h,
        latestPostTime,
        activitySummary,
        activityStatus,
        activityErrorReason
      }
    """

    activity = {
        "postsLast24h": 0,
        "staticPostsLast24h": 0,
        "reelsVideosLast24h": 0,
        "otherPostsLast24h": 0,
        "latestPostTime": "",
        "activitySummary": "",
        "activityStatus": "not_checked",
        "activityErrorReason": ""
    }

    try:
        scroll_page_for_posts(page)

        articles = page.locator('[role="article"]')
        article_count = articles.count()

        if article_count == 0:
            activity["activityStatus"] = "failed"
            activity["activityErrorReason"] = "No visible post articles found"
            activity["activitySummary"] = "No visible post articles found"
            return activity

        checked = 0
        seen_article_signatures = set()
        latest_time = ""

        for index in range(min(article_count, MAX_ARTICLES_TO_CHECK)):
            article = articles.nth(index)

            try:
                article_text = article.inner_text(timeout=4000)
            except Exception:
                continue

            article_text_clean = clean_text(article_text)

            if not article_text_clean:
                continue

            # Deduplicate similar repeated article blocks.
            signature = article_text_clean[:300]
            if signature in seen_article_signatures:
                continue
            seen_article_signatures.add(signature)

            checked += 1

            time_info = parse_facebook_time(article_text_clean)

            if time_info["found"] and not latest_time:
                latest_time = time_info["raw_time"]

            if not time_info["within_24h"]:
                continue

            post_type = classify_post_type(article, article_text_clean)

            activity["postsLast24h"] += 1

            if post_type == "reel_video":
                activity["reelsVideosLast24h"] += 1
            elif post_type == "static_image":
                activity["staticPostsLast24h"] += 1
            else:
                activity["otherPostsLast24h"] += 1

        activity["latestPostTime"] = latest_time

        activity["activityStatus"] = "success"

        activity["activitySummary"] = (
            f'{activity["postsLast24h"]} posts in last 24h; '
            f'{activity["staticPostsLast24h"]} static/image; '
            f'{activity["reelsVideosLast24h"]} reels/video; '
            f'{activity["otherPostsLast24h"]} other. '
            f'Latest visible post time: {latest_time or "Not found"}. '
            f'Checked {checked} visible article blocks.'
        )

        return activity

    except Exception as error:
        activity["activityStatus"] = "failed"
        activity["activityErrorReason"] = f"Post activity check failed: {str(error)[:120]}"
        activity["activitySummary"] = activity["activityErrorReason"]
        return activity


def empty_activity_result():
    return {
        "postsLast24h": "",
        "staticPostsLast24h": "",
        "reelsVideosLast24h": "",
        "otherPostsLast24h": "",
        "latestPostTime": "",
        "activitySummary": "",
        "activityStatus": "failed",
        "activityErrorReason": ""
    }


# =========================
# RESULT BUILDERS
# =========================

def build_success_result(row, current_followers, raw_follower_text, activity):
    previous_followers = parse_existing_count(row.get("oldFollowers"))

    if previous_followers is None:
        change_value = ""
        change_text = "First record"
        direction = "First record"
    else:
        change_value = current_followers - previous_followers

        if change_value > 0:
            change_text = f"↑ +{change_value}"
            direction = "Increased"
        elif change_value < 0:
            change_text = f"↓ {change_value}"
            direction = "Decreased"
        else:
            change_text = "No change"
            direction = "No change"

        if previous_followers > 0:
            percent_change = abs(change_value) / previous_followers * 100

            if percent_change >= LARGE_CHANGE_PERCENT:
                change_text = f"{change_text} | Large change - review"

    return {
        "rowNumber": row.get("rowNumber"),
        "pageName": row.get("pageName", ""),
        "pageLink": row.get("pageLink", ""),
        "previousFollowers": previous_followers if previous_followers is not None else "",
        "currentFollowers": current_followers,
        "change": change_value,
        "changeText": change_text,
        "direction": direction,
        "status": "success",
        "errorReason": "",
        "rawFollowerText": raw_follower_text,

        # New activity fields
        "postsLast24h": activity.get("postsLast24h", ""),
        "staticPostsLast24h": activity.get("staticPostsLast24h", ""),
        "reelsVideosLast24h": activity.get("reelsVideosLast24h", ""),
        "otherPostsLast24h": activity.get("otherPostsLast24h", ""),
        "latestPostTime": activity.get("latestPostTime", ""),
        "activitySummary": activity.get("activitySummary", ""),
        "activityStatus": activity.get("activityStatus", ""),
        "activityErrorReason": activity.get("activityErrorReason", "")
    }


def build_failed_result(row, reason, activity=None):
    previous_followers = parse_existing_count(row.get("oldFollowers"))
    activity = activity or empty_activity_result()

    return {
        "rowNumber": row.get("rowNumber"),
        "pageName": row.get("pageName", ""),
        "pageLink": row.get("pageLink", ""),
        "previousFollowers": previous_followers if previous_followers is not None else "",
        "currentFollowers": "",
        "change": "",
        "changeText": reason,
        "direction": "Failed",
        "status": "failed",
        "errorReason": reason,
        "rawFollowerText": "",

        # New activity fields
        "postsLast24h": activity.get("postsLast24h", ""),
        "staticPostsLast24h": activity.get("staticPostsLast24h", ""),
        "reelsVideosLast24h": activity.get("reelsVideosLast24h", ""),
        "otherPostsLast24h": activity.get("otherPostsLast24h", ""),
        "latestPostTime": activity.get("latestPostTime", ""),
        "activitySummary": activity.get("activitySummary", ""),
        "activityStatus": activity.get("activityStatus", ""),
        "activityErrorReason": activity.get("activityErrorReason", "")
    }


# =========================
# FACEBOOK CHECKER
# =========================

def check_facebook_page(page, row):
    page_name = row.get("pageName", "")
    page_link = normalize_url(row.get("pageLink", ""))

    print(f"Checking: {page_name} | {page_link}")

    if not page_link:
        return build_failed_result(row, "Missing link")

    if "facebook.com" not in page_link.lower() and "fb.com" not in page_link.lower():
        return build_failed_result(row, "Invalid URL")

    try:
        page.goto(
            page_link,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS
        )

        page.wait_for_timeout(7000)

        try:
            page_text_initial = page.locator("body").inner_text(timeout=10000)
        except Exception:
            page_text_initial = page.content()

        follower_count, raw_follower_text = extract_follower_count(page_text_initial)

        # Check recent visible post activity.
        activity = extract_post_activity(page)

        # After scrolling, try follower extraction again if first attempt failed.
        if follower_count is None:
            try:
                page_text_after_scroll = page.locator("body").inner_text(timeout=10000)
            except Exception:
                page_text_after_scroll = page.content()

            follower_count, raw_follower_text = extract_follower_count(page_text_after_scroll)

            if follower_count is None:
                reason = detect_failure_reason(page_text_after_scroll)
                return build_failed_result(row, reason, activity)

        return build_success_result(row, follower_count, raw_follower_text, activity)

    except PlaywrightTimeoutError:
        return build_failed_result(row, "Failed to fetch")

    except Exception as error:
        return build_failed_result(row, f"Failed to fetch: {str(error)[:100]}")


# =========================
# MAIN PROGRAM
# =========================

def main():
    if not WEB_APP_URL:
        raise RuntimeError("Missing WEB_APP_URL GitHub secret.")

    if not SECRET_KEY:
        raise RuntimeError("Missing SECRET_KEY GitHub secret.")

    print("Starting upgraded cloud Facebook Follower Tracker Agent...")

    rows = get_rows_from_google_sheet()

    if not rows:
        print("No rows found in Google Sheet.")
        return

    print(f"Rows found: {len(rows)}")

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="en-US"
        )

        page = context.new_page()

        for row in rows:
            result = check_facebook_page(page, row)
            results.append(result)

            print(
                f"Result: {result.get('pageName')} | "
                f"{result.get('status')} | "
                f"{result.get('changeText') or result.get('errorReason')} | "
                f"Activity: {result.get('activitySummary')}"
            )

            delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
            time.sleep(delay)

        browser.close()

    update_response = send_results_to_google_sheet(results)

    print("Google Sheet updated.")
    print(update_response)


if __name__ == "__main__":
    main()
