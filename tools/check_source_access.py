"""Read-only diagnostic: no cookies, challenge solving, or credentials."""
import json
import argparse

import httpx
from app.config import Settings

URL = "https://www.allianz.com.au/sitemap.xml"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-agent", default=Settings().user_agent)
    args = parser.parse_args()
    response = httpx.get(URL, headers={"User-Agent": args.user_agent}, timeout=30, follow_redirects=False)
    print(json.dumps({"url": URL, "status": response.status_code,
                      "user_agent": args.user_agent,
                      "content_type": response.headers.get("content-type"),
                      "body_preview": response.text[:350]}, indent=2))
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
