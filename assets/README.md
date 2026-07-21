# Goldcomb Assets

## logo.png — the Goldcomb logo (v3)

### Concept

v3 keeps the v2 silhouette exactly — ONE mark: a single solid pointy-top
hexagon pierced by a single hairline hexagonal outline rendered as pure
negative space — and turns the tonal dial up from a whisper to a real event.
The fill is now a three-stop gold gradient raked diagonally across the mark
(45° axis), so light reads as raking across a polished metal face: a bright
champagne highlight at the top-left, warming through classic champagne gold
at the center, falling to a deep antique gold shadow at the bottom-right.
Rich, not gaudy — every stop stays in the antique/champagne gold family,
with no yellow-gold saturation spikes. The silhouette is still
mathematically crisp (rendered at 4x supersampling), the mark occupies the
same ~51% x ~58% of the canvas so it breathes, and the pointy-top
orientation keeps its calm, jewel-like stance. It stays legible at 64px,
where the gradient mostly merges and the mark reads as a solid gold hexagon.

### Color palette (gradient stops, top-left -> bottom-right)

| Use                                  | Hex       | RGB             |
|--------------------------------------|-----------|-----------------|
| Highlight (light champagne)          | `#E8C766` | (232, 199, 102) |
| Mid (champagne gold)                 | `#D4AF37` | (212, 175, 55)  |
| Shadow (deep antique gold)           | `#A67C1B` | (166, 124, 27)  |
| Inner hairline                       | transparent (negative space) |

Gradient axis: 45° diagonal (top-left highlight to bottom-right shadow),
interpolated linearly through the three stops in 512 bands.

### Reproduce

```sh
.venv/bin/python assets/logo.py
```

Regenerates `assets/logo.png` (1024x1024, RGBA, transparent background).
The generator (`assets/logo.py`) renders at 4x supersampling (4096x4096)
and LANCZOS-downsamples for smooth edges; geometry and colors are
parameterized at the top of the script (`V3_GRADIENT_STOPS`,
`V3_GRADIENT_ANGLE`, hex radius, hairline scale/width) for easy iteration.

## logo-v2-minimal.png — v2, preserved for comparison

The v2 logo: the same single pointy-top hexagon + hairline negative-space
outline, but with only a whisper of vertical two-gold tonal variation
(antique gold `#C9A227` at the top to champagne `#D4AF37` at the bottom).
Superseded by v3's richer raked gradient; preserved here for comparison.

To regenerate it, set `CONCEPT = "minimal"` at the top of
`assets/logo.py` — the minimal path writes to `assets/logo-v2-minimal.png`
and is byte-identical to the preserved file.

## logo-v1-cluster.png — v1, preserved for comparison

The original v1 logo: a honeycomb cluster of seven flat-top hexagonal cells
(one bright "AI core" center with a soft radial glow, six ring cells with a
light-to-dark gold vertical gradient, thin darker-gold strokes between
cells). Palette: `#FFD166` core, `#F0B429`/`#B8860B` ring, `#8A5E0B`
strokes, `#FFE9A8` glow.

To regenerate it, set `CONCEPT = "cluster"` at the top of
`assets/logo.py` — the cluster path writes to `assets/logo-v1-cluster.png`
and is byte-identical to the preserved file.
