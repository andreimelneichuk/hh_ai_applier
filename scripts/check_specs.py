import urllib.request
import urllib.parse
import json
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

terms = ["машин", "backend", "бэкенд"]

for term in terms:
    q = urllib.parse.quote(term)
    urls = [
        f"https://career.habr.com/api/frontend/suggestions/specializations?term={q}",
        f"https://career.habr.com/api/frontend/suggestions/divisions?term={q}"
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, context=ctx) as response:
                print(f"URL: {url}")
                print(json.loads(response.read().decode()))
        except Exception as e:
            print(f"Error for {url}: {e}")
