import asyncio
import os
from contextlib import suppress
from playwright.async_api import async_playwright

_FILL_NAME_JS = """
(name) => {
    const candidates = [
        'input[jsname="YPqjbf"]',
        'input[aria-label="Your name"]',
        'input[placeholder="Your name"]',
        'input[aria-label*="name" i]',
        'input[placeholder*="name" i]',
        'input[type="text"]',
        'input:not([type])',
    ];
    const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    for (const sel of candidates) {
        const el = document.querySelector(sel);
        // Only target visible elements
        if (el && el.getBoundingClientRect().width > 0) {
            setter.call(el, name);
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
            el.focus();
            return sel;
        }
    }
    // Last resort: any visible input by bounding rect
    for (const el of document.querySelectorAll('input')) {
        const r = el.getBoundingClientRect();
        if (r.width > 50 && r.height > 10) {
            setter.call(el, name);
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
            el.focus();
            return 'input[rect-fallback]';
        }
    }
    return null;
}
"""

_CLICK_JOIN_BUTTON_JS = """
() => {
    const exact = /^(ask to join|join now|request to join|join meeting|join)$/i;
    const loose = /join/i;
    for (const pass of [exact, loose]) {
        for (const el of document.querySelectorAll('button, [role="button"]')) {
            const text  = (el.textContent || '').replace(/\\s+/g, ' ').trim();
            const label = (el.getAttribute('aria-label') || '').trim();
            if (pass.test(text) || pass.test(label)) {
                const r = el.getBoundingClientRect();
                if (r.width > 20 && r.height > 10) {
                    el.click();
                    return text || label;
                }
            }
        }
    }
    return null;
}
"""

async def test_meet():
    async with async_playwright() as pw:
        # We need headless=True to simulate production, but False to debug locally if we want.
        # Let's run headless=True and take screenshots to verify.
        browser = await pw.chromium.launch(
            headless=True,
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
        )
        context = await browser.new_context(
            permissions=["camera", "microphone"],
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        print("Navigating to meet.google.com/abc-defg-hij")
        await page.goto("https://meet.google.com/abc-defg-hij", wait_until="domcontentloaded", timeout=30000)
        
        # Wait for the lobby to load
        print("Waiting 5s for lobby...")
        await page.wait_for_timeout(5000)
        await page.screenshot(path="test_prejoin.png")
        
        # Try to dismiss popups
        for label in ["Got it", "Continue without microphone", "Continue without camera", "Dismiss"]:
            with suppress(Exception):
                await page.get_by_role("button", name=label, exact=True).click(timeout=1500)
        
        print("Filling name...")
        matched = await page.evaluate(_FILL_NAME_JS, "MinuteAI Recorder Test")
        print(f"JS fill returned: {matched}")
        await page.wait_for_timeout(2000)
        
        # Click join
        print("Clicking join...")
        clicked_text = await page.evaluate(_CLICK_JOIN_BUTTON_JS)
        print(f"JS join returned: {clicked_text}")
        
        await page.wait_for_timeout(3000)
        await page.screenshot(path="test_postjoin.png")
        
        await browser.close()
        print("Done. Check test_prejoin.png and test_postjoin.png.")

if __name__ == "__main__":
    asyncio.run(test_meet())
