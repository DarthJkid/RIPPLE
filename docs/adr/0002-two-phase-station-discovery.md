#**ADR-0002: Two-phase Station Discovery**

**STATUS**: Accepted
**DATE**: 14-05-2026

#**CONTEXT**
The EA Hydrology API exposes station data via two endpoints:
- ```/id/stations``` - List of stations, filterable by ```lat```, ```long```, ```dist```, ```observedProperty```.
- ```/id/stations/{id}``` - Single station detail.
  
The list endpoint a ```_view``` parameter taking values ```default``` or ```minimal``` only. ```_view=full``` is documented for some endpoints in the EA's REST family but returns HTTP 400 on ```/id/stations``` - I discovered this the hard way. 

In the ```default``` view, the ```measures``` array on each station item is abbreviated which is sufficient to know that the station publishes the queried ```observedProperty```, but missing the nested ```observedProperty.@id```, ```unitName```, ```valueType```, and ```period``` fields needed by ```pick_measure_id``` to resolve which specific measure to fetch (e.g. DO in mg/L vs DO in % saturation).

The single-station endpoint returns the full nested ```measures``` array.

#**DECISION**

Discovery will happen in two passes per catchment:
1. List pass: Call ```/id/stations?lat=...&long=...&dist=...``` once per catchment with no ```observedProperty``` filter. Filter the responce to active stations and collect their ```stationGuid```.
2. Detail Pass: For each active GUID, call ```/id/stations/{guid}.json``` and read the full ```measures``` array.


Parameter-to-measure resolution happens at the application layer in ```pick_measure_id``` against the fully populated measures.

#**CONSEQUENCES**
- API call count rises from ```catchments x parameter``` to ```active stations per catchment```. For 3 catchments x ~500 stations = ~500 calls which is about 8 and 1/2 minutes wall-clock at 1 req/sec. Acceptable for a daily refresh.
- The pipeline is robust to parameters being added later: a new parameter doesn't require re-running discover, jusr re-iterating cached measures.
- Postive side-effect: Discover captures every parameter each station publishes, not just the ones in our current ```PARAMETERS``` list. This is the data foundation for any addition decisions.
- Negative: Discovery is slower than a single call. Caching the station-detail responses across runs would speed re-discovery but adds complexity.

Alternatives considered
- Single list call per ```(catchment, parameter)``` with ```observedProperty``` filter. Tried first. Failed silently: the abbreviated measures in the default-view list response didn't carry enough metadata for ```pick_measure_id``` to resolve the right measure, so ```pick_measure_id``` returned ```None``` for every station and the script wrote zero Parquet files with no log line indicating why. Caught only when the empty manifest crashed ```groupby("status")``` at the end.
- ```_view=full``` on ```/id/stations```. Returns HTTP 400; not a valid view on that endpoint.
- Use ```/id/measures?station={guid}&observedProperty={param}``` directly by replacing ```pick_measure_id```. Rejected because it adds a third endpoint to the pipeline's model and a per-station detail approach is simpler.

