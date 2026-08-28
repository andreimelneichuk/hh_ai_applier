import urllib.request
import urllib.parse
import json
import ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

my_skills = [
    "Machine Learning", "ML", "Artificial Intelligence", "NLP", 
    "Natural Language Processing", "Prompt", "Elasticsearch", "Data Science"
]

for skill in my_skills:
    url = f"https://career.habr.com/api/frontend/suggestions/skills?term={urllib.parse.quote(skill)}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ctx) as response:
            data = json.loads(response.read().decode())
            options = data.get("list", [])
            print(f"{skill}: {[o['title'] for o in options[:3]]}")
    except Exception as e:
        print(f"Error fetching {skill}: {e}")
