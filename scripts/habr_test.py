import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        print("Navigating to login page...")
        await page.goto('https://account.habr.com/login/')
        
        print("Filling credentials...")
        await page.fill('input[type="email"]', 'andreimelneichuk@yandex.ru')
        await page.fill('input[type="password"]', 'wevkoq-zoshox-9kExha')
        
        print("Clicking submit...")
        await page.click('button[type="submit"], button.button_submit, button:has-text("Войти")')
        
        print("Waiting for navigation...")
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except Exception as e:
            print(f"Wait timeout: {e}")
            
        print("Navigating to career...")
        await page.goto('https://career.habr.com/resumes')
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except:
            pass
            
        print("Taking screenshot...")
        await page.screenshot(path='screenshot.png', full_page=True)
        
        print("Done!")
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
