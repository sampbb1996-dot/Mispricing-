import yaml
import requests
import argparse
import re

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

def extract_price_html(url, regex):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    m = re.search(regex, r.text)
    if not m:
        raise RuntimeError(f"Price not found at {url}")
    return float(m.group(1))

def run_detector(det):
    a = det["params"]["source_a"]
    b = det["params"]["source_b"]

    pa = extract_price_html(a["url"], a["price_regex"])
    pb = extract_price_html(b["url"], b["price_regex"])

    low, high = min(pa, pb), max(pa, pb)
    abs_profit = high - low
    roi = abs_profit / low if low > 0 else 0

    if abs_profit >= det["params"]["min_abs_profit"] and roi >= det["params"]["min_roi"]:
        print(
            f"[ALERT] {det['name']} | buy {low:.2f} sell {high:.2f} "
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
