import urllib.request
import urllib.parse
import json
import time
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

my_skills = [
    "Python", "LLM", "RAG", "LangChain", "LangGraph", 
    "Prompt Engineering", "MCP", "AI Agents", "Transformers", 
    "BERT", "Fine-Tuning", "Asyncio", "FastAPI", "Apache Kafka", 
    "OpenSearch", "PostgreSQL", "Docker", "CI/CD", "Pytest"
]

matched = []
unmatched = []

for skill in my_skills:
    url = f"https://career.habr.com/api/frontend/suggestions/skills?term={urllib.parse.quote(skill)}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ctx) as response:
            data = json.loads(response.read().decode())
            options = data.get("list", [])
            
            exact = next((item for item in options if item["title"].lower() == skill.lower()), None)
            if exact:
                matched.append(exact["title"])
            elif len(options) > 0:
                matched.append(options[0]["title"])
            else:
                unmatched.append(skill)
    except Exception as e:
        print(f"Error fetching {skill}: {e}")
    
    time.sleep(0.5)

print("Matched skills:")
for s in matched:
    print(f"- {s}")

print("\nUnmatched skills:")
for s in unmatched:
    print(f"- {s}")
