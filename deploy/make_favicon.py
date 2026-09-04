#!/usr/bin/env python3
"""Render static/favicon.png from the same geometry as static/favicon.svg.

Safari and a few older browsers ignore SVG favicons, so a raster fallback is
worth having. The host has no ImageMagick, rsvg or Pillow, so this draws the
shape directly and writes the PNG with zlib and struct from the stdlib.

    python3 deploy/make_favicon.py

Re-run it if favicon.svg changes; the two are meant to match.
"""
import os
import struct
import zlib

SIZE = 32
SS = 4  # supersampling factor per axis, for antialiased edges
TILE = (0x2F, 0x5F, 0xD8)  # --accent in the light palette
GLYPH = (0xFF, 0xFF, 0xFF)
RADIUS = 5.0 / 24.0  # rounded-rect corner radius, as a fraction of the side


def quad(p0, c, p1, steps):
    """Points along a quadratic Bezier, matching the curved sides of the SVG."""
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append(
            (
                u * u * p0[0] + 2 * u * t * c[0] + t * t * p1[0],
                u * u * p0[1] + 2 * u * t * c[1] + t * t * p1[1],
            )
        )
    return out


# Shield outline in the SVG's 24x24 coordinate space, clockwise from the apex.
SHIELD = (
    [(12.0, 4.0), (18.3, 6.4), (18.3, 11.9)]
    + quad((18.3, 11.9), (17.8, 17.6), (12.0, 20.5), 12)[1:]
    + quad((12.0, 20.5), (6.2, 17.6), (5.7, 11.9), 12)[1:]
    + [(5.7, 6.4)]
)


def in_polygon(x, y, poly):
    """Even-odd ray cast."""
    inside = False
    j = len(poly) - 1
    for i, (xi, yi) in enumerate(poly):
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_rounded_rect(x, y, side, r):
    """x, y in [0, side]; r is the corner radius in the same units."""
    cx = min(max(x, r), side - r)
    cy = min(max(y, r), side - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render():
    rows = []
    r = RADIUS * SIZE
    scale = SIZE / 24.0
    for py in range(SIZE):
        row = []
        for px in range(SIZE):
            tile_hits = 0
            glyph_hits = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = px + (sx + 0.5) / SS
                    y = py + (sy + 0.5) / SS
                    if in_rounded_rect(x, y, SIZE, r):
                        tile_hits += 1
                        if in_polygon(x / scale, y / scale, SHIELD):
                            glyph_hits += 1
            total = SS * SS
            alpha = tile_hits / total
            if alpha == 0:
                row.append((0, 0, 0, 0))
                continue
            # blend the glyph over the tile, then apply the tile's own coverage
            g = glyph_hits / tile_hits
            colour = tuple(
                round(TILE[i] * (1 - g) + GLYPH[i] * g) for i in range(3)
            )
            row.append(colour + (round(alpha * 255),))
        rows.append(row)
    return rows


def write_png(path, rows):
    height = len(rows)
    width = len(rows[0])
    raw = b"".join(
        b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in rows
    )

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(png)
    return len(png)


if __name__ == "__main__":
    target = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static",
        "favicon.png",
    )
    size = write_png(target, render())
    print("wrote %s (%d bytes, %dx%d)" % (target, size, SIZE, SIZE))
