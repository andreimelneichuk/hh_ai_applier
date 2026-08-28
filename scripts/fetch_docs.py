import requests

url = "https://raw.githubusercontent.com/hhru/api/master/docs/negotiations.md"
r = requests.get(url)
with open("negotiations_full.md", "w") as f:
    f.write(r.text)
print("Saved negotiations_full.md, total length:", len(r.text))
