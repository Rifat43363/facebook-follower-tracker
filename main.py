import os
import re
import time
import random
import requests
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
# DATA CLEANING
# =========================

def normalize_url(url):
    url = str(url or "").strip()

    if not url:
        return ""

    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    return url


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


def extract_follower_count(page_text):
    if not page_text:
        return None, ""

    text = " ".join(page_text.split())

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
# RESULT BUILDERS
# =========================

def build_success_result(row, current_followers, raw_follower_text):
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
        "rawFollowerText": raw_follower_text
    }


def build_failed_result(row, reason):
    previous_followers = parse_existing_count(row.get("oldFollowers"))

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
        "rawFollowerText": ""
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
            page_text = page.locator("body").inner_text(timeout=10000)
        except Exception:
            page_text = page.content()

        follower_count, raw_follower_text = extract_follower_count(page_text)

        if follower_count is not None:
            return build_success_result(row, follower_count, raw_follower_text)

        reason = detect_failure_reason(page_text)
        return build_failed_result(row, reason)

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

    print("Starting cloud Facebook Follower Tracker Agent...")

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
                f"{result.get('changeText') or result.get('errorReason')}"
            )

            delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
            time.sleep(delay)

        browser.close()

    update_response = send_results_to_google_sheet(results)

    print("Google Sheet updated.")
    print(update_response)


if __name__ == "__main__":
    main()
