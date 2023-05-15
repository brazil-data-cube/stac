#
# This file is part of BDC-STAC.
# Copyright (C) 2022 INPE.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.
#

"""STAC data management.

This module describes the internal queries and utilities to retrieve STAC definitions
collections items and artifacts from BDC-Catalog.

.. versionadded:: 1.0

    Integrate with BDC-Catalog v1.0+ and role system support.
"""
import warnings
from datetime import datetime as dt
from functools import lru_cache
from typing import List, Optional
from urllib.parse import urljoin

import shapely.geometry
from bdc_catalog.models import Band, Collection, CompositeFunction, GridRefSys, Item, ItemsProcessors, Tile, Timeline
from flask import abort, current_app, request
from flask_sqlalchemy import Pagination, SQLAlchemy
from geoalchemy2.shape import to_shape
from sqlalchemy import Float, and_, cast, exc, func, or_

from .config import (
    BDC_STAC_API_VERSION,
    BDC_STAC_BASE_URL,
    BDC_STAC_FILE_ROOT,
    BDC_STAC_MAX_LIMIT,
    BDC_STAC_USE_FOOTPRINT,
    get_stac_extensions,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", category=exc.SAWarning)


db = SQLAlchemy()

session = db.create_scoped_session({"autocommit": True})

DATETIME_RFC339 = "%Y-%m-%dT%H:%M:%S.%fZ"


def get_collection_items(
    collection_id=None,
    roles=None,
    item_id=None,
    bbox=None,
    datetime=None,
    ids=None,
    collections=None,
    intersects=None,
    page=1,
    limit=10,
    query=None,
    **kwargs,
) -> Pagination:
    """Retrieve a list of collection items based on filters.

    :param collection_id: Single Collection ID to include in the search for items.
                          Only Items in one of the provided Collection will be searched, defaults to None
    :type collection_id: str, optional
    :param item_id: item identifier, defaults to None
    :type item_id: str, optional
    :param bbox: bounding box for intersection [west, north, east, south], defaults to None
    :type bbox: list, optional
    :param datetime: Single date+time, or a range ("/" seperator), formatted to RFC 3339, section 5.6.
                     Use double dots ".." for open date ranges, defaults to None. If the start or end date of an image
                     generated by a temporal composition intersects the given datetime or range it will be included in the
                     result.
    :type datetime: str, optional
    :param ids: Array of Item ids to return. All other filter parameters that further restrict the
                number of search results are ignored, defaults to None
    :type ids: list, optional
    :param collections: Array of Collection IDs to include in the search for items.
                        Only Items in one of the provided Collections will be searched, defaults to None
    :type collections: list, optional
    :param intersects: Searches items by performing intersection between their geometry and provided GeoJSON geometry.
                       All GeoJSON geometry types must be supported., defaults to None
    :type intersects: dict, optional
    :param page: The page offset of results, defaults to 1
    :type page: int, optional
    :param limit: The maximum number of results to return (page size), defaults to 10
    :type limit: int, optional
    :param query: The STAC extra query internal properties
    :type query: dict, optional
    :return: list of collectio items
    :rtype: list
    """
    exclude = kwargs.get("exclude", [])

    columns = [
        Collection.identifier.label("collection"),
        Collection.collection_type,
        Collection.category,
        Item.metadata_.label("item_meta"),
        Item.name.label("item"),
        Item.id,
        Item.collection_id,
        Item.start_date.label("start"),
        Item.end_date.label("end"),
        Item.created,
        Item.updated,
        cast(Item.cloud_cover, Float).label("cloud_cover"),
        Item.footprint,
        Item.bbox,
        Tile.name.label("tile"),
    ]

    # For performance, only retrieve assets when required
    if "assets" not in exclude:
        columns.append(Item.assets)

    if roles is None:
        roles = []

    where = [
        Collection.id == Item.collection_id,
        Collection.is_available.is_(True),
        Item.is_available.is_(True),
        _add_roles_constraint(roles),
    ]
    geom_field = Item.footprint if BDC_STAC_USE_FOOTPRINT else Item.bbox

    if ids is not None:
        if isinstance(ids, str):
            ids = ids.split(",")
        where += [Item.name.in_(ids)]
    else:
        if collection_id and collections:
            abort(400, "Invalid parameter. Use collection_id or collections.")

        if collection_id:
            collections = [collection_id]

        if collections:
            collections = collections.split(",") if isinstance(collections, str) else collections

            rows = db.session.query(Collection.id).filter(Collection.identifier.in_(collections)).all()
            where += [Collection.id.in_(c.id for c in rows)]

        if item_id is not None:
            where += [Item.name.like(item_id)]

        if query:
            filters = create_query_filter(query)
            where += filters

        if intersects is not None:
            where += [func.ST_Intersects(func.ST_GeomFromGeoJSON(str(intersects)), geom_field)]
        elif bbox is not None:
            try:
                if isinstance(bbox, str):
                    bbox = bbox.split(",")

                bbox = [float(x) for x in bbox]

                if bbox[0] == bbox[2] or bbox[1] == bbox[3]:
                    raise InvalidBoundingBoxError("")

                where += [
                    func.ST_Intersects(
                        func.ST_MakeEnvelope(bbox[0], bbox[1], bbox[2], bbox[3], 4326),
                        # TODO: Use footprint to intersect or bbox?
                        geom_field,
                    )
                ]
            except (ValueError, InvalidBoundingBoxError) as e:
                abort(400, f"{bbox} is not a valid bbox.")

        if datetime is not None:
            if "/" in datetime:
                matches_open = ("..", "")
                time_start, time_end = datetime.split("/")
                if time_start in matches_open:  # open start
                    date_filter = [or_(Item.start_date <= time_end, Item.end_date <= time_end)]
                elif time_end in matches_open:  # open end
                    date_filter = [or_(Item.start_date >= time_start, Item.end_date >= time_start)]
                else:  # closed range
                    date_filter = [
                        or_(
                            # TODO: Review this legacy date interval comparison
                            and_(Item.start_date >= time_start, Item.start_date <= time_end),
                            and_(Item.end_date >= time_start, Item.end_date <= time_end),
                            and_(Item.start_date < time_start, Item.end_date > time_end),
                        )
                    ]
            else:
                date_filter = [and_(Item.start_date <= datetime, Item.end_date >= datetime)]
            where += date_filter
    outer = [Item.tile_id == Tile.id]
    query = session.query(*columns).outerjoin(Tile, *outer).filter(*where).order_by(Item.start_date.desc(), Item.id)

    result: Pagination = query.paginate(
        page=int(page), per_page=int(limit), error_out=False, max_per_page=BDC_STAC_MAX_LIMIT
    )

    return result


@lru_cache()
def get_collection_eo(collection_id):
    """Get Collection Electro-Optical properties.

    .. note::

        This method uses LRU Cache to improve response time.

    Args:
        collection_id (str): collection identifier
    Returns:
        eo_gsd, eo_bands (tuple(float, dict)):
    """
    bands = Band.query().filter(Band.collection_id == collection_id)
    eo_bands = list()
    eo_gsd = 0.0

    for band in bands:
        band_meta = dict(
            name=band.name,
            common_name=band.common_name,
            description=band.description,
            min=float(band.min_value) if band.min_value is not None else None,
            max=float(band.max_value) if band.max_value is not None else None,
            nodata=float(band.nodata) if band.nodata is not None else None,
            scale=float(band.scale_mult) if band.scale_mult is not None else None,
            scale_add=float(band.scale_add) if band.scale_add is not None else None,
            data_type=band.data_type,
        )
        band_meta.update(band.properties)
        resolutions = band.eo_resolutions
        if resolutions is None:
            current_app.logger.warning(f"No resolution configured for {band.collection.name} - Band {band.name}")
            continue

        eo_bands.append(band_meta)
        if resolutions[0] > eo_gsd:
            eo_gsd = resolutions[0]

    return {"eo:gsd": eo_gsd, "eo:bands": eo_bands}


@lru_cache()
def get_collection_crs(collection: Collection) -> str:
    """Retrieve the CRS for a given collection.

    By default, this method uses the grid reference system to retrieve collection crs.
    When no grid is set, tries to seek for property ``bdc:crs`` in Collection.properties.

    :param collection: The BDC Collection object
    :type collection: Collection
    :return: CRS for the collection
    :rtype: str
    """
    crs = None
    if collection.grs is not None:
        crs = collection.grs.crs
    elif collection.properties is not None:
        crs = collection.properties.get("bdc:crs")
    return crs


def format_timeline(timeline: Optional[List[Timeline]] = None):
    """Format the collection timeline values with Dateformat.

    :param timeline: The collection timeline instance
    :type timeline: Optional[List[Timeline]]
    :return: list of dates for the collection
    :rtype: list
    """
    if timeline is None:
        return []
    return [dt.fromisoformat(str(t.time_inst)).strftime("%Y-%m-%d") for t in timeline]


@lru_cache()
def get_collection_quicklook(collection_id):
    """Retrieve a list of bands used to create the quicklook for a given collection.

    :param collection_id: collection identifier
    :type collection_id: str
    :return: list of bands
    :rtype: list.
    """
    quicklook_bands = session.execute(
        "SELECT  array[r.name, g.name, b.name] as quicklooks "
        "FROM bdc.quicklook q "
        "INNER JOIN bdc.bands r ON q.red = r.id "
        "INNER JOIN bdc.bands g ON q.green = g.id "
        "INNER JOIN bdc.bands b ON q.blue = b.id "
        "INNER JOIN bdc.collections c ON q.collection_id = c.id "
        "WHERE c.id = :collection_id",
        {"collection_id": collection_id},
    ).fetchone()

    return quicklook_bands["quicklooks"] if quicklook_bands else None


def get_collections(collection_id=None, roles=None, assets_kwargs=None):
    """Retrieve information of all collections or one if an id is given.

    :param collection_id: collection identifier
    :type collection_id: str
    :return: list of collections
    :rtype: list
    """
    columns = [
        Collection,
        CompositeFunction.name.label("composite_function"),
        GridRefSys.name.label("grid_ref_sys"),
    ]

    if roles is None:
        roles = []

    where = [Collection.is_available.is_(True), _add_roles_constraint(roles)]

    if collection_id:
        where.append(func.concat(Collection.name, "-", Collection.version) == collection_id)

    q = (
        session.query(*columns)
        .outerjoin(CompositeFunction, Collection.composite_function_id == CompositeFunction.id)
        .outerjoin(GridRefSys, Collection.grid_ref_sys_id == GridRefSys.id)
        .filter(*where)
    )
    result = q.all()

    collection_map = {row.Collection.id: row.Collection for row in result}

    if collection_id and result:  # shallow query property to generate traceability
        where = [Collection.is_available.is_(True), _add_roles_constraint(roles)]
        _collection_relation = (
            db.session.query(Collection.id, Collection.identifier, Collection.title).filter(*where).all()
        )
        collection_map = {row.id: row for row in _collection_relation}

    collections = list()
    default_stac_extensions = get_stac_extensions("version", "processing", "item-assets")

    for r in result:
        category = r.Collection.category

        providers = [provider.to_dict() for provider in r.Collection.providers]

        collection_extensions = []
        if r.Collection.collection_type == "datacube":
            collection_extensions.append("datacube")

        if category == "sar" or category == "eo":
            collection_extensions.append(category)

        successor: Optional[Collection] = None

        extra_links = []
        if r.Collection.version_successor is not None:
            successor: Collection = collection_map.get(r.Collection.version_successor)
            if successor:
                extra_links.append(_collection_link(successor, rel="successor-version", qs=assets_kwargs))

        if r.Collection.version_predecessor is not None:
            predecessor: Collection = collection_map.get(r.Collection.version_predecessor)
            if predecessor:
                extra_links.append(_collection_link(predecessor, rel="predecessor-version", qs=assets_kwargs))

        meta = r.Collection.metadata_
        deprecated = successor is not None or (meta and meta.get("deprecated", False))

        collection = {
            "id": r.Collection.identifier,
            "type": "Collection",
            "stac_version": BDC_STAC_API_VERSION,
            "stac_extensions": default_stac_extensions + get_stac_extensions(*collection_extensions),
            "title": r.Collection.title,
            "version": r.Collection.version,
            "deprecated": deprecated,
            "description": r.Collection.description,
            "keywords": r.Collection.keywords,
            "providers": providers,
            "summaries": r.Collection.summaries,
            "item_assets": r.Collection.item_assets,
            "properties": r.Collection.properties or {},
            "bdc:type": r.Collection.collection_type,
        }
        collection["properties"]["created"] = r.Collection.created.strftime(DATETIME_RFC339)
        collection["properties"]["updated"] = r.Collection.updated.strftime(DATETIME_RFC339)

        if r.Collection.grs:
            collection["bdc:grs"] = r.Collection.grs.name
        if r.Collection.composite_function:
            collection["bdc:composite_function"] = r.composite_function

        collection["license"] = collection["properties"].pop("license", "")
        extra_links.extend(collection["properties"].pop("links", []))

        bbox = to_shape(r.Collection.spatial_extent).bounds if r.Collection.spatial_extent else [None] * 4

        start, end = None, None

        if r.Collection.start_date:
            start = r.Collection.start_date.strftime(DATETIME_RFC339)
            if r.Collection.end_date:
                end = r.Collection.end_date.strftime(DATETIME_RFC339)

        collection["extent"] = {
            "spatial": {"bbox": [bbox]},
            "temporal": {"interval": [[start, end]]},
        }

        quicklooks = get_collection_quicklook(r.Collection.id)

        if quicklooks is not None:
            collection["bdc:bands_quicklook"] = quicklooks

        if category == "eo":
            collection_eo = get_collection_eo(r.Collection.id)
            collection["properties"].update(collection_eo)

        if meta:
            collection["bdc:metadata"] = meta

        if r.Collection.collection_type == "cube":
            proj4text = get_collection_crs(r.Collection)

            datacube = {
                "x": dict(type="spatial", axis="x", extent=[bbox[0], bbox[2]], reference_system=proj4text),
                "y": dict(type="spatial", axis="y", extent=[bbox[1], bbox[3]], reference_system=proj4text),
                "temporal": dict(type="temporal", extent=[start, end], values=format_timeline(r.Collection.timeline)),
            }
            if category == "eo":
                datacube["bands"] = dict(type="bands", values=[band["name"] for band in collection_eo["eo:bands"]])

            collection["cube:dimensions"] = datacube
            collection["bdc:crs"] = proj4text
            collection["bdc:temporal_composition"] = r.Collection.temporal_composition_schema
            collection["stac_extensions"].extend(get_stac_extensions("datacube"))

        collection["links"] = [
            {
                "href": f"{resolve_stac_url()}/collections/{r.Collection.identifier}{assets_kwargs}",
                "rel": "self",
                "type": "application/json",
                "title": "Link to this document",
            },
            {
                "href": f"{resolve_stac_url()}/collections/{r.Collection.identifier}/items{assets_kwargs}",
                "rel": "items",
                "type": "application/json",
                "title": f"Items of the collection {r.Collection.identifier}",
            },
            {
                "href": f"{resolve_stac_url()}/collections{assets_kwargs}",
                "rel": "parent",
                "type": "application/json",
                "title": "Link to catalog collections",
            },
            {
                "href": f"{resolve_stac_url()}/{assets_kwargs}",
                "rel": "root",
                "type": "application/json",
                "title": "API landing page (root catalog)",
            },
            # Add extra links like license etc.
            *extra_links,
        ]

        collections.append(collection)

    return collections


def get_catalog(roles=None):
    """Retrieve all available collections.

    :return: a list of available collections
    :rtype: list
    """
    if not roles:
        roles = []

    q = session.query(
        Collection.id,
        func.concat(Collection.name, "-", Collection.version).label("name"),
        Collection.title,
    ).filter(Collection.is_available.is_(True), _add_roles_constraint(roles))
    return q.all()


def make_geojson(items, assets_kwargs="", exclude=None):
    """Generate a list of STAC Items from a list of collection items.

    param items: collection items to be formated as GeoJSON Features
    type items: list
    param extension: The STAC extension for Item Context (sar/eo/label).
    type extension: str
    return: GeoJSON Features.
    rtype: list
    """
    features = list()
    exclude = exclude or []

    for i in items:
        geom = i.footprint or i.bbox
        geom = shapely.geometry.mapping(to_shape(geom))
        feature = {
            "type": "Feature",
            "id": i.item,
            "collection": i.collection,
            "stac_version": BDC_STAC_API_VERSION,
            "stac_extensions": get_stac_extensions(i.category),
            "geometry": geom,
            "links": [
                {
                    "href": f"{resolve_stac_url()}/collections/{i.collection}/items/{i.item}{assets_kwargs}",
                    "rel": "self",
                },
                {"href": f"{resolve_stac_url()}/collections/{i.collection}{assets_kwargs}", "rel": "parent"},
                {"href": f"{resolve_stac_url()}/collections/{i.collection}{assets_kwargs}", "rel": "collection"},
                {"href": f"{resolve_stac_url()}/", "rel": "root"},
            ],
        }

        # Processors
        processors = get_item_processors(i.id)
        if processors:
            feature["stac_extensions"].extend(get_stac_extensions("processing"))

        _item_url_resolver = _resolve_item_file_root(i)

        bbox = list()
        if i.bbox:
            bbox = to_shape(i.bbox).bounds
        feature["bbox"] = bbox

        properties = {
            "datetime": i.start.strftime(DATETIME_RFC339),
            "start_datetime": i.start.strftime(DATETIME_RFC339),
            "end_datetime": i.end.strftime(DATETIME_RFC339),
            "created": i.created.strftime(DATETIME_RFC339),
            "updated": i.updated.strftime(DATETIME_RFC339),
        }
        properties.update(i.item_meta or {})
        properties.update(processors)

        bands = {}
        if i.tile:
            properties["bdc:tiles"] = [i.tile]

        if i.category == "eo":
            properties["eo:cloud_cover"] = i.cloud_cover
            bands = get_collection_eo(i.collection_id)

        if i.assets:
            for key, value in i.assets.items():
                value["href"] = urljoin(_item_url_resolver(), value["href"] + assets_kwargs)

                if i.category == "eo":
                    for band in bands["eo:bands"]:
                        if band["name"] == key:
                            value["eo:bands"] = [band]
            feature["assets"] = i.assets

        feature["properties"] = properties
        if feature["properties"].get("storage:platform"):
            feature["stac_extensions"].extend(get_stac_extensions("storage"))

        for key in exclude:
            feature.pop(key, None)

        features.append(feature)
    return features


def get_item_processors(item_id: int) -> dict:
    """List the Processors used to compose the given Item.

    Note:
         Follows the STAC Extension `processing <https://github.com/stac-extensions/processing>`_.
    """
    processors = ItemsProcessors.get_processors(item_id)
    proc_root = None
    processors_obj = {}
    for proc in processors:
        if proc_root is None or (proc_root is not None and proc.level > proc_root.level):
            proc_root = proc
        processors_obj[proc.facility] = proc.version

    out = {}
    if processors_obj:
        out["processing:lineage"] = proc_root.name
        out["processing:facility"] = proc_root.facility
        out["processing:level"] = proc_root.level
        out["processing:software"] = processors_obj

    return out


def create_query_filter(query):
    """Create SQLAlchemy statement filter for Item metadata.

    This function creates a SQLAlchemy filter mapping object to deal
    with Item properties. With this, the user may filter any property from
    STAC item `properties` context, according the spec.

    Example:
        >>> # Create a statement to filter items which has the cloud cover less than 50 percent.
        >>> create_query_filter({"eo:cloud_cover": {"lte": 50}})  # doctest: +SKIP
        >>> # Create a statement to filter items which has the tile MGRS 23LLG
        >>> create_query_filter({"bdc:tile": {"eq": "23LLG"}})  # doctest: +SKIP

    .. note::

        Queryable properties must be mapped in these functions.

    .. tip::

        You may face limitation when filtering for any non-indexed property.
        See `PostgreSQL Indexes <https://www.postgresql.org/docs/current/indexes.html>`_
        to improve any property you need.
    """
    mapping = {
        "eq": "__eq__",
        "neq": "__ne__",
        "lt": "__lt__",
        "lte": "__le__",
        "gt": "__gt__",
        "gte": "__ge__",
        "startsWith": "startswith",
        "endsWith": "endswith",
        "contains": "contains",
        "in": "in_",
    }

    bdc_properties = {
        "bdc:tile": Tile.name,
        "bdc:tiles": Tile.name,  # Legacy: for compatibility
        "eo:cloud_cover": Item.cloud_cover,
    }

    filters = []

    for column, _filters in query.items():
        for op, value in _filters.items():
            if bdc_properties.get(column):
                f = getattr(bdc_properties[column], mapping[op])(value)
            # TODO: Remove the hard-code for comparison on JSON fields (Only text comparisons)
            else:
                f = getattr(Item.metadata_[column].astext, mapping[op])(value)
            filters.append(f)

    return filters


def parse_fields_parameter(fields: Optional[str] = None):
    """Parse the string parameter `fields` to include/exclude certain fields in response.

    Follow the `STAC API Fields Fragment <https://github.com/radiantearth/stac-api-spec/blob/v1.0.0-rc.1/fragments/fields/README.md>`_.
    """
    if fields is None:
        return [], []

    include = []
    exclude = []
    fields = fields.split(",")

    for field in fields:
        if field.startswith("-"):
            splitter = field.split(".")
            left = splitter[0][1:]
            exclude.append((left, splitter[1:]) if len(splitter) > 1 else left)
        else:
            include.append(field)

    return include, exclude


class InvalidBoundingBoxError(Exception):
    """Exception for malformed bounding box."""

    def __init__(self, description):
        """Initialize exception with a description.

        :param description: exception description.
        :type description: str
        """
        super(InvalidBoundingBoxError, self).__init__()
        self.description = description

    def __str__(self):
        """:return: str representation of the exception."""
        return str(self.description)


def resolve_base_file_root_url() -> str:
    """Retrieve base URL used as STAC BASE URL ROOT for items from HTTP header.

    Note:
        This method uses ``flask.request`` object to check for ``X-Script-Name`` in header.
        Make sure you are inside flask app context.
    """
    return request.headers.get("X-Script-Name", BDC_STAC_FILE_ROOT)


def resolve_stac_url() -> str:
    """Retrieve base URL used as STAC URL for items from HTTP header.

    Note:
        This method uses ``flask.request`` object to check for ``X-Stac-Url`` in header.
        Make sure you are inside flask app context.
    """
    return request.headers.get("X-Stac-Url", BDC_STAC_BASE_URL).rstrip("/")


def _resolve_item_file_root(ctx):
    _fn = resolve_base_file_root_url

    if ctx.item_meta is not None:
        for prop in ctx.item_meta.keys():
            if prop.startswith("storage:"):
                # Return Empty string since the asset[href] must be absolute
                # s3://<bucket>/.../file.tif
                _fn = lambda: ""
                break
    return _fn


def _add_roles_constraint(roles: List[str]):
    """Add SQLAlchemy roles constraint for db queries.

    .. versionadded: 1.0

    Expand the given roles and generate SQLAlchemy query condition
    to restrict access for internal collections on BDC-Catalog.
    A role may have the following signature:

    - ``Name-Version``: Give access for specific collections: ``S2_L2A-1``, ``S2-16D-2``.
    - ``*``: Give free access to the all collections in database.

    For special treatment, use `*` to specify free access to the resources.

    Args:
        roles
    """
    where = []
    if "*" not in roles:
        where.append(Collection.identifier.in_(roles) if len(roles) > 0 else False)

    return or_(Collection.is_public.is_(True), *where)


def _collection_link(collection: Collection, rel: str, qs: str):
    """Build STAC collection link for predecessor and successor."""
    return {
        "href": f"{resolve_stac_url()}/collections/{collection.identifier}{qs}",
        "rel": rel,
        "type": "application/json",
        "title": collection.title,
    }
