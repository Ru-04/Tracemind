# Error fingerprint — v1 specification

Status: implemented in M1 (`backend/tracemind/fingerprint.py`). Deterministic; no LLM involved.

## Inputs

`project_id`, `service`, `environment` (all normalized), `error_type` (nullable), and a **signature** chosen as the first available of:

1. `frame:<last two path segments>:<function>` — the innermost frame of a **Python traceback** that lies outside `site-packages`, `dist-packages` and the standard library (falls back to the innermost frame). Path separators are normalized to `/` and only the last two segments are used, so the value is identical across machines and operating systems; line numbers are excluded.
2. `msg:<normalized message>` — when there is no parsable Python traceback. V1 parses Python tracebacks only; other languages group by message.

The message and stack trace are the **redacted** values.

## Message normalization (applied in this order)

1. Whitespace is collapsed and trimmed. Case is **not** changed (case can distinguish errors).
2. UUIDs → `<uuid>`
3. Hex strings of 8+ characters → `<hex>`
4. Quoted substrings (`"…"` or `'…'`) → `<str>`
5. Numbers (integers, decimals, optional leading `-`) → `<num>`
6. Redaction markers (`[REDACTED:…]`) are left as-is, so redaction never changes grouping.

## Hash

```
sha256( "v1|" + project_id + "|" + service + "|" + environment + "|" + (error_type or "-") + "|" + signature )
```

UTF-8, lower-case hex digest. A golden-value test (`tests/unit/test_fingerprint.py`) locks this format.

## Rules

- Same inputs ⇒ same fingerprint, on every run and machine.
- Different service or environment ⇒ different fingerprint (no cross-service merging).
- Missing `error_type` uses `-`; the fingerprint is still produced.
- Changing any rule requires bumping `FINGERPRINT_VERSION`; incidents are keyed by `(…, fingerprint, fingerprint_version)`, so old incidents keep their version and are not silently regrouped.
- Known limitations: messages that differ only in wording are *not* merged (over-splitting is preferred to merging unrelated failures, SOW §7.2). A literal `|` inside a message could in theory collide with the field separator; this requires crafted input and is accepted for V1.
