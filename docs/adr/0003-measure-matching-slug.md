# **ADR-003: Match measures via `observedProperty.@id` URL slug**
 
**STATUS**: Accepted
**DATE**: 14-05-2026
 
## **CONTEXT**
 
Each station's `measures` array contains entries with multiple parameter-related fields:
 
| Field | Example value |
|---|---|
| `parameter` | `"DISSOLVED OXYGEN"` |
| `parameterName` | `"Dissolved Oxygen"` |
| `observedProperty.@id` | `".../def/op/dissolved-oxygen"` |
| `notation` | `"E09235A-do-i-subdaily-mgL"` |
 
Our wanted parameters are stored as the kebab-case (water-quality) or camelCase (hydrological) slugs the API uses in `observedProperty=` query filters: `"dissolved-oxygen"`, `"waterFlow"`, and so on.
 
Given a station's measure dict and a wanted parameter slug, we need to decide whether they match.
 
## **DECISION**
 
`pick_measure_id` matches on the trailing URL segment of `observedProperty.@id`:
 
```python
slug = m["observedProperty"]["@id"].rsplit("/", 1)[-1]
if slug == wanted_parameter:
    ...
```
 
This is exact-equality against the same slug the API uses in `observedProperty=` query parameters.
 
When multiple measures on the same station match the parameter (DO in both `%` and `mg/L`; flow in both 15-min instantaneous and daily aggregates), `pick_measure_id` sorts to prefer:
 
1. `valueType == "instantaneous"` over aggregates;
2. smaller `period` (in seconds) over larger;
3. the unit listed in `PREFERRED_UNITS` over alternatives.
## **CONSEQUENCES**
 
- Exact-equality match avoids false positives that substring matching can produce (e.g. matching `dissolved-oxygen` against an entry for `dissolved-oxygen-mg-l-stage` if such a thing existed).
- Handles the API's inconsistent casing automatically: water-quality parameters are kebab-case, hydrological parameters are camelCase, and both appear correctly in `.@id`.
- The instantaneous-and-short-period sort prevents the silent-downgrade-to-daily problem caught at end of Day 1: stations that publish both 15-min and daily flow now consistently yield 15-min, ~17,500-row series rather than 180-row daily series.
- Negative: relies on `observedProperty.@id` being present in every measure. Confirmed present in single-station detail responses; *not* always present in list endpoint summary view — which is why ADR-002's two-phase discovery is required for this to work.
## Alternatives considered
 
- **Substring match on `parameterName`.** Tried first; failed silently. `"dissolved-oxygen" in "Dissolved Oxygen".lower()` is `False` because hyphen ≠ space. Cost half a day of zero-output runs before being caught.
- **Match on the measure `notation`.** Reverse-engineer the slug structure from strings like `E09235A-do-i-subdaily-mgL`. Fragile — the abbreviation rules (`do` for dissolved oxygen, `amm` for ammonium, `temp` for temperature, etc.) are nowhere documented and would need to be rebuilt by reading example responses. Wrong layer of abstraction.
- **Match on `parameter` (uppercase).** Equivalent to substring match on `parameterName` and has the same hyphen/space problem.