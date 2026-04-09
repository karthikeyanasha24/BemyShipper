import os
import sys

import requests


def main() -> int:
    api_key = os.environ.get("SCRAPERAPI_KEY")
    if not api_key:
        print("Missing env var: SCRAPERAPI_KEY", file=sys.stderr)
        return 2

    url = (
        sys.argv[1]
        if len(sys.argv) >= 2
        else "https://www.sheinindia.in/shein-shein-drop-shoulder-spread-collar-short-shirt/p/443339230_white"
    )
    out_path = sys.argv[2] if len(sys.argv) >= 3 else "out.html"

    payload = {"api_key": api_key, "url": url}
    r = requests.get("https://api.scraperapi.com/", params=payload, timeout=60)

    with open(out_path, "wb") as f:
        f.write(r.content)

    print(f"Saved {len(r.content)} bytes to {out_path}")
    print(f"HTTP {r.status_code} {r.reason}")
    if r.status_code >= 400:
        print(
            "ScraperAPI returned an error. Double-check the product URL is real (not ending in '...') "
            "and try again; the error body was still saved to the output file.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
