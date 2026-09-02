############################
# vibecoded renderer below #
############################

import colorsys
import pathlib
import random

from shapely.geometry import Polygon, MultiPolygon


def draw_regions_svg(
    region_lookup: "RegionLookup",
    output_path: str = "regions.svg",
    scale: float = 1.0,
    padding: float = 20.0,
    opacity: float = 0.4,
    color_by_layer: bool = True,
    label_size: float = 12.0,
    color_override_by_region: dict[int, tuple[int, int, int]] = {},
) -> None:
    regions = [
        region
        for region in region_lookup._map.values()
        if not region.deleted
    ]

    geometries = [
        geometry
        for region in regions
        for geometry in region.polygons.values()
        if geometry is not None and not geometry.is_empty
    ]

    if not geometries:
        raise ValueError("No polygons to draw")

    min_x = min(g.bounds[0] for g in geometries)
    min_y = min(g.bounds[1] for g in geometries)
    max_x = max(g.bounds[2] for g in geometries)
    max_y = max(g.bounds[3] for g in geometries)

    width = (max_x - min_x) * scale + 2 * padding
    height = (max_y - min_y) * scale + 2 * padding

    def transform_point(x: float, y: float) -> tuple[float, float]:
        return (
            (x - min_x) * scale + padding,
            (max_y - y) * scale + padding,
        )

    def ring_to_path(ring) -> str:
        points = [
            transform_point(x, y)
            for x, y in ring.coords
        ]

        parts = [
            f"M {points[0][0]:.3f},{points[0][1]:.3f}"
        ]

        for x, y in points[1:]:
            parts.append(f"L {x:.3f},{y:.3f}")

        parts.append("Z")
        return " ".join(parts)

    def polygon_to_path(polygon: Polygon) -> str:
        parts = [ring_to_path(polygon.exterior)]

        for interior in polygon.interiors:
            parts.append(ring_to_path(interior))

        return " ".join(parts)

    def geometry_to_path(geometry) -> str:
        if isinstance(geometry, Polygon):
            polygons = [geometry]
        elif isinstance(geometry, MultiPolygon):
            polygons = geometry.geoms
        else:
            raise TypeError(
                f"Expected Polygon or MultiPolygon, "
                f"got {geometry.geom_type}"
            )

        return " ".join(
            polygon_to_path(polygon)
            for polygon in polygons
        )

    def hsv_color(hue: float) -> str:
        r, g, b = colorsys.hsv_to_rgb(
            hue % 1.0,
            0.65,
            0.95,
        )

        return (
            f"rgb({int(r * 255)},"
            f"{int(g * 255)},"
            f"{int(b * 255)})"
        )

    def color_override(rgb):
        r, g, b = rgb
        return (
            f"rgb({int(r * 255)},"
            f"{int(g * 255)},"
            f"{int(b * 255)})"
        )

    # ------------------------------------------------------------------
    # Layer colors
    # ------------------------------------------------------------------

    if color_by_layer:
        layers = sorted({
            layer
            for region in regions
            for layer in region.polygons
        })

        layer_colors = {
            layer: hsv_color(i / len(layers))
            for i, layer in enumerate(layers)
        }

    # ------------------------------------------------------------------
    # Pick a real geometry vertex for the label.
    # ------------------------------------------------------------------

    def label_vertex(region):
        points = []

        for geometry in region.polygons.values():
            if geometry is None or geometry.is_empty:
                continue

            polygons = (
                [geometry]
                if isinstance(geometry, Polygon)
                else geometry.geoms
            )

            for polygon in polygons:
                # Exclude the duplicated closing coordinate.
                points.extend(polygon.exterior.coords[:-1])

        if not points:
            return None

        if len(points) == 1:
            return points[0]

        # Choose the vertex whose nearest other vertex is furthest away.
        return max(
            points,
            key=lambda p: min(
                (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
                for q in points
                if q != p
            ),
        )

    # ------------------------------------------------------------------
    # SVG
    # ------------------------------------------------------------------

    svg = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width:.3f}" '
            f'height="{height:.3f}" '
            f'viewBox="0 0 {width:.3f} {height:.3f}">'
        ),
    ]

    # Polygons.
    svg.append(
        f'<g fill-rule="evenodd" stroke="none" '
        f'fill-opacity="{opacity}">'
    )

    region_color_map = {
        region.label: random.random() * 360.0
        for region in regions
    }

    for region in regions:
        if color_by_layer:
            for layer, geometry in region.polygons.items():
                if geometry is None or geometry.is_empty:
                    continue

                svg.append(
                    f'<path '
                    f'd="{geometry_to_path(geometry)}" '
                    f'fill="{layer_colors[layer]}"/>'
                )
        else:
            # Directly derived from the region label.
            # color = hsv_color(hash(region.label) / 360.0)
            if region.label in color_override_by_region:
                color = color_override(color_override_by_region[region.label])
            else:
                color = hsv_color(region_color_map[region.label])

            for geometry in region.polygons.values():
                if geometry is None or geometry.is_empty:
                    continue

                svg.append(
                    f'<path '
                    f'd="{geometry_to_path(geometry)}" '
                    f'fill="{color}"/>'
                )

    svg.append("</g>")

    # Labels.
    #
    # label_size is specified in the same units as the original geometry,
    # so it gets scaled along with the geometry.
    # font_size = label_size * scale
    # label_offset = 6 * scale

    # svg.append(
    #     '<g '
    #     'fill="black" '
    #     'stroke="white" '
    #     f'stroke-width="{max(1, scale):.3f}" '
    #     'paint-order="stroke" '
    #     'font-family="sans-serif" '
    #     'font-weight="bold" '
    #     'text-anchor="start" '
    #     'dominant-baseline="auto" '
    #     'pointer-events="none">'
    # )

    # for region in regions:
    #     point = label_vertex(region)

    #     if point is None:
    #         continue

    #     x, y = transform_point(*point)

    #     svg.append(
    #         f'<text '
    #         f'x="{x + label_offset:.3f}" '
    #         f'y="{y - label_offset:.3f}" '
    #         f'font-size="{font_size:.3f}">'
    #         f'{region.label}'
    #         f'</text>'
    #     )
    # svg.append("</g>")

    svg.append("</svg>")

    pathlib.Path(output_path).write_text(
        "\n".join(svg),
        encoding="utf-8",
    )
