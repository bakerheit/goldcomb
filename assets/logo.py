#!/usr/bin/env python3
"""Generate the Goldcomb logo, on a transparent background.

Three concepts, selected with the CONCEPT switch below:

  "gradient" (v3, default) — the v2 mark with a REAL gradient: the same
             single solid pointy-top hexagon + hairline negative-space inner
             outline, but filled with a three-stop antique/champagne gold
             sweep, raked diagonally so light feels like it's moving across
             a polished metal face (bright champagne at the top-left, deep
             antique at the bottom-right).

  "minimal"  (v2, preserved) — ONE mark: a single solid pointy-top hexagon
             in matte antique gold, pierced by a hairline negative-space
             inner outline, with a whisper of vertical tonal variation
             (antique gold -> champagne gold). Two-gold palette.

  "cluster"  (v1, preserved) — the original 7-cell honeycomb cluster with
             bright "AI core" center cell, gold gradient, and soft glow.

All render at SUPERSAMPLE x final size and LANCZOS-downsample for smooth
edges. All geometry/color parameters are at the top for easy iteration.
"""
import math
from PIL import Image, ImageDraw

# --- Concept switch ---------------------------------------------------------
CONCEPT = "gradient"                   # "gradient" (v3) | "minimal" (v2) | "cluster" (v1)
OUTPUT = "assets/logo.png"             # v3 ships as logo.png
OUTPUT_MINIMAL = "assets/logo-v2-minimal.png"
OUTPUT_CLUSTER = "assets/logo-v1-cluster.png"

# --- Shared parameters ------------------------------------------------------
FINAL_SIZE = 1024
SUPERSAMPLE = 4                        # render at 4096, downsample to 1024
CANVAS = FINAL_SIZE * SUPERSAMPLE

# --- v2 "minimal" parameters ------------------------------------------------
# Single solid hexagon; coverage ~55% so the mark breathes at small sizes.
V2_HEX_RADIUS = int(FINAL_SIZE * 0.29 * SUPERSAMPLE)   # circumradius, pointy-top
V2_COLOR_TOP = (201, 162, 39)          # #C9A227  antique gold (top of mark)
V2_COLOR_BOTTOM = (212, 175, 55)       # #D4AF37  champagne gold (bottom)
V2_INNER_SCALE = 0.76                  # inner hairline hexagon, fraction of outer
V2_INNER_WIDTH = 3 * SUPERSAMPLE       # hairline stroke (transparent negative space)

# --- v3 "gradient" parameters -----------------------------------------------
# Same silhouette/geometry as v2; only the fill changes. Three-stop gold
# sweep, interpolated along a diagonal axis so light rakes across the mark
# from the top-left highlight to the bottom-right shadow — a polished metal
# face rather than a flat vertical fade. Rich, not gaudy: all stops stay in
# the antique/champagne gold family, no yellow saturation spikes.
V3_GRADIENT_STOPS = [
    (232, 199, 102),                   # #E8C766  light champagne (highlight, top-left)
    (212, 175, 55),                    # #D4AF37  champagne gold (mid)
    (166, 124, 27),                    # #A67C1B  deep antique gold (shadow, bottom-right)
]
V3_GRADIENT_ANGLE = 45.0               # degrees; 0 = vertical, 45 = diagonal rake
V3_GRADIENT_BANDS = 512                # band slices (>= 256 keeps 8-bit smooth)

# --- v1 "cluster" parameters (unchanged) ------------------------------------
CELL_RADIUS = 138 * SUPERSAMPLE        # hexagon circumradius (center -> vertex)
STROKE_WIDTH = 3 * SUPERSAMPLE         # dark gold line between cells
COLOR_CORE = (255, 209, 102)           # #FFD166  bright gold (AI core)
COLOR_RING_LIGHT = (240, 180, 41)      # #F0B429  light gold (top of ring)
COLOR_RING_DARK = (184, 134, 11)       # #B8860B  dark goldenrod (bottom)
COLOR_STROKE = (138, 94, 11)           # #8A5E0B  darker gold outlines
COLOR_GLOW = (255, 233, 168)           # #FFE9A8  soft glow behind the core
GLOW_RADIUS = int(CELL_RADIUS * 2.2)
GLOW_ALPHA = 110

# --- Helpers ----------------------------------------------------------------
def hex_points(cx, cy, r, rotation=0):
    """Hexagon vertices; rotation=0 flat-top, rotation=30 pointy-top."""
    return [(cx + r * math.cos(math.radians(rotation + a)),
             cy + r * math.sin(math.radians(rotation + a))) for a in range(0, 360, 60)]

def lerp(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))

