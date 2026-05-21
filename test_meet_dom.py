import asyncio
from playwright.async_api import async_playwright

async def dump_meet_dom():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
        )
        context = await browser.new_context(
            permissions=["camera", "microphone"],
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.goto("https://meet.google.com/abc-defg-hij", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)
        
        html = await page.content()
        with open("meet_dom.html", "w", encoding="utf-8") as f:
            f.write(html)
            
        await browser.close()
        print("DOM saved to meet_dom.html")

if __name__ == "__main__":
    asyncio.run(dump_meet_dom())
