# Validation: 11 November 2026

- Offline suite: 115 tests passed, covering extraction, XML/origin checks, filters,
  active revisions, source age, global scope, API auth/errors and model output validation.
- `pip check`: no broken requirements.
- Python compilation: app and tools modules compiled successfully.
- Ollama doctor: installed embedding and LLM models are reachable.
- Real Ollama + Qdrant + LangChain + LangGraph smoke test: Mistral selected the
  supplied example passage for a supported question and abstained when asked for
  a premium absent from the passage. Temporary synthetic data was removed.
- Llama3.2 failed these checks with both the original list-valued schema and the
  scalar selector: it was not retained as the default model.
- Empty real index: question correctly abstained and did not invent a citation.
- Live source access: `aal_seo` returned HTTP 200 for robots.txt, sitemap.xml and
  all three landing pages. Discovery found 512 total entries, 192 in-scope URLs
  (75 car, 60 home, 57 business), all with lastmod. Full ingestion indexed all
  192 pages with zero errors. Earlier user agents returned HTTP 403.
- Final index: 2703 chunks across 57 business pages (660 chunks), 75 car pages
  (1188 chunks) and 60 home pages (855 chunks).
- Targeted refreshes corrected and verified the real Essentials, building,
  contents and home-and-contents variant URLs. Each refresh completed without errors.
- Final CLI question against the complete index answered the payment FAQ with
  its full condition, source URL, sitemap lastmod and sitemap origin.
- Question-only requests now infer the product conservatively or ask for
  clarification before retrieval. Tests cover API schema examples, ambiguous and
  conflicting products, commercial vehicles, and inferred home-only retrieval/citations.

Real car-payment answer checks with 8000- and 3000-character selector budgets
timed out at 180 seconds on the local CPU. The default budget is now 3000,
preserving whole sections instead of truncating conditions. The longer-timeout
real snapshot check subsequently passed: the payment FAQ answered in 314.17
seconds and the unsupported personal-premium question abstained in 201.04 seconds
during concurrent ingestion (`data/live_answer_check.json`). These are not
acceptable interactive latency targets. The exact-FAQ shortcut returned the same
complete passage and source in 1.75 seconds (`data/live_faq_check.json`). Broader
semantic questions still use the slow local model.
The LLM now has a separate configurable timeout (default 600 seconds). Gemma 2B
was also tested but failed unsupported-question abstention and was not adopted.
Accessibility labels in comparison icons were checked against real car HTML,
preserved by the corrected extractor, and covered by a regression test. Older
extractor revisions are rejected by retrieval until reprocessed.

Not yet validated: human-reviewed extraction completeness across all pages, full product taxonomy,
retrieval/selection quality on a reviewed real question set, PDF policy documents,
Qdrant server deployment, load/latency targets, deployment backups and restores.
The dashboard and refresh scheduler are deferred.

## Local chat interface: 13 November 2026

Added a local typed chat interface at `/` and `/chat`; `/docs` remains available.
Conversation/API tests cover restoring pending questions after short product
replies, continuing a topic, explicit product switching, separate sessions,
expiry/capacity, in-flight request protection, reset, error recovery, authentication,
static assets and source links. JavaScript syntax and Python compilation passed.
No additional application dependencies were installed.
Existing system Playwright and Chrome passed desktop/mobile interaction checks:
sending, short clarification replies with the same session ID, source links,
safe text rendering, mobile overflow and New conversation. Browser requests used
isolated UI fixtures; backend/session behavior was tested separately against the
real graph and temporary vector-store fixtures. Screenshots are saved under
`data/chat-desktop.png` and `data/chat-mobile.png`.
The local server was restarted with the new interface. Live HTTP checks returned
200 for the homepage, clarification turn and source-backed continuation against
the actual knowledge index. The test conversation was cleared afterward.

## Startup troubleshooting: 15 November 2026

Confirmed an existing healthy chatbot on localhost:8000 while another instance
failed to acquire the embedded-store lock. Ollama and both models were reachable.
Added `python start.py` to reuse a healthy instance, reject an occupied/unhealthy
port, or launch the project environment. Lock errors now explain how to recover.
Launcher/lock regression tests passed. The live homepage and health endpoint
returned HTTP 200 after the changes.

## Qwen switch: 15 November 2026

Downloaded `qwen3:4b-instruct` with `ollama pull` (model ID 0edcdef34593,
approximately 2.5 GB). No Python dependency changes were needed. Mistral's existing
4.4 GB download is retained. The embedding model remains nomic-embed-text.

Qwen passed both synthetic evidence/abstention checks. With the exact-FAQ shortcut
disabled, it answered the real archived payment question in 70.58 seconds and
abstained on the unsupported personal-premium question in 66.31 seconds.
The report is `data/qwen3_4b_benchmark.json`. Earlier Mistral timings were 314.17
and 201.04 seconds during concurrent ingestion; this is not a controlled A/B run.
The new default is Qwen; model-selected answers are still not instant on this CPU.
All 91 offline tests passed after the switch. The server was restarted and its
live `/stats` response confirmed `qwen3:4b-instruct` with `nomic-embed-text:latest`.
The index retained all 192 pages and 2703 chunks, and the chat homepage returned 200.

## Clarification before retrieval: 15 November 2026

Added deterministic intent/incident clarification for vague car problems and
plan/topic clarification for generic coverage questions. Chat clarification turns
do not call Ollama or the knowledge store. Pending questions, user clarifications
and selected variants are carried into the eventual filtered retrieval. New-topic
questions can replace pending clarifications without mixing products.

All 115 tests passed, including API-level checks that neither vague-problem nor
incident clarification contacts Ollama. The live 'my car broke' turn returned its
intent choices in 0.044 seconds; 'Repair coverage' returned incident choices in
0.003 seconds. Qwen remains the active LLM for final evidence selection.