# --- v2: single minimal hexagon ---------------------------------------------
def render_minimal():
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    cx = cy = CANVAS // 2
    r = V2_HEX_RADIUS
    pts = hex_points(cx, cy, r, rotation=30)      # pointy-top: calm, jewel-like

    # Whisper of vertical tonal variation across the solid mark (two golds,
    # band-sliced so the silhouette stays mathematically crisp).
    ys = [p[1] for p in pts]
    top, bot = min(ys), max(ys)
    d = ImageDraw.Draw(img)
    bands = 96
    for i in range(bands):
        y0 = top + (bot - top) * i / bands
        y1 = top + (bot - top) * (i + 1) / bands + 1
        col = lerp(V2_COLOR_TOP, V2_COLOR_BOTTOM, i / (bands - 1))
        d.rectangle([0, y0, CANVAS, y1], fill=col + (255,))
    mask = Image.new("L", (CANVAS, CANVAS), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    img.putalpha(mask)

    # One hairline inner outline, punched out as negative space.
    inner = Image.new("L", (CANVAS, CANVAS), 0)
    ImageDraw.Draw(inner).polygon(
        hex_points(cx, cy, r * V2_INNER_SCALE, rotation=30),
        outline=255, width=V2_INNER_WIDTH)
    img.putalpha(Image.composite(
        Image.new("L", (CANVAS, CANVAS), 0), mask, inner))
    return img

def lerp_stops(stops, t):
    """Interpolate through a list of color stops at position t in [0, 1]."""
    t = max(0.0, min(1.0, t)) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    return lerp(stops[i], stops[i + 1], t - i)

# --- v3: v2 silhouette + richer raked gradient --------------------------------
def render_gradient():
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    cx = cy = CANVAS // 2
    r = V2_HEX_RADIUS
    pts = hex_points(cx, cy, r, rotation=30)      # same pointy-top silhouette as v2

    # Raking light: project every pixel onto a diagonal axis through the
    # mark's center, normalize by the axis extents, then fill with the
    # three-stop gold sweep. Band-sliced perpendicular to the axis so the
    # silhouette stays mathematically crisp.
    ang = math.radians(V3_GRADIENT_ANGLE)
    ux, uy = math.sin(ang), math.cos(ang)         # axis: 0 deg = top->bottom,
                                                  # 45 deg = top-left -> bottom-right
    proj = [(x - cx) * ux + (y - cy) * uy for x, y in pts]
    p_min, p_max = min(proj), max(proj)
    bands = V3_GRADIENT_BANDS
    d = ImageDraw.Draw(img)
    for i in range(bands):
        t0, t1 = i / bands, (i + 1) / bands
        col = lerp_stops(V3_GRADIENT_STOPS, (t0 + t1) / 2)
        q0 = p_min + (p_max - p_min) * t0
        q1 = p_min + (p_max - p_min) * t1
        # Band = slab between the two perpendicular lines q0, q1 along the
        # axis; padded far past the canvas along the perpendicular direction.
        vx, vy = uy, -ux                        # perpendicular to the axis
        L = CANVAS * 2
        band = [(cx + q0 * ux - L * vx, cy + q0 * uy - L * vy),
                (cx + q0 * ux + L * vx, cy + q0 * uy + L * vy),
                (cx + q1 * ux + L * vx, cy + q1 * uy + L * vy),
                (cx + q1 * ux - L * vx, cy + q1 * uy - L * vy)]
        d.polygon(band, fill=col + (255,))
    mask = Image.new("L", (CANVAS, CANVAS), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    img.putalpha(mask)

    # One hairline inner outline, punched out as negative space (identical
    # to v2: pure transparency, no stroke color of its own).
    inner = Image.new("L", (CANVAS, CANVAS), 0)
    ImageDraw.Draw(inner).polygon(
        hex_points(cx, cy, r * V2_INNER_SCALE, rotation=30),
        outline=255, width=V2_INNER_WIDTH)
    img.putalpha(Image.composite(
        Image.new("L", (CANVAS, CANVAS), 0), mask, inner))
    return img

# --- v1: honeycomb cluster (original code path, unchanged) -------------------
def render_cluster():
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    cx = cy = CANVAS // 2

    # Soft radial glow behind the core cell (concentric alpha circles).
    glow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    steps = 48
    for i in range(steps, 0, -1):
        r = GLOW_RADIUS * i / steps
        alpha = int(GLOW_ALPHA * (1 - i / steps) ** 2)
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=COLOR_GLOW + (alpha,))
    img.alpha_composite(glow)

    d = ImageDraw.Draw(img)

    # Six ring cells: flat-top neighbors sit at distance sqrt(3)*R,
    # directions 30/90/150/210/270/330 degrees.
    ring_dist = math.sqrt(3) * CELL_RADIUS
    cells = [(cx + ring_dist * math.cos(math.radians(30 + 60 * k)),
              cy + ring_dist * math.sin(math.radians(30 + 60 * k)))
             for k in range(6)]

    # Vertical gradient across the ring: lighter at top, darker at bottom.
    ys = [c[1] for c in cells]
    for x, y in cells:
        t = (y - min(ys)) / (max(ys) - min(ys))
        d.polygon(hex_points(x, y, CELL_RADIUS),
                  fill=lerp(COLOR_RING_LIGHT, COLOR_RING_DARK, t),
                  outline=COLOR_STROKE, width=STROKE_WIDTH)

    # Bright core cell on top.
    d.polygon(hex_points(cx, cy, CELL_RADIUS),
              fill=COLOR_CORE, outline=COLOR_STROKE, width=STROKE_WIDTH)
    return img

# --- Main --------------------------------------------------------------------
def main():
    render = {"gradient": render_gradient,
              "minimal": render_minimal,
              "cluster": render_cluster}[CONCEPT]
    output = {"gradient": OUTPUT,
              "minimal": OUTPUT_MINIMAL,
              "cluster": OUTPUT_CLUSTER}[CONCEPT]
    img = render().resize((FINAL_SIZE, FINAL_SIZE), Image.LANCZOS)
    img.save(output)
    print(f"concept={CONCEPT} saved {output} size={img.size} mode={img.mode}")

if __name__ == "__main__":
    main()
