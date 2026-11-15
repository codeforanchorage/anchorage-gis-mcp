"""Anchorage GIS plugin for OpenContext.

Access to the Municipality of Anchorage GIS Gallery and spatial data via
the ArcGIS Portal REST API. Everything a GIS deployment shares --
discovery, schema, attribute and spatial queries, the polygon analytics,
upstream resilience, structured output -- lives in the
`opencontext-arcgis-hub` package (`arcgis_hub.plugin.ArcGISHubPlugin`).
This module holds only what is specific to Anchorage: MOA wording, the
municipal bounding box, the assessor-layer banner, and the two parcel
tools (`find_parcel`, `footprint_for_parcel`).

To change shared behaviour, change it in opencontext-arcgis-hub, tag a
release, and move the pin in requirements.txt / pyproject.toml.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import pyclipper

from arcgis_hub.plugin import (  # noqa: F401  (re-exported for tests)
    RETRY_MIN_REMAINING_S,
    TOOL_CALL_BUDGET_S,
    ArcGISHubPlugin,
    UpstreamTimeoutError,
    _call_deadline,
)
from arcgis_hub.where_validator import OutFieldsValidator, WhereValidator
from core.interfaces import ToolDefinition, ToolInputError
from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig

logger = logging.getLogger(__name__)


def _replace_text(node: Any, old: str, new: str) -> Any:
    """Deep copy of a JSON-schema tree with `old` replaced in strings."""
    if isinstance(node, dict):
        return {k: _replace_text(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_text(v, old, new) for v in node]
    if isinstance(node, str):
        return node.replace(old, new)
    return node


class AnchorageGISPlugin(ArcGISHubPlugin):
    """Plugin for accessing Municipality of Anchorage GIS data."""

    plugin_name = "anchorage_gis"
    plugin_version = "1.2.0"

    CONFIG_CLASS = AnchorageGISPluginConfig
    DISPLAY_NAME = "Anchorage GIS"
    REGION_NAME = "Anchorage"

    # On-prem MOA hosts we'll proxy without further checks.
    ONPREM_HOST_SUFFIXES = (".muni.org",)

    TEXT = {
        **ArcGISHubPlugin.TEXT,
        "ambiguity_warning": (
            "\n> **AMBIGUITY WARNING:** multiple queryable "
            "layers match this topic. They may be maintained "
            "by different agencies (e.g. municipal vs state "
            "vs federal) or cover different subsets (e.g. "
            "all trails vs nordic trails only). For 'how "
            "many?' / 'list all' questions, do NOT silently "
            "pick the first one -- either (a) query each "
            "layer with `limit=1` and report a breakdown of "
            "totals, or (b) ask the user which subset they "
            "mean (e.g. 'municipal Parks & Rec', 'state-"
            "managed', 'all combined'). The titles below "
            "hint at scope (look for agency prefixes like "
            "'ADNR', 'USFS', 'ParksRec', 'NSAA').\n\n"
        ),
        "unmatched_diagnosis": (
            "This usually indicates data-quality signal "
            "(stray coordinates, records outside the city "
            "boundary)."
        ),
        "mercator_inflation": "~4.3x at Anchorage",
        "coverage_verify_hint": (
            "Verify individual parcels with "
            "footprint_for_parcel, which clips buildings "
            "to the lot line."
        ),
        "stacked_records_note": (
            "MOA layers hold multipart parcels plus stacked "
            "Lease and Economic records. On "
            "PropertyInformation_Hosted add "
            "GIS_Category='Parcel' to target_where to drop "
            "the stacked records."
        ),
        "spanning_example": (
            "should be a parcels layer (e.g. MOA_Parcels_Hosted, "
            "TaxParcels_Hosted) and classification should be a "
            "zoning layer (e.g. Zoning_Hosted). Use "
        ),
        "parcel_layer_examples": (
            "(e.g. "
            "MOA_Parcels_Hosted, TaxParcels_Hosted, "
            "PropertyInformation_Hosted) "
        ),
        "shape_area_note": (
            "NOTE: Shape__Area on MOA hosted layers is "
            "WEB MERCATOR, not a real area -- it is "
            "inflated ~4.3x at Anchorage's latitude. The "
        ),
        "hosted_layers": "MOA hosted layers",
    }

    # ── Coverage of the municipality ─────────────────────────────────────

    # Anchorage muni bbox in WGS84 (xmin, ymin, xmax, ymax). Generous
    # envelope around Cook Inlet to Eklutna -- used for the
    # coverage-gap devil's-advocate check, not for filtering. Exact
    # boundary fidelity isn't needed; the goal is to flag clearly
    # partial-coverage layers (e.g. a single neighborhood layer).
    ANCHORAGE_BBOX_WGS84 = (-150.5, 60.5, -148.5, 61.6)
    @classmethod
    def _anchorage_coverage_pct(cls, extent: Any) -> Optional[float]:
        # Returns (layer intersect muni) / muni-area, in [0, ~1+]. >1 if the
        # layer is larger than the muni (statewide data). Used to fire
        # the LIMITED COVERAGE caveat for layers covering <50% of the
        # muni. Returns None when we can't honestly compute coverage
        # (unhandled SR, malformed extent) so the caveat is suppressed
        # rather than guessed.
        if not isinstance(extent, dict):
            return None
        coords = [extent.get(k) for k in ("xmin", "ymin", "xmax", "ymax")]
        if any(not isinstance(v, (int, float)) for v in coords):
            return None
        xmin, ymin, xmax, ymax = coords
        # Degenerate bbox (single point or zero-width/height) carries no
        # useful coverage signal -- a hospital layer with one point in
        # downtown isn't "non-overlapping Anchorage", it just has no
        # area. Return None so the caveat stays silent.
        if xmin == xmax or ymin == ymax:
            return None
        sr = extent.get("spatialReference") or {}
        wkid = sr.get("wkid") or sr.get("latestWkid")
        if wkid in (4326, 4269):
            pass
        elif wkid in (102100, 3857, 102113):
            xmin, ymin = cls._webmerc_to_wgs84(xmin, ymin)
            xmax, ymax = cls._webmerc_to_wgs84(xmax, ymax)
        else:
            return None
        muni = cls.ANCHORAGE_BBOX_WGS84
        ix = max(xmin, muni[0])
        iy = max(ymin, muni[1])
        ax = min(xmax, muni[2])
        ay = min(ymax, muni[3])
        if ix >= ax or iy >= ay:
            return 0.0
        intersection = (ax - ix) * (ay - iy)
        muni_area = (muni[2] - muni[0]) * (muni[3] - muni[1])
        return intersection / muni_area
    @classmethod
    def _region_coverage_pct(cls, extent: Any) -> Optional[float]:
        return cls._anchorage_coverage_pct(extent)

    # ── Assessor layer banner ────────────────────────────────────────────

    # Layer-specific traps surfaced as a response banner. These are
    # properties of the published assessment roll, not of this server, and
    # they have each already produced a wrong public-facing number. They
    # live here rather than only in the tool description so they reach the
    # caller at the moment they query the layer.
    PROPERTY_INFO_ITEM_ID = "57d6ff611f444d75a1bf2b4a1d340163"

    ASSESSOR_TRAP_BANNER = (
        "**ASSESSOR DATA TRAPS (PropertyInformation_Hosted):**\n"
        "- `Total_Living_Units` CANNOT be summed naively. Condo parcels "
        "each repeat their whole building's unit total, and "
        "`Land_Use='Lease Master'` rows duplicate apartment entries. "
        "Count condo parcels as one unit each, and sum "
        "`Total_Living_Units` only for `Land_Use NOT LIKE 'Condo%' AND "
        "Land_Use <> 'Lease Master'`.\n"
        "- `Property_Type='Residential'` UNDERCOUNTS dwellings: apartment "
        "buildings are classified `Commercial` in the assessment roll.\n"
        "- `Total_Living_Units` has miscoded outliers -- a mini-warehouse "
        "at 10880 Mausel St carries 423 'living units' (storage units), a "
        "single-family parcel at 20490 Icefall Dr carries 101. "
        "Cross-validate against `Land_Use` for any unit-count work.\n"
        "- `Zoning_District` is NOT normalized: both `B-2C` and `B2C` "
        "exist as distinct values (likewise other B-2 districts), so a "
        "WHERE clause on one form silently misses the other. Call "
        "`get_distinct_values(field='Zoning_District', like='B2')` before "
        "filtering.\n"
        "- ~1,063 records have a NULL `Parcel_ID` (and null `Land_Use`, "
        "`Legal_Description`, `Appraised_Total_Value`). They carry "
        "geometry but nothing else and inflate every count by ~1%. Add "
        "`Parcel_ID IS NOT NULL` when counting.\n"
        "- Rows are POLYGONS, not parcels: multipart parcels plus stacked "
        "`Lease` and `Economic` records. Add `GIS_Category='Parcel'` for "
        "one row per parcel."
    )
    def _query_result_banner(self, item_id: str) -> Optional[str]:
        if item_id == self.PROPERTY_INFO_ITEM_ID:
            return self.ASSESSOR_TRAP_BANNER
        return None

    # ── Parcel tools ─────────────────────────────────────────────────────

    @staticmethod
    def _normalize_parcel_variants(raw: Any) -> List[str]:
        """Generate MOA parcel ID format variants for cross-dataset
        lookup.

        MOA parcel IDs are stored in two related canonical forms across
        layers:
          - 8-digit base: ``XXXXXXXX`` (compact) or ``XXX-XXX-XX``
            (hyphenated) -- e.g. ``00318487`` / ``003-184-87``.
          - 11-digit extended: 8-digit base + 3-digit sub-parcel
            suffix (``000`` means no sub) -- e.g. ``00318487000`` /
            ``003-184-87-000``.

        TaxParcels stores 11-digit compact in ``Parcel_Num``/``Name``;
        PropertyInformation has all four variants in separate columns
        (``GIS_ParcelNum8``, ``GIS_ParcelNum8Formatted``,
        ``GIS_ParcelNum11``, ``GIS_ParcelNum11Formatted``). The model
        rarely knows which form a given layer uses, so we generate
        all four for use in ``WHERE field IN (...)``.

        Input handling: extracts digits from the input, pads/splits
        based on length to recover the 8-digit base + 3-digit sub.
        Hyphens, leading zeros, and prefixes/suffixes are flexible.
        """
        if raw is None:
            return []
        digits = "".join(c for c in str(raw) if c.isdigit())
        if not digits or len(digits) < 5:
            return []

        if len(digits) >= 11:
            # Take the LAST 11 digits -- accommodates inputs like
            # "Parcel 00318487000" if any non-digit prefixes slipped
            # through.
            tail = digits[-11:]
            base8 = tail[:8]
            sub3 = tail[8:11]
        elif len(digits) >= 9:
            # 9 or 10 digits -- pad on the left to 11, then split.
            padded = digits.rjust(11, "0")
            base8 = padded[:8]
            sub3 = padded[8:11]
        else:
            # 5-8 digits -- pad on the left to 8, default to no
            # sub-parcel.
            base8 = digits.rjust(8, "0")
            sub3 = "000"

        variants: set = set()
        variants.add(base8)
        variants.add(f"{base8[0:3]}-{base8[3:6]}-{base8[6:8]}")
        variants.add(base8 + sub3)
        variants.add(
            f"{base8[0:3]}-{base8[3:6]}-{base8[6:8]}-{sub3}"
        )
        # Always also try the literal stripped input, in case the layer
        # stores some non-canonical form we did not anticipate.
        literal = str(raw).strip()
        if literal:
            variants.add(literal)
        return sorted(variants)
    async def _find_parcel(
        self, args: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        """Look up a parcel across MOA format variants in one call.

        The same parcel can appear in different layers as
        ``001-213-29``, ``00121329``, ``003-184-87-000``, etc. The
        model rarely knows which form a given layer uses; this tool
        generates the four canonical forms from the input and tries
        them all in a single ``WHERE field IN (...)``. If none match,
        it falls back to a ``LIKE`` query on a distinctive substring
        and returns up to 5 candidates the model can inspect.
        """
        item_id = self._validate_item_id(
            (args.get("item_id") or "").strip()
        )
        parcel_field = (args.get("parcel_field") or "").strip()
        if not parcel_field:
            raise ToolInputError("parcel_field is required")
        parcel_id = (args.get("parcel_id") or "").strip()
        if not parcel_id:
            raise ToolInputError("parcel_id is required")
        out_fields = OutFieldsValidator.validate(
            args.get("out_fields") or "*"
        )
        limit = min(self._int_arg(args, "limit", 10), 100)

        layer_url = await self._resolve_layer_url(item_id)
        meta = await self._fetch_layer_meta(
            layer_url, require_fields=(parcel_field,)
        )
        field_names = {f.get("name") for f in meta.get("fields", [])}
        if parcel_field not in field_names:
            raise ToolInputError(
                f"parcel_field {parcel_field!r} is not a field on "
                f"this layer. Call `get_layer_schema(item_id="
                f"'{item_id}', keyword='parcel')` to find the right "
                f"field name. Common MOA parcel field names: "
                f"`Parcel_Num`, `Name`, `Parcel_ID`, `GIS_ParcelNum8`, "
                f"`GIS_ParcelNum11`, `GIS_ParcelNum8Formatted`, "
                f"`GIS_ParcelNum11Formatted`."
            )

        variants = self._normalize_parcel_variants(parcel_id)
        if not variants:
            raise ToolInputError(
                f"Could not extract any digits from parcel_id "
                f"{parcel_id!r}. Provide a parcel ID like "
                f"'001-213-29', '00121329', or '00121329000'."
            )

        # Build IN clause; SQL-quote each variant with '' escape.
        quoted = ",".join(
            "'" + v.replace("'", "''") + "'" for v in variants
        )
        where_in = WhereValidator.validate(
            f"{parcel_field} IN ({quoted})"
        )

        params = {
            "f": "json",
            "where": where_in,
            "outFields": out_fields,
            "returnGeometry": "false",
            "resultRecordCount": str(limit),
        }
        resp = await self._send("get",
            f"{layer_url}/query", params=params
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise RuntimeError(
                "Parcel lookup query failed: "
                + self._rewrite_arcgis_error(
                    err.get("message", "Unknown error"),
                    err.get("details", []),
                    resource_id=item_id,
                    has_where=True,
                )
            )

        features = data.get("features", [])
        query_echo = {
            "item_id": item_id,
            "parcel_field": parcel_field,
            "parcel_id": parcel_id,
            "variants": [str(v) for v in variants],
            "limit": limit,
        }

        if features:
            matched_values: set = set()
            for f in features:
                v = (f.get("attributes") or {}).get(parcel_field)
                if v is not None:
                    matched_values.add(v)
            ordered = sorted(matched_values, key=str)
            structured = {
                "query": query_echo,
                "summary": {
                    "returned": len(features),
                    "matched": True,
                    "matched_values": [self._scalar(v) for v in ordered],
                    "canonical": (
                        self._scalar(sorted(matched_values)[0])
                        if matched_values
                        else None
                    ),
                },
                "rows": [
                    dict(f.get("attributes") or {}) for f in features
                ],
                "caveats": [],
            }
            lines = [
                f"## Parcel lookup: `{parcel_id}` -> "
                f"{len(features)} record(s) found",
                f"**Layer:** `{item_id}`",
                f"**Field:** `{parcel_field}`",
                f"**Variants tried ({len(variants)}):** "
                + ", ".join(f"`{v}`" for v in variants),
            ]
            if matched_values:
                canonical = sorted(matched_values)[0]
                lines.append(
                    "**Matched stored format(s):** "
                    + ", ".join(
                        f"`{v}`" for v in sorted(matched_values)
                    )
                )
                lines.append(
                    f"**Canonical form for this layer:** "
                    f"`{canonical}` -- use this verbatim for "
                    f"follow-up queries here."
                )
            lines.append("")
            for i, f in enumerate(features, 1):
                attrs = f.get("attributes") or {}
                lines.append(f"### Record {i}")
                for k, v in attrs.items():
                    lines.append(f"  {k}: {v}")
                lines.append("")
            return "\n".join(lines), structured

        # Not found via exact match. Fall back to LIKE on a distinctive
        # digit substring. Skip the first 3 chars (often leading
        # zeros + low-info prefix) to maximise selectivity.
        digits = "".join(c for c in parcel_id if c.isdigit())
        candidates: List[Dict[str, Any]] = []
        substring_used = ""
        if len(digits) >= 5:
            # Pick a 6-char window starting after any leading zeros for
            # distinctiveness; fall back to the longest available.
            stripped = digits.lstrip("0")
            substring_used = (
                stripped[:6] if len(stripped) >= 6 else stripped
            )
            if substring_used:
                safe_sub = substring_used.replace("'", "''")
                like_where = WhereValidator.validate(
                    f"{parcel_field} LIKE '%{safe_sub}%'"
                )
                try:
                    like_resp = await self._send("get",
                        f"{layer_url}/query",
                        params={
                            "f": "json",
                            "where": like_where,
                            "outFields": parcel_field,
                            "returnGeometry": "false",
                            "resultRecordCount": "5",
                        },
                    )
                    like_resp.raise_for_status()
                    like_data = like_resp.json()
                    if "error" not in like_data:
                        candidates = like_data.get("features") or []
                except Exception:
                    candidates = []

        lines = [
            f"## Parcel lookup: no exact match for `{parcel_id}`",
            f"**Layer:** `{item_id}`",
            f"**Field:** `{parcel_field}`",
            f"**Tried variants ({len(variants)}):** "
            + ", ".join(f"`{v}`" for v in variants),
            "",
        ]
        if candidates:
            lines.append(
                f"**LIKE fallback** with substring "
                f"`%{substring_used}%` returned "
                f"{len(candidates)} candidate(s):"
            )
            lines.append("")
            for c in candidates:
                attrs = c.get("attributes") or {}
                v = attrs.get(parcel_field)
                lines.append(f"- `{v}`")
            lines += [
                "",
                "_Pick the right candidate above and use its EXACT "
                "value for follow-up queries on this layer._",
            ]
        else:
            lines += [
                "_No candidates via LIKE fallback either. The parcel "
                "may not exist in this layer, or the field stores the "
                "ID in an unrecognised format. Try "
                f"`get_distinct_values(item_id='{item_id}', "
                f"field='{parcel_field}', "
                f"like='{substring_used or digits[:6]}')` to discover "
                f"the storage format._",
            ]

        if candidates:
            miss_message = (
                f"No exact match for {parcel_id!r} in any of "
                f"{len(variants)} format variants. A LIKE search on "
                f"'%{substring_used}%' found {len(candidates)} "
                "candidate(s), listed in `candidates`: pick the right "
                "one and use its EXACT value for follow-up queries on "
                "this layer."
            )
        else:
            miss_message = (
                f"No exact match for {parcel_id!r} and no LIKE "
                "candidates. The parcel may not exist in this layer, or "
                "the field stores the id in an unrecognised format; try "
                f"get_distinct_values(item_id='{item_id}', "
                f"field='{parcel_field}', "
                f"like='{substring_used or digits[:6]}') to discover it."
            )
        caveat: Dict[str, Any] = {
            "code": "parcel_not_found",
            "message": miss_message,
            "count": len(candidates),
        }
        if candidates:
            caveat["candidates"] = [
                {
                    parcel_field: self._scalar(
                        (c.get("attributes") or {}).get(parcel_field)
                    )
                }
                for c in candidates
            ]
        structured = {
            "query": query_echo,
            "summary": {
                "returned": 0,
                "matched": False,
                "matched_values": [],
                "canonical": None,
            },
            "rows": [],
            "caveats": [caveat],
        }
        return "\n".join(lines), structured

    # ── footprint_for_parcel: lot coverage / ADU headroom ────────────────
    #
    # TODO follow-ups (deliberately not built yet):
    # - coverage_for_area(grid_map | community_council): batch rollup that
    #   pages parcels and reuses this per-parcel clip. aggregate_by_polygon
    #   is NOT a substitute -- it pulls source features to a 5,000 cap with
    #   no geographic prefilter and assigns whole buildings by centroid,
    #   double-dumping shared footprints.
    # - Detached-only fast path: server-side outStatistics
    #   SUM(Shape__Area) / 4.26 where Total_Living_Units = 1 and a single
    #   building intersects -- faster for bulk screening.
    # - Pluggable footprint source (FEMA USA Structures / Overture) behind
    #   the same tool signature, for QA / gap-fill.

    # Residential base maximum lot coverage by zoning district, from AMC
    # 21.06.020 Table 21.06-1 (https://ecode360.com/49423901). Hardcoded
    # snapshot of the published table -- TODO: re-sync if Title 21 amends
    # it (the eCode MCP serves the live text but is a separate connector
    # this server cannot call). R4A is intentionally absent: the table
    # sets no maximum lot coverage for it.
    LOT_COVERAGE_CAPS = {
        "R1": 0.40,
        "R1A": 0.40,
        "R2A": 0.40,
        "R2D": 0.40,
        "R2M": 0.40,
        "R3A": 0.50,
        "R3": 0.60,  # townhouse / multifamily districts
        "R4": 0.60,
        "R5": 0.30,
        "R6": 0.30,
        "R7": 0.30,
        "R8": 0.05,
        "R9": 0.10,
    }
    # Table 21.06-1 note 3: in these districts the cap rises to 50% on
    # lots under 10,000 sqft IF the principal structure is < 16 ft tall.
    LOT_COVERAGE_NOTE3_DISTRICTS = frozenset(
        {"R1", "R1A", "R2A", "R2D", "R2M"}
    )
    LOT_COVERAGE_UNRESTRICTED = frozenset({"R4A"})
    NOTE3_LOT_SQFT = 10000.0
    NOTE3_CAP = 0.50

    # Integer scaling for pyclipper: 1 clipper unit = 1 mm in EPSG:3338
    # meters, so area comes back in mm^2 / CLIP_SCALE^2.
    CLIP_SCALE = 1000.0
    # A building that merely grazes a shared lot line clips to a sliver;
    # anything under this area (m^2) still counts toward footprint area
    # but not toward building_count.
    FOOTPRINT_COUNT_MIN_M2 = 1.0
    FOOTPRINT_BUILDING_LIMIT = 500
    # Fields fetched from the assessor parcel layer for the report.
    FOOTPRINT_PARCEL_FIELDS = (
        "Parcel_ID",
        "GIS_ParcelNum11",
        "Parcel_Address",
        "Property_Type",
        "Land_Use",
        "Total_Living_Units",
        "Lot_Size",
        "Zoning_District",
        "Grid_Map",
        "Condo_Unit_Number",
        "Parcel_ID_URL",
    )

    @classmethod
    def _normalize_zoning_district(cls, raw: Any) -> Tuple[str, str]:
        """Normalize a stored ``Zoning_District`` value for cap lookup.

        Returns ``(base_zone, code_area)`` where ``code_area`` is
        ``'bowl'``, ``'chugiak-eagle river'`` (``CE...`` districts,
        AMC ch. 21.10) or ``'girdwood'`` (``G...`` districts, ch.
        21.09). Strips hyphens/spaces and one trailing ``SL``
        (special limitations) suffix. Stored examples: ``R2D``,
        ``R6SL``, ``CER1``, ``CE-R1A``, ``CE RO``, ``GR1``.
        """
        z = re.sub(r"[\s\-]+", "", str(raw or "").upper())
        code_area = "bowl"
        if z.startswith("CE"):
            code_area = "chugiak-eagle river"
            z = z[2:]
        elif z.startswith("G"):
            # No Anchorage Bowl district code starts with G (verified
            # against the live distinct Zoning_District values).
            code_area = "girdwood"
            z = z[1:]
        if z.endswith("SL") and len(z) > 2:
            z = z[:-2]
        return z, code_area

    @classmethod
    def _ring_to_clipper_path(
        cls, ring: List[List[float]]
    ) -> Optional[List[Tuple[int, int]]]:
        """One coordinate ring -> integer pyclipper path (or None)."""
        path = [
            (
                int(round(pt[0] * cls.CLIP_SCALE)),
                int(round(pt[1] * cls.CLIP_SCALE)),
            )
            for pt in ring
        ]
        # Drop the duplicated closing vertex if present.
        if len(path) > 1 and path[0] == path[-1]:
            path.pop()
        return path if len(path) >= 3 else None

    @classmethod
    def _esri_rings_to_clipper_paths(
        cls, rings: List[List[List[float]]]
    ) -> List[List[Tuple[int, int]]]:
        """Esri ``rings`` -> pyclipper paths, orientation preserved.

        Intended for the CLIP side of an intersection with
        PFT_EVENODD, which is orientation-agnostic, so outer rings
        and holes need no normalization.
        """
        paths = []
        for ring in rings or []:
            p = cls._ring_to_clipper_path(ring)
            if p is not None:
                paths.append(p)
        return paths

    @classmethod
    def _geojson_to_clipper_paths(
        cls, geometry: Dict[str, Any]
    ) -> List[List[Tuple[int, int]]]:
        """GeoJSON Polygon/MultiPolygon -> oriented pyclipper paths.

        Each polygon's exterior ring is forced CCW and its holes CW,
        so a PFT_NONZERO fill unions overlapping polygons instead of
        cancelling them (source data orientation is not trusted).
        """
        gtype = (geometry or {}).get("type", "")
        coords = (geometry or {}).get("coordinates") or []
        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = list(coords)
        else:
            return []
        paths = []
        for poly in polys:
            for i, ring in enumerate(poly):
                p = cls._ring_to_clipper_path(ring)
                if p is None:
                    continue
                want_ccw = i == 0  # exterior first, then holes
                if pyclipper.Orientation(p) != want_ccw:
                    p.reverse()
                paths.append(p)
        return paths

    @classmethod
    def _clip_intersection_area_m2(
        cls,
        subject_paths: List[List[Tuple[int, int]]],
        clip_paths: List[List[Tuple[int, int]]],
    ) -> float:
        """Area of (subject ∩ clip) in m^2 via robust integer clipping.

        Subject paths must be oriented (``_geojson_to_clipper_paths``)
        so PFT_NONZERO unions overlapping subject polygons -- summing
        per-polygon clips would double-count overlaps. Holes in the
        solution come back oppositely oriented, so the signed ring
        areas net out correctly.
        """
        if not subject_paths or not clip_paths:
            return 0.0
        pc = pyclipper.Pyclipper()
        pc.AddPaths(subject_paths, pyclipper.PT_SUBJECT, True)
        pc.AddPaths(clip_paths, pyclipper.PT_CLIP, True)
        solution = pc.Execute(
            pyclipper.CT_INTERSECTION,
            pyclipper.PFT_NONZERO,
            pyclipper.PFT_EVENODD,
        )
        area = sum(pyclipper.Area(p) for p in solution)
        return abs(area) / (cls.CLIP_SCALE**2)

    async def _footprint_for_parcel(
        self, args: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        """Lot coverage and footprint headroom for a single parcel.

        Returns (text, structured) conforming to
        FOOTPRINT_OUTPUT_SCHEMA. Unlike the table tools this one
        answers about ONE parcel, so the payload carries `result`
        (an object, or null when no parcel matched) not `rows`."""
        """Real lot coverage + ADU footprint headroom for one parcel.

        Spatially joins MOA building footprints to the parcel polygon
        (the buildings layer has no parcel key), CLIPS each building
        to the lot (attached / zero-lot-line rows share one polygon
        across several platted lots), sums the clipped area in
        EPSG:3338 (Alaska Albers, equal-area), and compares against
        the zoning district's maximum-lot-coverage cap.

        Never trusts ``Shape__Area``: both layers are published in
        Web Mercator (3857), where stored areas run ~4.26x true
        ground area at Anchorage's latitude.
        """
        parcel_id = (args.get("parcel_id") or "").strip()
        if not parcel_id:
            raise ToolInputError("parcel_id is required")
        variants = self._normalize_parcel_variants(parcel_id)
        if not variants:
            raise ToolInputError(
                f"Could not extract any digits from parcel_id "
                f"{parcel_id!r}. Provide a parcel ID like "
                f"'001-213-29', '00121329', or '00121329000'."
            )

        parcel_item_id = self.plugin_config.property_item_id
        parcel_url = await self._resolve_layer_url(parcel_item_id)
        quoted = ",".join(
            "'" + v.replace("'", "''") + "'" for v in variants
        )
        where = WhereValidator.validate(
            f"(GIS_ParcelNum11 IN ({quoted}) OR Parcel_ID IN ({quoted})) "
            f"AND GIS_Category = 'Parcel'"
        )
        data = await self._request_json_with_retry(
            f"{parcel_url}/query",
            method="post",
            data={
                "f": "json",
                "where": where,
                "outFields": ",".join(self.FOOTPRINT_PARCEL_FIELDS),
                "returnGeometry": "true",
                "outSR": "3338",
                "resultRecordCount": "11",
            },
        )
        features = data.get("features") or []

        if not features:
            return "\n".join([
                f"## Lot coverage: no parcel found for `{parcel_id}`",
                f"**Variants tried ({len(variants)}):** "
                + ", ".join(f"`{v}`" for v in variants),
                "",
                f"_No assessment record matched in the parcel layer. "
                f"Try `find_parcel(item_id='{parcel_item_id}', "
                f"parcel_field='Parcel_ID', parcel_id='{parcel_id}')` "
                f"-- its LIKE fallback surfaces near-miss candidates._",
            ]), {
                "query": {"parcel_id": parcel_id},
                "result": None,
                "caveats": [
                    {
                        "code": "parcel_not_found",
                        "count": len(variants),
                        "message": (
                            f"No assessment record matched "
                            f"{parcel_id!r} in the parcel layer "
                            f"after trying {len(variants)} MOA "
                            f"format variants."
                        ),
                    }
                ],
            }

        distinct_ids = {
            (f.get("attributes") or {}).get("Parcel_ID")
            for f in features
        }
        if len(features) > 1 and len(distinct_ids) > 1:
            lines = [
                f"## Lot coverage: `{parcel_id}` matched "
                f"{len(features)} parcel records",
                "",
                "_Multiple distinct parcels matched (typically condo "
                "units under one root number). Condo units have no "
                "independent lot, so coverage is not computed. Pick "
                "one record and re-call with its exact Parcel_ID:_",
                "",
            ]
            candidates = []
            for f in features:
                a = f.get("attributes") or {}
                unit = a.get("Condo_Unit_Number")
                unit_txt = f"  unit: {unit}" if unit else ""
                lines.append(
                    f"- `{a.get('Parcel_ID')}` -- "
                    f"{a.get('Parcel_Address')}{unit_txt}  "
                    f"({a.get('Land_Use')})"
                )
                candidates.append({
                    "parcel_id": a.get("Parcel_ID"),
                    "address": a.get("Parcel_Address"),
                    "unit": unit,
                    "land_use": a.get("Land_Use"),
                })
            # The candidate list must live in the STRUCTURED half too:
            # structured-output clients render only structuredContent,
            # so a message saying "the Parcel_IDs listed" with the list
            # only in the markdown half points at nothing.
            id_preview = ", ".join(
                str(c["parcel_id"]) for c in candidates[:8]
            )
            if len(candidates) > 8:
                id_preview += f", ... and {len(candidates) - 8} more"
            return "\n".join(lines), {
                "query": {"parcel_id": parcel_id},
                "result": None,
                "caveats": [
                    {
                        "code": "parcel_ambiguous",
                        "count": len(features),
                        "message": (
                            f"{len(features)} parcels matched "
                            f"{parcel_id!r}; re-call with one exact "
                            f"Parcel_ID: {id_preview}."
                        ),
                        "candidates": candidates,
                    }
                ],
            }

        feat = features[0]
        attrs = feat.get("attributes") or {}
        addr = attrs.get("Parcel_Address") or "address unknown"
        canonical_id = attrs.get("Parcel_ID") or parcel_id

        try:
            lot_sqft = float(attrs.get("Lot_Size") or 0)
        except (TypeError, ValueError):
            lot_sqft = 0.0
        if lot_sqft <= 0:
            unit = attrs.get("Condo_Unit_Number")
            unit_txt = f" (condo unit {unit})" if unit else ""
            return "\n".join([
                f"## Lot coverage: `{canonical_id}` ({addr}) has no "
                f"independent lot",
                "",
                f"**Land use:** {attrs.get('Land_Use')}{unit_txt}  |  "
                f"**Lot_Size:** {attrs.get('Lot_Size')!r}",
                "",
                "_The assessor records no lot area for this parcel "
                "(typical for condo units and some leases), so lot "
                "coverage and ADU headroom are undefined for it. For "
                "a condo, the buildable envelope belongs to the "
                "common-interest parcel, not the unit._",
            ]), {
                "query": {"parcel_id": parcel_id},
                "result": None,
                "caveats": [
                    {
                        "code": "no_independent_lot",
                        "message": (
                            f"Parcel {canonical_id} ({addr}) has no "
                            f"independent lot area on the assessor "
                            f"record, so lot coverage is undefined "
                            f"for it."
                        ),
                    }
                ],
            }

        geom = feat.get("geometry") or {}
        rings = geom.get("rings") or []
        if not rings:
            raise ToolInputError(
                f"Parcel {canonical_id} has no polygon geometry in "
                f"the parcel layer, so buildings cannot be joined."
            )

        # Spatial join: buildings intersecting the parcel polygon.
        # Both filter and output stay in EPSG:3338 -- the filter via
        # inSR, the geometry via outSR -- so the clip below runs in an
        # equal-area SR and planar area ≈ true ground area.
        bldg_item_id = self.plugin_config.buildings_item_id
        bldg_url = await self._resolve_layer_url(bldg_item_id)
        esri_filter = json.dumps(
            {"rings": rings, "spatialReference": {"wkid": 3338}},
            separators=(",", ":"),
        )
        bldg_feats = await self._paged_geojson_fetch(
            bldg_url,
            where="1=1",
            out_fields="OBJECTID,Category",
            limit=self.FOOTPRINT_BUILDING_LIMIT,
            method="post",
            extra_params={
                "geometry": esri_filter,
                "geometryType": "esriGeometryPolygon",
                "inSR": "3338",
                "spatialRel": "esriSpatialRelIntersects",
                "outSR": "3338",
                # Override the pager's degree-based simplification:
                # 0 = full-detail geometry (units here are meters).
                "maxAllowableOffset": "0",
            },
        )

        # Clip every building to the lot line and take the UNION area
        # (one Execute over all buildings; per-building areas are for
        # the count/detail only, where clip slivers are excluded).
        parcel_paths = self._esri_rings_to_clipper_paths(rings)
        if not parcel_paths:
            raise ToolInputError(
                f"Parcel {canonical_id} geometry has no usable rings."
            )
        all_paths: List[List[Tuple[int, int]]] = []
        building_count = 0
        for f in bldg_feats:
            bpaths = self._geojson_to_clipper_paths(
                f.get("geometry") or {}
            )
            if not bpaths:
                continue
            clipped = self._clip_intersection_area_m2(
                bpaths, parcel_paths
            )
            if clipped >= self.FOOTPRINT_COUNT_MIN_M2:
                building_count += 1
            all_paths.extend(bpaths)
        footprint_m2 = self._clip_intersection_area_m2(
            all_paths, parcel_paths
        )
        footprint_sqft = footprint_m2 * self.M2_TO_SQFT
        coverage = footprint_sqft / lot_sqft

        # Cross-check the assessor Lot_Size against the mapped polygon.
        # Esri ring convention: outer CW (negative shoelace), holes CCW,
        # so the signed sum nets holes out; abs() gives the area.
        parcel_geom_sqft = (
            abs(sum(self._ring_area(r) for r in rings))
            * self.M2_TO_SQFT
        )

        zone_raw = attrs.get("Zoning_District")
        base_zone, code_area = self._normalize_zoning_district(zone_raw)
        bowl_cap = self.LOT_COVERAGE_CAPS.get(base_zone)
        cap: Optional[float] = None
        cap_note = ""
        if code_area != "bowl":
            table = (
                "AMC ch. 21.10 (Chugiak-Eagle River)"
                if code_area == "chugiak-eagle river"
                else "AMC ch. 21.09 (Girdwood)"
            )
            cap_note = (
                f"zoning `{zone_raw}` is a {code_area.title()} "
                f"district -- verify its cap in {table}"
            )
            if bowl_cap is not None:
                cap_note += (
                    f"; the Bowl table value for {base_zone} would be "
                    f"{bowl_cap:.0%} but does not automatically apply"
                )
        elif base_zone in self.LOT_COVERAGE_UNRESTRICTED:
            cap_note = (
                f"{base_zone} has no maximum lot coverage in Table "
                f"21.06-1 (unrestricted)"
            )
        elif bowl_cap is not None:
            cap = bowl_cap
        else:
            cap_note = (
                f"district `{zone_raw}` is not in the residential "
                f"Table 21.06-1 snapshot -- no cap applied"
            )

        max_footprint_sqft: Optional[float] = None
        headroom_sqft: Optional[float] = None
        if cap is not None:
            max_footprint_sqft = lot_sqft * cap
            headroom_sqft = max_footprint_sqft - footprint_sqft

        note3_applies = (
            code_area == "bowl"
            and base_zone in self.LOT_COVERAGE_NOTE3_DISTRICTS
            and lot_sqft < self.NOTE3_LOT_SQFT
        )
        headroom_note3: Optional[float] = None
        if note3_applies:
            headroom_note3 = lot_sqft * self.NOTE3_CAP - footprint_sqft

        # Structured caveats; the italic notes appended to the rendering
        # are generated from this same list further down, so the two
        # cannot drift. Messages are plain text for structured consumers.
        caveats: List[Dict[str, Any]] = [
            {
                "code": "footprint_methodology",
                "message": (
                    "Footprint is building polygons clipped to the "
                    "parcel and unioned (shared attached-housing "
                    "polygons are not dumped whole onto one lot); areas "
                    "computed in EPSG:3338 (equal-area), never from "
                    "Web-Mercator Shape__Area."
                ),
            },
            {
                "code": "headroom_is_footprint_only",
                "message": (
                    "Headroom is the footprint envelope under the "
                    "coverage cap only. Setbacks and the ADU floor-area "
                    "cap (AMC 21.05.070D.1: greater of 900 sqft or 40% "
                    "of the principal dwelling's floor area, max 1,200 "
                    "sqft) are separate constraints -- footprint "
                    "headroom alone does not mean an ADU fits."
                ),
            },
        ]
        if note3_applies:
            caveats.append(
                {
                    "code": "note3_conditional",
                    "message": (
                        "The note-3 50% figure is conditional: it "
                        "requires the principal structure to be under "
                        "16 ft tall, which cannot be verified from GIS "
                        "data (ELEVATION on the buildings layer is "
                        "ground elevation, not height)."
                    ),
                }
            )
        if cap_note:
            caveats.append(
                {"code": "cap_source", "message": f"Cap: {cap_note}."}
            )
        if lot_sqft > 0 and abs(parcel_geom_sqft - lot_sqft) > (
            0.15 * lot_sqft
        ):
            caveats.append(
                {
                    "code": "lot_area_disagreement",
                    "message": (
                        f"The mapped parcel polygon measures "
                        f"{parcel_geom_sqft:,.0f} sqft vs the assessor "
                        f"Lot_Size of {lot_sqft:,.0f} sqft (>15% apart). "
                        f"Coverage uses the assessor figure; treat "
                        f"results with care."
                    ),
                }
            )
        if len(bldg_feats) >= self.FOOTPRINT_BUILDING_LIMIT:
            caveats.append(
                {
                    "code": "building_cap_reached",
                    "limit": self.FOOTPRINT_BUILDING_LIMIT,
                    "message": (
                        f"Hit the {self.FOOTPRINT_BUILDING_LIMIT}-building "
                        f"fetch cap; the footprint may be undercounted."
                    ),
                }
            )

        payload: Dict[str, Any] = {
            "parcel_id": canonical_id,
            "address": addr,
            "zoning_district": zone_raw,
            "zoning_district_base": base_zone,
            "land_use": attrs.get("Land_Use"),
            "total_living_units": attrs.get("Total_Living_Units"),
            "lot_size_sqft": round(lot_sqft),
            "existing_footprint_sqft": round(footprint_sqft),
            "coverage_pct": round(coverage, 3),
            "district_max_coverage": cap,
            "max_footprint_sqft": (
                round(max_footprint_sqft)
                if max_footprint_sqft is not None
                else None
            ),
            "adu_footprint_headroom_sqft": (
                round(headroom_sqft) if headroom_sqft is not None else None
            ),
            "building_count": building_count,
            "buildings_intersecting": len(bldg_feats),
        }
        if note3_applies:
            payload["headroom_if_note3_50pct"] = round(headroom_note3)
            payload["note3_conditional"] = (
                "requires principal structure < 16 ft; "
                "lot < 10,000 sqft"
            )
        if cap_note:
            payload["cap_source"] = cap_note

        lines = [
            f"## Lot coverage: parcel `{canonical_id}` ({addr})",
            f"**Zoning:** {zone_raw}  |  "
            f"**Land use:** {attrs.get('Land_Use')}  |  "
            f"**Living units:** {attrs.get('Total_Living_Units')}",
            f"**Lot size (assessor):** {lot_sqft:,.0f} sqft",
            f"**Building footprint on lot (clipped):** "
            f"{footprint_sqft:,.0f} sqft across {building_count} "
            f"building(s)",
            f"**Coverage:** {coverage:.1%} of lot"
            + (
                f"  |  **District cap:** {cap:.0%} "
                f"(AMC Table 21.06-1)"
                if cap is not None
                else "  |  **District cap:** n/a"
            ),
        ]
        if max_footprint_sqft is not None:
            lines.append(
                f"**Max footprint at cap:** "
                f"{max_footprint_sqft:,.0f} sqft  |  "
                f"**Footprint headroom:** {headroom_sqft:,.0f} sqft"
            )
        if note3_applies:
            lines.append(
                f"**Conditional (note 3):** on lots < 10,000 sqft the "
                f"cap rises to {self.NOTE3_CAP:.0%} IF the principal "
                f"structure is < 16 ft tall -> headroom would be "
                f"{headroom_note3:,.0f} sqft"
            )
        datalet = attrs.get("Parcel_ID_URL")
        if datalet:
            lines.append(f"**Assessor record:** {datalet}")
        lines += [
            "",
            "```json",
            json.dumps(payload, indent=1),
            "```",
            "",
        ]
        lines.extend(f"_{c['message']}_" for c in caveats)
        return "\n".join(lines), {
            "query": {"parcel_id": parcel_id},
            "result": payload,
            "caveats": caveats,
        }
    # ── Output schemas and tool registration ─────────────────────────────

    FOOTPRINT_OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["query", "result", "caveats"],
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "object",
                "required": ["parcel_id"],
                "properties": {"parcel_id": {"type": "string"}},
            },
            "result": {
                "type": ["object", "null"],
                "description": (
                    "One parcel, not a table -- hence `result` rather "
                    "than `rows`. null when no parcel matched, the match "
                    "was ambiguous, or the parcel has no independent "
                    "lot; the caveat code says which."
                ),
                "properties": {
                    "parcel_id": {"type": ["string", "null"]},
                    "address": {"type": ["string", "null"]},
                    "zoning_district": {"type": ["string", "null"]},
                    "zoning_district_base": {"type": ["string", "null"]},
                    "land_use": {"type": ["string", "null"]},
                    "total_living_units": {
                        "type": ["integer", "number", "string", "null"]
                    },
                    "lot_size_sqft": {"type": "number", "minimum": 0},
                    "existing_footprint_sqft": {
                        "type": "number",
                        "minimum": 0,
                    },
                    "coverage_pct": {
                        "type": "number",
                        "minimum": 0,
                        "description": (
                            "A RATIO (0.42 = 42%), not a percentage, "
                            "and uncapped."
                        ),
                    },
                    "district_max_coverage": {
                        "type": ["number", "null"],
                        "description": (
                            "null when the district has no mapped "
                            "coverage cap, which also makes "
                            "max_footprint_sqft and the headroom fields "
                            "null."
                        ),
                    },
                    "max_footprint_sqft": {"type": ["number", "null"]},
                    "adu_footprint_headroom_sqft": {
                        "type": ["number", "null"],
                        "description": (
                            "May be NEGATIVE when the lot is already "
                            "over its cap."
                        ),
                    },
                    "building_count": {
                        "type": ["integer", "number", "string", "null"]
                    },
                    "buildings_intersecting": {
                        "type": "integer",
                        "minimum": 0,
                    },
                    "headroom_if_note3_50pct": {"type": "number"},
                    "note3_conditional": {"type": "string"},
                    "cap_source": {"type": "string"},
                },
            },
            "caveats": ArcGISHubPlugin._CAVEATS_SCHEMA,
        },
    }
    FIND_PARCEL_OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["query", "summary", "rows", "caveats"],
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "parcel_field": {"type": "string"},
                    "parcel_id": {"type": "string"},
                    "variants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Every format variant tried.",
                    },
                    "limit": {"type": "integer", "minimum": 0},
                },
            },
            "summary": {
                "type": "object",
                "required": ["returned", "matched", "canonical"],
                "properties": {
                    "returned": {"type": "integer", "minimum": 0},
                    "matched": {
                        "type": "boolean",
                        "description": (
                            "True when a variant matched exactly. When "
                            "false, see the parcel_not_found caveat's "
                            "`candidates` for near matches."
                        ),
                    },
                    "matched_values": {
                        "type": "array",
                        "items": {"type": ArcGISHubPlugin._SCALAR_TYPES},
                    },
                    "canonical": {
                        "type": ArcGISHubPlugin._SCALAR_TYPES,
                        "description": (
                            "The parcel id exactly as THIS layer stores "
                            "it -- use it verbatim in follow-up "
                            "queries. null when nothing matched."
                        ),
                    },
                },
            },
            "rows": {
                "type": "array",
                "description": (
                    "Raw attribute records in the layer's own types "
                    "(dates are epoch milliseconds)."
                ),
                "items": {"type": "object"},
            },
            "caveats": ArcGISHubPlugin._CAVEATS_SCHEMA,
        },
    }
    # Same schema as the base, with the hosted-layer wording MOA's tools
    # have always used.
    COVERAGE_OUTPUT_SCHEMA = _replace_text(
        ArcGISHubPlugin.COVERAGE_OUTPUT_SCHEMA,
        "Web Mercator on ArcGIS Online hosted layers, i.e.",
        "Web Mercator on MOA hosted layers, i.e.",
    )

    TOOL_TITLES = {
        **ArcGISHubPlugin.TOOL_TITLES,
        "find_parcel": "Find Parcel",
        "footprint_for_parcel": "Lot Coverage for Parcel",
    }

    TOOL_OUTPUT_SCHEMAS = {
        **ArcGISHubPlugin.TOOL_OUTPUT_SCHEMAS,
        "coverage_by_polygon": COVERAGE_OUTPUT_SCHEMA,
        "find_parcel": FIND_PARCEL_OUTPUT_SCHEMA,
        "footprint_for_parcel": FOOTPRINT_OUTPUT_SCHEMA,
    }

    def _tool_definitions(self) -> List[ToolDefinition]:
        """The shared tools plus Anchorage's two parcel tools, in the
        order this server has always listed them."""
        city = (
            self.plugin_config.city_name if self.plugin_config else "Unknown"
        )
        find_parcel = ToolDefinition(
            name="find_parcel",
            description=(
                f"Look up a {city} parcel across the 4 MOA format "
                f"variants (`001-213-29`, `00121329`, "
                f"`00121329000`, `003-184-87-000`) in one query. "
                f"Pass any one form -- hyphens, leading zeros, "
                f"and 'Parcel ' prefixes are normalized. Falls "
                f"back to a LIKE search returning up to 5 "
                f"candidates on no exact match. Pre-flight "
                f"`get_layer_schema(item_id, keyword='parcel')` "
                f"to pick the right `parcel_field` -- usually "
                f"one of: Parcel_Num, Parcel_ID, GIS_ParcelNum8, "
                f"GIS_ParcelNum11."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": (
                            "ArcGIS item ID of a queryable "
                            "Feature/Map Service that has a "
                            "parcel-ID field."
                        ),
                    },
                    "parcel_field": {
                        "type": "string",
                        "description": (
                            "Name of the parcel-ID field on the "
                            "layer. CASE-SENSITIVE. Use "
                            "`get_layer_schema(item_id=<id>, "
                            "keyword='parcel')` to find it."
                        ),
                    },
                    "parcel_id": {
                        "type": "string",
                        "description": (
                            "The parcel ID in any common form: "
                            "'001-213-29', '00121329', "
                            "'00121329000', '003-184-87-000', "
                            "or even '1-213-29' (leading zeros "
                            "filled in). Hyphens and prefixes "
                            "are flexible."
                        ),
                    },
                    "out_fields": {
                        "type": "string",
                        "description": (
                            "Comma-separated field names to "
                            "return for matched records "
                            "(default '*')."
                        ),
                        "default": "*",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max records to return (default 10, "
                            "max 100)."
                        ),
                        "default": 10,
                    },
                },
                "required": [
                    "item_id",
                    "parcel_field",
                    "parcel_id",
                ],
            },
        )
        footprint_for_parcel = ToolDefinition(
            name="footprint_for_parcel",
            description=(
                f"Real building-footprint lot coverage and ADU "
                f"headroom for ONE {city} parcel. Spatially joins "
                f"MOA building footprints to the parcel polygon, "
                f"CLIPS buildings shared across attached / "
                f"zero-lot-line rows to the lot line, computes "
                f"true ground area in an equal-area projection "
                f"(never the distorted Web-Mercator Shape__Area), "
                f"and compares against the zoning district's "
                f"max-lot-coverage cap (AMC Table 21.06-1). Use "
                f"for 'what % of this lot is built', 'can an ADU "
                f"fit', 'how much more footprint is allowed' "
                f"questions. Accepts any MOA parcel ID format. "
                f"For bulk screening across many parcels use "
                f"coverage_by_polygon instead (faster but "
                f"centroid-based, not clipped)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "parcel_id": {
                        "type": "string",
                        "description": (
                            "MOA parcel number in any format: "
                            "'003-264-77', '00326477', "
                            "'00326477000', or "
                            "'003-264-77-000'."
                        ),
                    },
                },
                "required": ["parcel_id"],
            },
        )
        tools = super()._tool_definitions()
        names = [t.name for t in tools]
        tools.insert(names.index("get_distinct_values") + 1, find_parcel)
        tools.append(footprint_for_parcel)
        return tools

    async def _execute_extra_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        if tool_name == "find_parcel":
            return await self._find_parcel(arguments)
        if tool_name == "footprint_for_parcel":
            return await self._footprint_for_parcel(arguments)
        return None
