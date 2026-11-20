"""Export readable chunk text and metadata without opening/locking Qdrant."""
import argparse
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Inspect stored RAG chunks as readable JSON")
    parser.add_argument("--database", type=Path, default=ROOT / "data/manifest.sqlite3")
    parser.add_argument("--product", choices=["car", "home", "business", "global"])
    parser.add_argument("--query", action="append", default=[], help="Text/heading search; repeat to match any term")
    parser.add_argument("--output", type=Path, default=ROOT / "data/chunks_export.json")
    args = parser.parse_args()
    conditions, parameters = [], []
    if args.product:
        conditions.append("json_extract(metadata, '$.product')=?")
        parameters.append(args.product)
    terms = [term.strip().lower() for term in args.query if term.strip()]
    if terms:
        conditions.append("(" + " OR ".join(
            "(instr(lower(body),?)>0 OR instr(lower(json_extract(metadata,'$.heading_path')),?)>0)"
            for _ in terms) + ")")
        for term in terms:
            parameters.extend([term, term])
    query = "SELECT id,url,body,metadata FROM chunks"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY url,rowid"
    database = args.database.resolve()
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        chunks = [{"chunk_id": row[0], "source_url": row[1], "text": row[2],
                   "metadata": json.loads(row[3])} for row in db.execute(query, parameters)]
    finally:
        db.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"chunks": len(chunks), "output": str(args.output.resolve()),
                      "bytes": args.output.stat().st_size}, indent=2))


if __name__ == "__main__":
    main()
