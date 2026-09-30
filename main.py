from __future__ import annotations

from datetime import datetime
from html import escape
import os
import re
import sqlite3
import time
from typing import Any
from urllib.parse import quote

import pytz
import requests
from bs4 import BeautifulSoup, Tag

# توقيت الإمارات
UAE_TZ = pytz.timezone("Asia/Dubai")

# الإعدادات
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCRAPINGANT_KEY_NAMES = (
    "SCRAPINGANT_API_KEY",
    "SCRAPINGANT_API_KEY-A2",
    "SCRAPINGANT_API_KEY-A3",
    "SCRAPINGANT_API_KEY-A4",
    "SCRAPINGANT_API_KEY-M5",
    "SCRAPINGANT_API_KEY-M6",
    "SCRAPINGANT_API_KEY-M7",
)
SCRAPINGANT_API_KEYS = [
    (name, os.getenv(name)) for name in SCRAPINGANT_KEY_NAMES if os.getenv(name)
]
DB_FILE = os.getenv("DB_FILE", "sent_ads.db")
MAX_ADS_PER_RUN = int(os.getenv("MAX_ADS_PER_RUN", "5"))

TARGET_URL = (
    "https://ae.opensooq.com/ar/سيارات-ومركبات/سيارات-للبيع/تويوتا"
    "?search=true&sort_code=recent&ConditionUsed=4643"
)
BASE_URL = "https://ae.opensooq.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def now_uae() -> str:
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M:%S")


def text_of(node: Tag | None, default: str = "غير محدد") -> str:
    if not node:
        return default
    value = " ".join(node.get_text(" ", strip=True).split())
    return value or default


def fetch_direct(url: str) -> str | None:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ar,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        response = requests.get(url, headers=headers, timeout=45)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        print(f"[{now_uae()}] فشل الجلب المباشر: {exc}")
        return None


def fetch_with_fallback(url: str) -> str | None:
    """يجرب الجلب المباشر ثم مفاتيح ScrapingAnt بالتسلسل."""
    html = fetch_direct(url)
    if html:
        return html

    if not SCRAPINGANT_API_KEYS:
        print(f"[{now_uae()}] لا توجد مفاتيح ScrapingAnt مهيأة.")
        return None

    for name, api_key in SCRAPINGANT_API_KEYS:
        try:
            print(f"[{now_uae()}] محاولة الجلب عبر مفتاح {name}...")
            response = requests.get(
                "https://api.scrapingant.com/v2/general",
                params={"url": url, "x-api-key": api_key, "browser": "true"},
                timeout=90,
            )
            if response.ok and response.text:
                print(f"[{now_uae()}] نجح الجلب عبر مفتاح {name}.")
                return response.text
            print(
                f"[{now_uae()}] مفتاح {name} غير متاح أو استنفد حصته "
                f"(HTTP {response.status_code})، الانتقال للمفتاح التالي."
            )
        except requests.RequestException as exc:
            print(f"[{now_uae()}] خطأ في مفتاح {name}: {exc}، الانتقال للمفتاح التالي.")
        time.sleep(2)

    return None


def parse_card(card: Tag) -> dict[str, Any] | None:
    ad_id = card.get("data-id1")
    href = card.get("href", "")
    if not ad_id:
        match = re.search(r"/search/(\d+)", href)
        ad_id = match.group(1) if match else None
    if not ad_id:
        return None

    title = text_of(card.select_one("h2"), "تويوتا")
    price = text_of(card.select_one(".redColor"), "غير معلن")
    location = text_of(card.select_one(".locationText"), "الإمارات")
    image = card.find("img", src=True)
    image_url = image.get("src") if image else None

    specs = [text_of(span, "") for span in card.select(".starCpsItem span")]
    specs = [item for item in specs if item]
    year = next((item for item in specs if re.fullmatch(r"(?:19|20)\d{2}", item)), "غير محدد")
    km = next((item for item in specs if "كم" in item), "غير محدد")
    condition = next((item for item in specs if item in {"مستعمل", "جديد"}), "مستعمل")

    return {
        "id": str(ad_id),
        "title": title,
        "price": price,
        "location": location,
        "image": image_url,
        "year": year,
        "km": km,
        "condition": condition,
        "link": href if href.startswith("http") else f"{BASE_URL}{href}",
    }


def parse_ads(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("a.postListItemData[data-id1]")
    if not cards:
        cards = soup.select("a[data-id1][href*='/search/']")

    ads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        ad = parse_card(card)
        if ad and ad["id"] not in seen:
            seen.add(ad["id"])
            ads.append(ad)
        if len(ads) >= MAX_ADS_PER_RUN:
            break
    return ads


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS sent_ads (ad_id TEXT PRIMARY KEY)")
    conn.commit()
    return conn


def already_sent(conn: sqlite3.Connection, ad_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM sent_ads WHERE ad_id = ?", (ad_id,)).fetchone()
    return row is not None


def mark_sent(conn: sqlite3.Connection, ad_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sent_ads (ad_id) VALUES (?)", (ad_id,))
    conn.commit()


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        print("لم يتم الإرسال: TELEGRAM_BOT_TOKEN أو CHAT_ID غير موجود.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if not response.ok:
            print(f"فشل sendMessage: HTTP {response.status_code} {response.text[:300]}")
        return response.ok
    except requests.RequestException as exc:
        print(f"خطأ إرسال الرسالة: {exc}")
        return False


def send_telegram_photo(image_url: str, caption: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID, "photo": image_url, "caption": caption, "parse_mode": "HTML"},
            timeout=25,
        )
        if response.ok:
            return True
        print(f"فشل sendPhoto: HTTP {response.status_code} {response.text[:300]}")
    except requests.RequestException as exc:
        print(f"خطأ إرسال الصورة: {exc}")
    return send_telegram_message(caption)


def make_caption(ad: dict[str, Any]) -> str:
    # HTML أكثر أماناً من Markdown مع عناوين وروابط عربية.
    caption = (
        f"🚘 <b>إعلان جديد من السوق المفتوح - تويوتا مستعملة</b>\n\n"
        f"🚗 <b>السيارة:</b> {escape(ad['title'])}\n"
        f"💰 <b>السعر:</b> {escape(ad['price'])}\n"
        f"📅 <b>الموديل:</b> {escape(ad['year'])}\n"
        f"🛣️ <b>الممشى:</b> {escape(ad['km'])}\n"
        f"📍 <b>الموقع:</b> {escape(ad['location'])}\n"
        f"🔗 <a href=\"{escape(ad['link'], quote=True)}\">مشاهدة تفاصيل الإعلان</a>"
    )
    return caption[:1024]


def process_and_send() -> None:
    print(f"[{now_uae()}] بدء فحص السوق المفتوح...")
    html = fetch_with_fallback(TARGET_URL)
    if not html:
        print("تعذر جلب صفحة السوق المفتوح.")
        return

    ads = parse_ads(html)
    print(f"[{now_uae()}] تم العثور على {len(ads)} إعلان حديث.")
    conn = open_db()
    try:
        for ad in ads:
            if already_sent(conn, ad["id"]):
                print(f"الإعلان {ad['id']} مكرر.")
                continue

            caption = make_caption(ad)
            sent = send_telegram_photo(ad["image"], caption) if ad["image"] else send_telegram_message(caption)
            if sent:
                mark_sent(conn, ad["id"])
                print(f"[{now_uae()}] تم إرسال: {ad['title']}")
                time.sleep(2)
    finally:
        conn.close()


if __name__ == "__main__":
    process_and_send()
