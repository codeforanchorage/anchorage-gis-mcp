"""Pydantic configuration schema for Anchorage GIS plugin."""

from pydantic import Field

from arcgis_hub.config_schema import ArcGISHubPluginConfig


class AnchorageGISPluginConfig(ArcGISHubPluginConfig):
    """Configuration schema for Anchorage GIS plugin.

    The shared fields (portal_base_url, gallery_group_id, org_id,
    city_name, gallery_url, timeout) and their validators come from
    opencontext-arcgis-hub. Added here: the two Municipality of Anchorage
    layers footprint_for_parcel reads.
    """

    property_item_id: str = Field(
        default="57d6ff611f444d75a1bf2b4a1d340163",
        description=(
            "Portal item ID of the assessor parcel layer "
            "(PropertyInformation_Hosted) used by footprint_for_parcel"
        ),
    )
    buildings_item_id: str = Field(
        default="56d274594cbc4aa8bac4257edd06d6cf",
        description=(
            "Portal item ID of the building-footprints layer "
            "(Buildings_Hosted) used by footprint_for_parcel"
        ),
    )
