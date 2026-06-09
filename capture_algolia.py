"""Перехватывает РЕАЛЬНЫЙ Algolia-запрос со страницы вакансий,
чтобы достать application id, search-only API key, имя индекса и payload."""
import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

START_URL = "https://sallinggroup.com/job/ledige-stillinger"
SAMPLES = Path(__file__).parent / "samples"
captured = []


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="da-DK")
        page = await context.new_page()

        def on_request(req):
            if "algolia" in req.url:
                captured.append({
                    "url": req.url,
                    "method": req.method,
                    "headers": dict(req.headers),
                    "post_data": req.post_data,
                })

        page.on("request", on_request)
        await page.goto(START_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        await browser.close()

    SAMPLES.mkdir(exist_ok=True)
    (SAMPLES / "algolia_request.json").write_text(
        json.dumps(captured, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Перехвачено algolia-запросов: {len(captured)}")
    for c in captured[:3]:
        print("\nURL:", c["url"])
        print("app_id header:", c["headers"].get("x-algolia-application-id"))
        print("api_key header:", c["headers"].get("x-algolia-api-key"))
        print("post_data:", (c["post_data"] or "")[:600])


if __name__ == "__main__":
    asyncio.run(main())
