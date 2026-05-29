"""
Deprecated module — kept as stub to avoid import errors in any external
code that may still reference monitor.ingest. All ingest logic moved into:

  - monitor.proxy             — captures X-Generation-Id from OpenRouter responses
  - monitor.openrouter_sync   — fetches billing data via /api/v1/generation?id=
  - monitor.main              — /api/ingest_text endpoint (prompts from Function)

Old per-model pricing JSON is no longer used; OpenRouter is the single source
of truth for tokens and cost.
"""
