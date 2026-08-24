"""Ad-hoc production smoke test for the Anchorage GIS MCP server.

Exercises the JSON-RPC surface and the core tool chain end-to-end
against the live Lambda. Read-only; paces calls to stay under the
API Gateway rate limit (5 rps) and WAF per-IP cap (300/5min).
"""

import json
import sys
import time
import urllib.request

URL = "https://622f4qcew8.execute-api.us-west-2.amazonaws.com/prod/mcp"
_id = 0
PASS = "PASS"
FAIL = "FAIL"
results = []


def rpc(method, params=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode())
    time.sleep(0.4)  # pace under 5 rps
    return body


def call_tool(name, args):
    return rpc("tools/call", {"name": f"anchorage_gis__{name}", "arguments": args})


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def check(label, ok, detail=""):
    results.append((label, ok))
    mark = PASS if ok else FAIL
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


# 1. ping
# The spec defines the ping result as an empty object -- the liveness signal
# is the successful response itself, not its body.
try:
    r = rpc("ping")
    check("ping", r.get("result") == {} and "error" not in r, str(r.get("result")))
except Exception as e:
    check("ping", False, repr(e))

# 2. initialize
try:
    r = rpc(
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {},
         "clientInfo": {"name": "smoke", "version": "1.0"}},
    )
    si = r["result"]["serverInfo"]
    pv = r["result"]["protocolVersion"]
    # serverInfo now reports the configured name (was hardcoded "opencontext"),
    # and the server echoes the client's requested protocol version.
    check(
        "initialize",
        si["name"] == "Anchorage GIS MCP" and pv == "2025-03-26",
        json.dumps({"serverInfo": si, "protocolVersion": pv}),
    )
except Exception as e:
    check("initialize", False, repr(e))

# 3. tools/list
try:
    r = rpc("tools/list")
    tools = [t["name"] for t in r["result"]["tools"]]
    check(
        "tools/list",
        len(tools) == 16
        and "anchorage_gis__footprint_for_parcel" in tools,
        f"{len(tools)} tools",
    )
except Exception as e:
    check("tools/list", False, repr(e))

# 4. find_gis_content (discovery)
try:
    r = call_tool("find_gis_content", {"topic": "parks", "limit": 6})
    t = text_of(r)
    check("find_gis_content(parks)", "ID:" in t, f"{len(t)} chars")
except Exception as e:
    check("find_gis_content(parks)", False, repr(e))

# 5. search_spatial_layers -> grab a Feature Service id
fs_id = None
try:
    r = call_tool(
        "search_spatial_layers",
        {"query": "parks", "layer_type": "layers", "limit": 8},
    )
    import re
    t = text_of(r)
    m = re.search(r"_Feature Service_\s*--\s*ID: `([0-9a-f]{32})`", t)
    fs_id = m.group(1) if m else None
    check("search_spatial_layers", fs_id is not None, f"picked {fs_id}")
except Exception as e:
    check("search_spatial_layers", False, repr(e))

# 6. get_item_details
if fs_id:
    try:
        r = call_tool("get_item_details", {"item_id": fs_id})
        t = text_of(r)
        check("get_item_details", "ID:" in t and "Type:" in t, f"{len(t)} chars")
    except Exception as e:
        check("get_item_details", False, repr(e))

# 7. get_layer_schema -> capture a field name
field = None
try:
    r = call_tool("get_layer_schema", {"item_id": fs_id})
    t = text_of(r)
    import re
    # schema lists fields; grab a plausible text field name
    names = re.findall(r"`([A-Za-z][A-Za-z0-9_]{2,})`", t)
    field = next((n for n in names if n not in ("OBJECTID", "Shape")), None)
    check("get_layer_schema", bool(names), f"field sample: {field}")
except Exception as e:
    check("get_layer_schema", False, repr(e))

# 8. query_data count (limit=1 -> TOTAL COUNT)
try:
    r = call_tool("query_data", {"item_id": fs_id, "limit": 1})
    t = text_of(r)
    check("query_data count", "TOTAL COUNT" in t, t.split("\n")[3][:80] if len(t.split("\n")) > 3 else t[:80])
except Exception as e:
    check("query_data count", False, repr(e))

# 9. query_data listing (limit=3)
try:
    r = call_tool("query_data", {"item_id": fs_id, "limit": 3})
    t = text_of(r)
    ok = "Record 1:" in t and "results above are a sample" not in t
    check("query_data listing (no stale reminder)", ok,
          "trimmed reminder absent" if ok else "stale text present")
except Exception as e:
    check("query_data listing (no stale reminder)", False, repr(e))

# 10. get_distinct_values
if field:
    try:
        r = call_tool("get_distinct_values", {"item_id": fs_id, "field": field, "limit": 10})
        t = text_of(r)
        check("get_distinct_values", "isError" not in r.get("result", {}), f"field={field}")
    except Exception as e:
        check("get_distinct_values", False, repr(e))

# 11. spatial_query_point on a polygon layer (Park Land), point in Anchorage
try:
    r = call_tool(
        "spatial_query_point",
        {"item_id": "466c5b7adafe4468aebcc29347e8c84e", "lon": -149.85, "lat": 61.19},
    )
    t = text_of(r)
    check("spatial_query_point", bool(t), t.split("\n")[0][:80])
