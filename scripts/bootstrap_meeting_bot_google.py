import asyncio
import argparse
import os
from contextlib import suppress
from pathlib import Path

from playwright.async_api import async_playwright
from playwright._impl._errors import TargetClosedError


PROFILE_DIR = Path(
    os.environ.get(
        "MEETING_BOT_PROFILE_DIR",
        str(Path(__file__).resolve().parent.parent / "storage" / "meeting_bot_recorder_profile"),
    )
)


async def main(profile_dir: Path):
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1440, "height": 1024},
            ignore_https_errors=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://meet.google.com/", wait_until="domcontentloaded", timeout=60000)

        print(f"Recorder profile directory: {profile_dir}")
        print("Sign into the shared Google recorder account in the opened browser window.")
        print("After the account is fully signed in and Meet loads, press Enter here to save and close.")
        print("If you already closed the browser window manually, pressing Enter will just finish cleanly.")
        input()

        with suppress(TargetClosedError):
            await context.close()

        print("Recorder bootstrap finished.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sign a Google Meet recorder account into a Playwright profile.")
    parser.add_argument(
        "--profile-dir",
        default=str(PROFILE_DIR),
        help="Profile directory to create/update. Defaults to MEETING_BOT_PROFILE_DIR or the shared recorder profile.",
    )
    args = parser.parse_args()
    asyncio.run(main(Path(args.profile_dir).resolve()))
