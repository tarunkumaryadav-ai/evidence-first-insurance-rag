# Architecture

## Design goals

The system is designed to answer only when indexed evidence supports the response.
Its priorities are traceability, incremental updates, conservative routing, local
inference, and deterministic failure behavior.

## Ingestion path

1. The sitemap discovery step selects car, home, and business URLs and retains each
   source `lastmod` value.
2. The crawler enforces the configured user agent, robots rules, crawl delay,
   response limits, retry policy, and allowed-domain checks.
3. Main-content extraction removes navigation and page furniture while preserving
   headings and meaningful HTML structure.
4. Tag-aware chunking builds bounded text units and attaches product, category,
   variant, optional sub-product, URL, content hash, and source dates.
5. SQLite stores the source manifest, active revisions, original chunk text, and
   keyword-search representation. Qdrant stores the corresponding embeddings.
6. A revision becomes active only after its complete replacement is available,
   preventing a partial refresh from silently replacing good evidence.

## Question path

1. Rule-based routing infers a product only when the question is sufficiently clear.
2. Ambiguous or conflicting requests return a clarification before retrieval.
3. The LangGraph workflow retrieves semantic and lexical candidates, merges them,
   and applies product and metadata constraints.
4. Freshness, score, evidence, and model-compatibility gates can stop the workflow.
5. The local Qwen model receives only bounded evidence and an explicit instruction
   to avoid unsupported claims.
6. The API returns answer status, cited passages, exact source URLs, and metadata.
   Failed evidence checks produce an abstention rather than a plausible guess.

## Why LangChain and LangGraph

LangChain supplies maintained adapters for Ollama and Qdrant. LangGraph makes the
workflow state and branching explicit: clarify, retrieve, validate, answer, or
abstain. A hand-written state machine could replace LangGraph, while direct Ollama
and Qdrant clients could replace LangChain. The framework choices reduce integration
code without delegating business rules to an autonomous agent.

## Storage modes

- **Local development:** embedded Qdrant plus a local SQLite manifest.
- **Service mode:** external Qdrant configured with `RAG_QDRANT_URL`; SQLite remains
  the lexical index and document manifest for this implementation.

The embedded mode should be owned by one process. Production deployments should use
external Qdrant, persistent storage, API authentication, TLS at the reverse proxy,
and one ingestion coordinator.

## Security and privacy boundaries

- Crawled HTML, vectors, SQLite files, imports, exports, `.env`, and model weights
  are excluded from Git.
- URLs are constrained to the allowed HTTPS origin and redirects are revalidated.
- XML parsing uses `defusedxml`; response and document sizes are bounded.
- Optional API-key authentication protects non-local deployments.
- Retrieved website text is treated as untrusted data, not as executable instructions.
- Conversations are bounded and stored only in process memory for the configured TTL.

## Known limitations

- HTML ingestion does not automatically cover policy PDFs.
- Product routing is deliberately conservative and is not a general intent model.
- Local CPU generation can be slow for broad questions.
- Source `lastmod` and fetch time do not prove a policy's legal effective date.
- Evaluation records demonstrate the tested snapshot, not permanent website accuracy.
