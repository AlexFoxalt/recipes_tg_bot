import json

import requests
from bs4 import BeautifulSoup

url = "https://moondeer.choiceqr.com/section:menyu"  # your real menu URL

response = requests.get(url)
soup = BeautifulSoup(response.text, "html.parser")

# ---- STEP 1: Extract JSON from <script id="__NEXT_DATA__"> ----
script_tag = soup.find("script", {"id": "__NEXT_DATA__", "type": "application/json"})
next_data = json.loads(script_tag.string)

# ---- STEP 2: Navigate to the menu list ----
menu_items = next_data["props"]["app"]["menu"]

# ---- STEP 3: Normalize each dish ----
recipes = []

for item in menu_items:
    name = item.get("name")
    description = item.get("description", "")
    price = item.get("price", 0) / 100  # convert 55000 -> 550.00
    weight = item.get("weight")
    weight_type = item.get("weightType")
    category = item.get("category")

    # extract first image
    image_url = None
    if item.get("media"):
        image_url = item["media"][0].get("url")

    media = item.get("media")
    if not media:
        image_url = None
    else:
        image_url = media[0]["url"]
    recipe = {
        "name": name,
        "recipe": description,
        "price": price,
        "weight": f"{weight}{weight_type}" if weight and weight_type else None,
        "image_url": image_url,
    }

    recipes.append(recipe)

# ---- STEP 4: Save to data.json ----
with open("recipes.json", "w", encoding="utf-8") as f:
    json.dump(recipes, f, ensure_ascii=False, indent=2)

print(f"Extracted {len(recipes)} dishes → data.json created.")
