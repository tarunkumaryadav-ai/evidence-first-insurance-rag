"""Inspect retrieved evidence metadata without invoking the answer model."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.models import Question
from app.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--product", choices=["car", "home", "business"], required=True)
    args = parser.parse_args()
    store = Store(Settings())
    try:
        rows = store.retrieve(Question(question=args.question, product=args.product))
        print(json.dumps([{"id": row["id"], "text": row["text"][:500],
                           "metadata": row["metadata"]} for row in rows],
                         ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
