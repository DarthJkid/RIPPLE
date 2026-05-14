# **ADR-004: Reframe case studies — Tees as primary, Wharfe/Wye as comparators**
 
**STATUS**: Accepted
**DATE**: 14-05-2026
 
## **CONTEXT**
 
My original RIPPLE specification framed three case-study catchments as roughly equal:
 
- Tees (urban-industrial, Northumbrian Water territory, local relevance);
- Wharfe (Yorkshire, the Ilkley bathing-water designation, Yorkshire Water territory);
- Wye (Welsh/English border, agricultural phosphate pollution, Welsh Water + Severn Trent).
The implicit assumption was that each catchment would yield comparable *continuous water-quality* coverage for planned model.
 
The actual data ingestion shows the assumption was wrong:
 
| Catchment | Continuous-WQ stations in bbox | Parameters with non-zero rows |
|---|---|---|
| Tees (25 km) | 1 (`E06388A`) | All 6 WQ params, ~3,800 rows each over 6 months |
| Wharfe (15 km) | 0 | — |
| Wye (30 km) | 1 (temperature-only, sparse) | Temperature, 9 rows over 6 months |
 
Everything else discovered in those bboxes is `waterFlow` from level/flow gauges. Continuous water-quality monitoring is concentrated, not distributed: the EA has ~8,000 hydrology stations nationally but only a few hundred publish continuous multi-parameter WQ.
 
Therefore, continuous EA water-quality stations are not uniformly distributed; the analysis will necessarily be representative rather than national but the symmetry of the three-case-study framing does not survive contact.
 
## **DECISION**
 
Reframe the project around three *asymmetric* case studies:
 
- **Tees (primary).** Continuous water-quality data from `E06388A`, plus flow data from ~17 gauges. The full RIPPLE pipeline (seasonal baseline → residual scoring → Isolation Forest → CSO context join) runs here. This is the case study the blog post will lead with.
- **Wharfe (flow + spills comparator).** Flow data from ~15 gauges, no continuous WQ. Substitute WIMS lab samples as the WQ signal; baseline modelling necessarily lower-frequency. The Ilkley bathing-water angle remains publicly interesting and worth presenting.
- **Wye (flow + spills comparator).** Similar to Wharfe. WIMS coverage is reportedly dense for this catchment because of the long-running agricultural-pollution story.
## **CONSEQUENCES**
 
- Honest about what the data permits. The "real-time anomaly detection on continuous sensor streams" framing is preserved where the data supports it (Tees) and adapted to a lower-frequency variant where it doesn't (Wharfe, Wye).
- The proposal text needs updating in two places: the abstract (which currently treats the three catchments symmetrically) and the limitations section (which can now state the scarcity quantitatively).
- The Tees becomes a single-point-of-failure: if `E06388A` turns out to have data quality issues — calibration drift, large gaps, or sensor swaps within the window — the continuous-WQ story for the whole project weakens. Mitigation: spot-check `E06388A` in the Day 7 sanity-checks notebook *before* committing to Week 2 modelling.
- Positive interview framing: "what did the data force you to change about the project plan" is exactly the kind of question this reframing answers. Worth surfacing in the README and blog post explicitly rather than hiding it.
## Alternatives considered
 
- **Expand the catchment radii** (Wharfe to 30 km, Wye to 50 km). Costs catchment purity (you start grabbing parts of neighbouring rivers). Defer the decision until Day 2 when WIMS coverage is checked — if WIMS fills the gap, no need to expand; if it doesn't, expansion is the fallback.
- **Drop Wharfe and Wye, do Tees only.** Loses the cross-catchment comparison and the local-relevance + agricultural-pollution stories. Too narrow for a portfolio piece.
- **Switch to a different combination of catchments.** Would invalidate the local-relevance argument for Tees, which is the strongest single answer to "why this project?". Not worth restarting.