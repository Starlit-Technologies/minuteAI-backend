import asyncio
from playwright.async_api import async_playwright

async def inspect_meet_new():
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
        print("Going to meet.google.com/new")
        await page.goto("https://meet.google.com/new", wait_until="domcontentloaded", timeout=30000)
        
        # We need to wait a bit for it to redirect to the new meeting URL
        try:
            await page.wait_for_url(lambda url: "meet.google.com/" in url and "new" not in url, timeout=10000)
        except Exception:
            pass
            
        print("URL after new:", page.url)
        await page.wait_for_timeout(5000)
        
        inputs = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('input')).map(el => ({
                type: el.type,
                placeholder: el.placeholder,
                ariaLabel: el.getAttribute('aria-label'),
                rect: el.getBoundingClientRect().toJSON()
            }));
        }''')
        print("INPUTS:", inputs)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(inspect_meet_new())