except Exception as e:
    check("spatial_query_point", False, repr(e))

# 12. error handling: query a viewer (Web Mapping App) -> graceful, actionable error
try:
    r = call_tool("query_data", {"item_id": "b4c05a8ee42d4d44b8c6eb27b5d0158f", "limit": 2})
    t = text_of(r)
    ok = "not a queryable" in t and "find_gis_content" in t
    check("error handling (viewer rejected gracefully)", ok, t[:80])
except Exception as e:
    check("error handling (viewer rejected gracefully)", False, repr(e))

# 13. error handling: bad field -> 'did you mean' / schema recovery hint
try:
    r = call_tool("query_data", {"item_id": fs_id, "where": "Nonexistent_Field='x'"})
    t = text_of(r)
    ok = ("get_layer_schema" in t) or ("does not exist" in t) or ("CASE-SENSITIVE" in t)
    check("error handling (bad field -> recovery hint)", ok, t[:90])
except Exception as e:
    check("error handling (bad field -> recovery hint)", False, repr(e))
# -- regression checks for previously-broken behaviours ----------------
# Each of these was a live bug that is now fixed. They assert on stable
# aggregates (bucket counts, record counts) rather than OBJECTIDs:
# Streets_Hosted was republished in April and its OBJECTIDs shifted by
# ~236k, so a test pinned to them would have rotted silently.

PARK_FACILITIES = "98d46bfe15a046b99645b6d6b32f5d59"   # POINT layer
COMMUNITY_COUNCIL = "934783d347ee4df5a0c12bd2d0339045"
STREETS = "9072014f74064419b0641bfc701f0ebd"           # POLYLINE layer
ASSEMBLY = "8e4f3735298f45fd9d31b99bddae4563"
PROPERTY_INFO = "57d6ff611f444d75a1bf2b4a1d340163"

# 14. spatial_query_polygon against a POINT target via filter_item_id
try:
    r = call_tool("spatial_query_polygon", {
        "item_id": PARK_FACILITIES,
        "filter_item_id": COMMUNITY_COUNCIL,
        "filter_where": "COUNCIL='Downtown'",
        "limit": 5,
    })
    t = text_of(r)
    ok = not r.get("result", {}).get("isError") and "No features" not in t
    check("regression: point-layer target via filter_item_id", ok, t[:80])
except Exception as e:
    check("regression: point-layer target via filter_item_id", False, repr(e))

# 15. the same, via inline filter_geometry (a separate code path)
try:
    r = call_tool("spatial_query_polygon", {
        "item_id": PARK_FACILITIES,
        "filter_geometry": {
            "type": "Polygon",
            "coordinates": [[[-149.92, 61.20], [-149.92, 61.23],
                             [-149.85, 61.23], [-149.85, 61.20],
                             [-149.92, 61.20]]],
        },
        "limit": 5,
    })
    t = text_of(r)
    ok = not r.get("result", {}).get("isError") and "No features" not in t
    check("regression: point-layer target via inline geometry", ok, t[:80])
except Exception as e:
    check("regression: point-layer target via inline geometry", False, repr(e))

# 16. search_layers_by_field with no service_keyword
try:
    r = call_tool("search_layers_by_field", {"field_keyword": "COUNCIL"})
    t = text_of(r)
    check("regression: search_layers_by_field without service_keyword",
          "CommunityCouncil" in t, t[:80])
except Exception as e:
    check("regression: search_layers_by_field without service_keyword",
          False, repr(e))

# 17. aggregate_by_polygon with a POLYLINE source
try:
    r = call_tool("aggregate_by_polygon", {
        "source_item_id": STREETS,
        "aggregation_item_id": ASSEMBLY,
        "group_by_field": "ASSEMBLY_SECTION",
    })
    sc = r.get("result", {}).get("structuredContent") or {}
    summ = sc.get("summary", {})
    ok = summ.get("buckets") == 3 and summ.get("unmatched") == 0
    check("regression: polyline source aggregation", ok,
          f"buckets={summ.get('buckets')} unmatched={summ.get('unmatched')} "
          f"source={summ.get('source_features')}")
except Exception as e:
    check("regression: polyline source aggregation", False, repr(e))

# 18. UNION inside a string literal must not trip the WHERE sanitizer
try:
    r = call_tool("query_data", {
        "item_id": PROPERTY_INFO,
        "where": "Parcel_Address LIKE '%UNION%'",
        "out_fields": "Parcel_ID,Parcel_Address",
        "limit": 10,
    })
    t = text_of(r)
    ok = "Forbidden keyword" not in t and not r.get("result", {}).get("isError")
    check("regression: UNION inside a string literal allowed", ok, t[:80])
except Exception as e:
    check("regression: UNION inside a string literal allowed", False, repr(e))

# 19. the assessor-trap banner fires on PropertyInformation_Hosted
try:
    r = call_tool("query_data", {"item_id": PROPERTY_INFO, "limit": 1})
    t = text_of(r)
    check("assessor trap banner present", "ASSESSOR DATA TRAPS" in t, t[:70])
except Exception as e:
    check("assessor trap banner present", False, repr(e))



print("\n=== SUMMARY ===")
n_pass = sum(1 for _, ok in results if ok)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
