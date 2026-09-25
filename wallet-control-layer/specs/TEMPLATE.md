# Spec: <rule name>

**Status:** draft | agreed | implemented   **Owner:** <name>   **Reason codes:** `<code>`, …

## Motivation
> Quote the exact sentence of the cardholder instruction this serves.

Which scenario(s) exercise it, and what the challenge says about it.

## Inputs
| Field | Type | Null semantics |
|---|---|---|
| `authorization.…` | | |

Enrichment required: …

## Rule
Truth table. **Every row must state boundary behaviour explicitly.**

| Condition | Verdict | Reason code | Evidence |
|---|---|---|---|
| value < limit | `pass` | | |
| value == limit | `pass` \| `violation` — **state which and why** | | |
| value > limit | `violation` | | |
| value unavailable | `unknown` | | |

## Failure mode
Required input missing or malformed → …

## Fixtures
- **Exercised by:** AU…, AU…
- **Must NOT change:** AU…, AU…   ← run `make replay` and diff to confirm

## Vectors
`tests/vectors/<name>.yaml` — include the boundary case and the `unknown` case.

## Open questions
- [ ] …
