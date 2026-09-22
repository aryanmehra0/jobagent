"""Inspect public Workday DOM only. Never clicks, uploads, logs in or submits."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('urls', nargs='+')
parser.add_argument('--output', default='data/outputs/validation/workday_readonly.json')
args = parser.parse_args()
from urllib.parse import urlparse
if len(args.urls) > 5 or any(not (urlparse(u).scheme == 'https' and
        (urlparse(u).hostname or '').endswith('.myworkdayjobs.com')) for u in args.urls):
    parser.error('Supply at most five public HTTPS Workday listing URLs.')
results = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    for url in args.urls:
        page = context.new_page()
        record = {'url': url, 'checked_at': datetime.now(timezone.utc).isoformat(), 'fields_filled': 0, 'submitted': False}
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=25000)
            try:
                page.wait_for_function("document.body.innerText.trim().length > 100", timeout=12000)
            except Exception:
                pass
            record.update(http_status=response.status if response else None, title=page.title(),
                          visible_text=page.locator('body').inner_text()[:500],
                          form_fields=page.locator('input:not([type=hidden]),select,textarea').count(),
                          workday_controls=page.locator('[data-automation-id]').count())
        except Exception as exc:
            record['error'] = type(exc).__name__
        results.append(record)
        page.close()
    browser.close()
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(results, indent=2), encoding='utf-8')
print(json.dumps(results, indent=2))
