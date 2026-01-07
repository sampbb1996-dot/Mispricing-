import yaml
import requests
import argparse

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

def extract_items(source):
    r = requests.get(source["url"], headers=HEADERS, timeout=20)
    r.raise_for_status()

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"URL did not return JSON: {source['url']}")

    items = data
    for part in source["list_path"].split("."):
        items = items[part]

    results = {}
    for item in items:
        title = item
        for part in source["title_field"].split("."):
            title = title[part]

        price = item
        for part in source["price_field"].split("."):
            price = price[int(part)] if part.isdigit() else price[part]

        results[title.lower()] = float(price)

    return results

def run_detector(det):
    a = extract_items(det["params"]["source_a"])
    b = extract_items(det["params"]["source_b"])

    for title in set(a) & set(b):
        pa, pb = a[title], b[title]
        low, high = min(pa, pb), max(pa, pb)

        abs_profit = high - low
        roi = abs_profit / low if low > 0 else 0

        if abs_profit >= det["params"]["min_abs_profit"] and roi >= det["params"]["min_roi"]:
            print(
                f"[ALERT] {title} | buy {low:.2f} sell {high:.2f} "
                f"| profit {abs_profit:.2f} ROI {roi:.2%}"
            )

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", default="long")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    for det in cfg["detectors"]:
        if det.get("enabled"):
            run_detector(det)

if __name__ == "__main__":
    main()
