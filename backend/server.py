"""
Amazon Reviews Scraper — FastAPI Backend (Cloud-Ready)
Серверная часть приложения для сбора отзывов с Amazon.
Поддерживает headless-режим для облачного развёртывания.
"""

import asyncio
import base64
import os
import re
import uuid
from collections import Counter
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Config (из переменных окружения) ───────────────────────────────────────
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")
PORT = int(os.getenv("PORT", "8000"))
USER_DATA_DIR = str(Path(__file__).parent / "playwright_profile")

# ─── App ────────────────────────────────────────────────────────────────────
app = FastAPI(title="Amazon Reviews Scraper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── State ──────────────────────────────────────────────────────────────────
scrape_jobs: dict = {}

STAR_FILTERS = {
    "all": None,
    "5_star": "five_star",
    "4_star": "four_star",
    "3_star": "three_star",
    "2_star": "two_star",
    "1_star": "one_star",
}


# ─── Models ─────────────────────────────────────────────────────────────────
class ScrapeRequest(BaseModel):
    url: str
    star_filter: str = "all"
    max_pages: int = 20


# ─── Helpers ────────────────────────────────────────────────────────────────
def extract_asin(url: str) -> str:
    """Извлекает ASIN из Amazon URL."""
    for pat in [r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})", r"/product-reviews/([A-Z0-9]{10})"]:
        m = re.search(pat, url, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    m = re.search(r"\b([A-Z0-9]{10})\b", url, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def detect_domain(url: str) -> str:
    """Определяет домен Amazon из URL."""
    m = re.search(r"(www\.amazon\.[a-z.]+)", url, re.IGNORECASE)
    return m.group(1) if m else "www.amazon.com"


def build_reviews_url(domain: str, asin: str, star_filter: Optional[str], page: int = 1) -> str:
    """Строит URL страницы отзывов."""
    base = f"https://{domain}/product-reviews/{asin}/"
    params = {"ie": "UTF8", "reviewerType": "all_reviews", "pageNumber": str(page)}
    if star_filter:
        params["filterByStar"] = star_filter
    return f"{base}?{urlencode(params)}"


async def safe_text(locator, timeout=3000):
    """Безопасное извлечение текста из async Playwright-локатора."""
    try:
        if await locator.count() > 0:
            return (await locator.first.inner_text(timeout=timeout)).strip()
    except Exception:
        pass
    return ""


def is_login_page(url: str) -> bool:
    u = url.lower()
    return "/ap/signin" in u or "signin" in u or "login" in u


async def is_captcha_page(page) -> bool:
    """Async-проверка CAPTCHA."""
    try:
        html = (await page.content()).lower()
        return (
            "captcha" in html
            or "sorry, we just need to make sure you're not a robot" in html
            or "enter the characters you see below" in html
        )
    except Exception:
        return False


async def take_screenshot_b64(page) -> str:
    """Делает скриншот страницы, возвращает base64 PNG."""
    try:
        buf = await page.screenshot(full_page=False)
        return base64.b64encode(buf).decode("utf-8")
    except Exception:
        return ""


async def click_next_page(page):
    """Нажимает кнопку Next. Пробует несколько стратегий."""
    old_url = page.url

    # 1) Классическая пагинация: li.a-last > a
    next_li = page.locator("li.a-last").first
    if await next_li.count() > 0:
        classes = ""
        try:
            classes = await next_li.get_attribute("class") or ""
        except Exception:
            pass
        if "a-disabled" not in classes:
            next_link = next_li.locator("a").first
            if await next_link.count() > 0:
                try:
                    await next_link.click()
                    await page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(3)
                    if page.url != old_url:
                        return True
                except Exception:
                    pass

    # 2) "Show more reviews" (EN + DE)
    for txt in ["Show 10 more reviews", "Show more reviews",
                 "Zeige 10 weitere Rezensionen", "Mehr Rezensionen anzeigen"]:
        btn = page.locator(f"text={txt}").first
        if await btn.count() > 0:
            try:
                await btn.click()
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(3)
                return True
            except Exception:
                continue

    return False


def parse_rating(text: str) -> Optional[float]:
    """Извлекает числовой рейтинг: '4.0 out of 5 stars' → 4.0"""
    for pattern in [r"(\d+[.,]\d+)\s+out of\s+5", r"(\d+[.,]\d+)\s+von\s+5"]:
        m = re.search(pattern, text)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def parse_date(text: str) -> str:
    """Извлекает дату: 'Reviewed in the US on January 16, 2026' → 'January 16, 2026'"""
    for pattern in [r"on\s+(.+)$", r"am\s+(.+)$"]:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return text


def compute_stats(reviews: list) -> dict:
    """Статистика и простой сентимент-анализ."""
    if not reviews:
        return {"total": 0}

    ratings = [r.get("rating_numeric") for r in reviews if r.get("rating_numeric")]
    star_counts = Counter(int(r) for r in ratings)

    positive_words = {"love", "great", "amazing", "excellent", "perfect", "wonderful",
                      "best", "fantastic", "awesome", "happy", "recommend",
                      "toll", "super", "wunderbar", "perfekt", "klasse"}
    negative_words = {"bad", "terrible", "awful", "worst", "broken", "waste",
                      "horrible", "disappointed", "poor", "defective",
                      "schlecht", "kaputt", "enttäuscht", "schrecklich"}

    pos = neg = 0
    for r in reviews:
        text = (r.get("body", "") + " " + r.get("title", "")).lower()
        if any(w in text for w in positive_words):
            pos += 1
        if any(w in text for w in negative_words):
            neg += 1

    return {
        "total": len(reviews),
        "average_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
        "star_distribution": {f"{k}_star": star_counts.get(k, 0) for k in range(1, 6)},
        "sentiment": {"positive": pos, "negative": neg, "neutral": len(reviews) - pos - neg},
    }


# ─── Scraping Logic ────────────────────────────────────────────────────────
async def extract_one_review(block, seen_ids: set):
    """Извлекает один отзыв из блока. Возвращает dict или None."""
    review_id = ""
    try:
        review_id = await block.get_attribute("id") or ""
    except Exception:
        pass

    if review_id and review_id in seen_ids:
        return None

    try:
        title = await safe_text(block.locator('[data-hook="review-title"]'))
        body = await safe_text(block.locator('[data-hook="review-body"]'))
        rating_text = await safe_text(block.locator('[data-hook="review-star-rating"]'))
        if not rating_text:
            rating_text = await safe_text(block.locator('[data-hook="cmps-review-star-rating"]'))
        date_text = await safe_text(block.locator('[data-hook="review-date"]'))
        helpful = await safe_text(block.locator('[data-hook="helpful-vote-statement"]'))
        author = await safe_text(block.locator('.a-profile-name'))

        if not review_id:
            review_id = f"{author}|{date_text}|{title[:60]}"

        if review_id in seen_ids:
            return None

        seen_ids.add(review_id)
        return {
            "title": title, "body": body, "rating_text": rating_text,
            "rating_numeric": parse_rating(rating_text),
            "date_raw": date_text, "date_clean": parse_date(date_text),
            "helpful": helpful, "author": author,
        }
    except Exception:
        return None


async def handle_block(job, page):
    """Обработка CAPTCHA/логина: скриншот + ожидание до 120 сек."""
    if not (is_login_page(page.url) or await is_captcha_page(page)):
        return True

    # Скриншот для пользователя (в headless-режиме не видно браузер)
    screenshot = await take_screenshot_b64(page)
    job["screenshot"] = screenshot
    job["status"] = "captcha_needed"
    job["progress"] = "CAPTCHA oder Login erkannt — siehe Screenshot unten. Bitte im Browser lösen und 'Weiter' klicken."

    for _ in range(60):
        await asyncio.sleep(2)
        if job.get("user_continue"):
            job["user_continue"] = False
            break

    if is_login_page(page.url) or await is_captcha_page(page):
        return False

    job["status"] = "running"
    job["screenshot"] = ""
    return True


async def run_scraper(job_id: str, url: str, star_filter_key: str, max_pages: int):
    """Основная логика сбора отзывов."""
    from playwright.async_api import async_playwright

    job = scrape_jobs[job_id]
    job["status"] = "running"
    job["progress"] = "Starte Browser..."
    job["reviews"] = []

    asin = extract_asin(url)
    domain = detect_domain(url)

    if not asin:
        job["status"] = "error"
        job["error"] = "ASIN konnte nicht aus der URL extrahiert werden."
        return

    job["asin"] = asin
    job["domain"] = domain
    star_filter_value = STAR_FILTERS.get(star_filter_key)
    seen_ids: set = set()

    try:
        async with async_playwright() as p:
            # Headless = Cloud, Non-headless = Local
            if HEADLESS:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    viewport={"width": 1400, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                )
            else:
                context = await p.chromium.launch_persistent_context(
                    USER_DATA_DIR,
                    headless=False,
                    viewport={"width": 1400, "height": 900},
                )
                browser = None

            page = await context.new_page()
            page.set_default_timeout(15000)

            # Seite 1 öffnen
            first_url = build_reviews_url(domain, asin, star_filter_value, page=1)
            job["progress"] = "Öffne Amazon Review-Seite..."
            await page.goto(first_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            if not await handle_block(job, page):
                job["status"] = "error"
                job["error"] = "Verifizierung nicht abgeschlossen."
                await context.close()
                if browser:
                    await browser.close()
                return

            # Hauptschleife
            consecutive_empty = 0

            for page_num in range(1, max_pages + 1):
                if job.get("cancel"):
                    job["progress"] = "Abgebrochen."
                    break

                job["progress"] = f"Seite {page_num}/{max_pages} wird geladen..."

                if page_num > 1:
                    moved = await click_next_page(page)
                    if not moved:
                        page_url = build_reviews_url(domain, asin, star_filter_value, page=page_num)
                        try:
                            await page.goto(page_url, wait_until="domcontentloaded")
                            await asyncio.sleep(3)
                        except Exception:
                            job["progress"] = f"Seite {page_num} konnte nicht geladen werden."
                            break

                if is_login_page(page.url) or await is_captcha_page(page):
                    if not await handle_block(job, page):
                        break

                try:
                    await page.wait_for_selector('[data-hook="review"]', timeout=8000)
                except Exception:
                    pass

                blocks = page.locator('[data-hook="review"]')
                count = await blocks.count()

                if count == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break
                    continue

                consecutive_empty = 0
                new_on_page = 0

                for i in range(count):
                    review = await extract_one_review(blocks.nth(i), seen_ids)
                    if review:
                        job["reviews"].append(review)
                        new_on_page += 1

                job["progress"] = f"Seite {page_num}: +{new_on_page} Reviews | Gesamt: {len(job['reviews'])}"

                if new_on_page == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break
                else:
                    consecutive_empty = 0

                await asyncio.sleep(1)

            await context.close()
            if browser:
                await browser.close()

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        return

    if job["status"] != "error":
        job["status"] = "completed"
        job["stats"] = compute_stats(job["reviews"])
        job["progress"] = f"Fertig! {len(job['reviews'])} Reviews gesammelt."


# ─── API Endpoints ──────────────────────────────────────────────────────────
@app.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    job_id = str(uuid.uuid4())[:8]
    scrape_jobs[job_id] = {
        "status": "starting", "progress": "Initialisiere...",
        "reviews": [], "error": None, "asin": "", "domain": "",
        "stats": {}, "cancel": False, "user_continue": False,
        "screenshot": "", "created_at": datetime.now().isoformat(),
    }
    asyncio.create_task(run_scraper(job_id, req.url, req.star_filter, req.max_pages))
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = scrape_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")
    return {
        "job_id": job_id, "status": job["status"], "progress": job["progress"],
        "total_reviews": len(job["reviews"]), "asin": job.get("asin", ""),
        "error": job.get("error"), "stats": job.get("stats", {}),
        "has_screenshot": bool(job.get("screenshot")),
    }


@app.get("/api/screenshot/{job_id}")
async def get_screenshot(job_id: str):
    """Возвращает скриншот CAPTCHA/логин-страницы как PNG."""
    job = scrape_jobs.get(job_id)
    if not job or not job.get("screenshot"):
        raise HTTPException(404, "Kein Screenshot verfügbar")
    img = base64.b64decode(job["screenshot"])
    return Response(content=img, media_type="image/png")


@app.post("/api/continue/{job_id}")
async def continue_job(job_id: str):
    job = scrape_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")
    job["user_continue"] = True
    return {"ok": True}


@app.post("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    job = scrape_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")
    job["cancel"] = True
    return {"ok": True}


@app.get("/api/reviews/{job_id}")
async def get_reviews(job_id: str, limit: int = 100, offset: int = 0):
    job = scrape_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")
    reviews = job["reviews"][offset:offset + limit]
    return {"total": len(job["reviews"]), "offset": offset, "limit": limit, "reviews": reviews}


@app.get("/api/export/{job_id}/csv")
async def export_csv(job_id: str):
    job = scrape_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")
    if not job["reviews"]:
        raise HTTPException(400, "Keine Reviews vorhanden")
    df = pd.DataFrame(job["reviews"])
    buf = StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="amazon_reviews_{job.get("asin","export")}.csv"'},
    )


@app.get("/api/export/{job_id}/xlsx")
async def export_xlsx(job_id: str):
    job = scrape_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")
    if not job["reviews"]:
        raise HTTPException(400, "Keine Reviews vorhanden")
    df = pd.DataFrame(job["reviews"])
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Reviews")
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="amazon_reviews_{job.get("asin","export")}.xlsx"'},
    )


@app.get("/api/stats/{job_id}")
async def get_stats(job_id: str):
    job = scrape_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")
    return compute_stats(job["reviews"])


# ─── Health Check (для Cloud-Plattformen) ───────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


# ─── Serve Frontend ─────────────────────────────────────────────────────────
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)


@app.get("/")
async def serve_index():
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "Amazon Reviews Scraper API läuft."}


app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
