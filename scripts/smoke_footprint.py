"""Smoke test footprint_for_parcel against the real MOA services.

Runs the acceptance checks from the tool spec:
  1. Units sanity -- a building's computed EPSG:3338 area reconciles
     with Shape__Area x ~2.52 (proves the reprojection path is used,
     not the distorted Web-Mercator field).
  2. Attached-housing fix -- parcel 00326133000 (2141 Dawnlight Ct)
     returns realistic coverage well under 100% (the naive centroid
     join reported ~93%).
  3. Clean detached case -- parcel 00326477000 (1820 Parkside Dr)
     lands in a plausible 30-55% band with positive note-3 headroom.
  4. Big-lot headroom -- any Lot_Size >= 10,000 R1 single-unit parcel
     returns generous positive headroom at the base cap.
  5. Condo / no-lot -- a Lot_Size null/0 parcel returns a clean "no
     independent lot" response, not a divide-by-zero.
  6. Not found -- a bogus parcel id returns a clear error.

Usage: python3 scripts/smoke_footprint.py
(Windows: PYTHONIOENCODING=utf-8 venv/Scripts/python.exe ...)
"""

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.anchorage_gis.plugin import AnchorageGISPlugin  # noqa: E402

CONFIG = {
    "portal_base_url": "https://muniorg.maps.arcgis.com/sharing/rest",
    "gallery_group_id": "c34ed10758ec4f4eb8aa6826ee5be3ff",
    "org_id": "Ce3DhLRthdwbHlfF",
    "city_name": "Municipality of Anchorage",
    "gallery_url": (
        "https://muniorg.maps.arcgis.com/apps/instant/filtergallery/"
        "index.html?appid=4dac7569f1cc4beb9f22ce168c899a30"
    ),
    "timeout": 30,
}

BUILDINGS_URL = (
    "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/"
    "Buildings_Hosted/FeatureServer/0"
)
PARCELS_URL = (
    "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/"
    "PropertyInformation_Hosted/FeatureServer/0"
)

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


def payload_of(text: str) -> dict:
    m = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    return json.loads(m.group(1)) if m else {}


async def run_tool(plugin: AnchorageGISPlugin, parcel_id: str) -> str:
    r = await plugin.execute_tool(
        "footprint_for_parcel", {"parcel_id": parcel_id}
    )
    assert r.success, f"tool errored: {r.error_message}"
    return r.content[0]["text"]


async def main() -> None:
    plugin = AnchorageGISPlugin(CONFIG)
    ok = await plugin.initialize()
    assert ok, "plugin failed to initialize"
    try:
        # 1. Units sanity: one building, full-polygon 3338 area vs
        #    Shape__Area (Web-Mercator m2). Ratio ~2.52 sqft per WM m2
        #    in the Bowl (distortion 4.20-4.32 across the muni).
        data = await plugin._request_json_with_retry(
            f"{BUILDINGS_URL}/query",
            params={
                "f": "geojson",
                "where": "OBJECTID = 1740",
                "outFields": "OBJECTID,Shape__Area",
                "returnGeometry": "true",
                "outSR": "3338",
                "maxAllowableOffset": "0",
            },
        )
        feat = data["features"][0]
        paths = plugin._geojson_to_clipper_paths(feat["geometry"])
        import pyclipper  # noqa: PLC0415

        m2 = abs(sum(pyclipper.Area(p) for p in paths)) / (
            plugin.CLIP_SCALE**2
        )
        sqft = m2 * plugin.M2_TO_SQFT
        wm_area = feat["properties"]["Shape__Area"]
        ratio = sqft / wm_area
        check(
            "1. units sanity (3338 area vs Shape__Area x ~2.52)",
            abs(ratio - 2.52) / 2.52 < 0.03,
            f"ratio={ratio:.3f}",
        )

        # 2. Attached housing: clipped coverage must be realistic.
        text = await run_tool(plugin, "00326133000")
        p = payload_of(text)
        check(
            "2. attached-housing clip (2141 Dawnlight Ct < 100%)",
            0.10 < p.get("coverage_pct", 99) < 0.80,
            f"coverage={p.get('coverage_pct')}",
        )

        # 3. Clean detached-ish case in the 30-55% band.
        text = await run_tool(plugin, "00326477000")
        p = payload_of(text)
        in_band = 0.30 <= p.get("coverage_pct", 0) <= 0.55
        note3_pos = p.get("headroom_if_note3_50pct", -1) > 0
        check(
            "3. detached case (1820 Parkside Dr, 30-55% + note-3 > 0)",
            in_band and note3_pos,
            f"coverage={p.get('coverage_pct')} "
            f"note3={p.get('headroom_if_note3_50pct')}",
        )

        # 4. Big-lot headroom: find a live R1 single-unit >= 10k lot.
        data = await plugin._request_json_with_retry(
            f"{PARCELS_URL}/query",
            params={
                "f": "json",
                "where": (
                    "Lot_Size >= 10000 AND Total_Living_Units = 1 "
                    "AND Zoning_District = 'R1' "
                    "AND GIS_Category = 'Parcel'"
                ),
                "outFields": "Parcel_ID",
                "returnGeometry": "false",
                "resultRecordCount": "1",
            },
        )
        big_id = data["features"][0]["attributes"]["Parcel_ID"]
        text = await run_tool(plugin, big_id)
        p = payload_of(text)
        check(
            f"4. big-lot headroom ({big_id})",
            p.get("adu_footprint_headroom_sqft", -1) > 0
            and p.get("district_max_coverage") == 0.40,
            f"headroom={p.get('adu_footprint_headroom_sqft')} "
            f"lot={p.get('lot_size_sqft')}",
        )

        # 5. No-lot parcel: clean response, no division blow-up.
        data = await plugin._request_json_with_retry(
            f"{PARCELS_URL}/query",
            params={
                "f": "json",
                "where": (
                    "(Lot_Size IS NULL OR Lot_Size <= 0) "
                    "AND GIS_Category = 'Parcel'"
                ),
                "outFields": "Parcel_ID",
                "returnGeometry": "false",
                "resultRecordCount": "1",
            },
        )
        feats = data.get("features") or []
        if feats:
            nolot_id = feats[0]["attributes"]["Parcel_ID"]
            text = await run_tool(plugin, nolot_id)
            check(
                f"5. no-lot parcel ({nolot_id})",
                "no independent lot" in text,
            )
        else:
            check(
                "5. no-lot parcel",
                True,
                "skipped: no Lot_Size null/0 parcel exists live "
                "(path is unit-tested)",
            )

        # 6. Bogus id: clear not-found, not an exception.
        text = await run_tool(plugin, "999-999-99")
        check("6. bogus parcel id", "no parcel found" in text)

    finally:
        await plugin.shutdown()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
