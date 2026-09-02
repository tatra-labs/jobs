"""
build_geo.py - pre-projected US state map geometry for a zero-dependency frontend.

The site ships no JS libraries (no d3, no topojson-client), so ALL of the map work
happens here, in Python, and the browser only gets plain SVG path strings.

What this does
--------------
1. Downloads us-atlas@3 `states-10m.json` (TopoJSON, WGS84 lon/lat) from jsDelivr.
   us-atlas is ISC-licensed; the underlying geometry is US Census Bureau
   cartographic boundary files (public domain, 1:10,000,000 scale).
   It also downloads `states-albers-10m.json` - the same atlas ALREADY projected by
   d3.geoAlbersUsa into a 975x610 frame - purely as GROUND TRUTH to validate the
   projection re-implemented below.  Nothing from it ends up in the output.

2. Decodes the TopoJSON by hand (delta-decoded quantized arcs -> lon/lat).

3. Re-implements d3.geoAlbersUsa in pure Python at scale 1300 / translate
   [487.5, 305], which is the standard 975x610 "US Albers" frame:
     lower 48 : conicEqualArea, parallels 29.5/45.5, rotate [96,0], center [-0.6,38.7]
     Alaska   : conicEqualArea, parallels 55/65,     rotate [154,0], center [-2,58.5]
                scale x0.35, translate [x-0.307k, y+0.201k], clip to d3's rect
     Hawaii   : conicEqualArea, parallels 8/18,      rotate [157,0], center [-3,19.9]
                scale x1.0,  translate [x-0.205k, y+0.212k], clip to d3's rect
   Puerto Rico is NOT part of d3.geoAlbersUsa (that projection returns null for it),
   so this script adds an explicit, documented PR inset - see PR_* constants.

4. Simplifies at the ARC level (not the ring level) with Visvalingam-Whyatt, so that
   borders shared by two states stay bit-identical and no slivers open up between
   neighbours.  Arc endpoints are never removed.  No arc is ever dropped.

5. Emits data/geo_states.json.

Run:  uv run python pipeline/build_geo.py
"""

import heapq
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import DATA, FIPS_TO_ABBR, RAW, ROOT, STATES, fetch, write_json  # noqa: E402

RAW_GEO = RAW / "geo"

ATLAS_URL = "https://cdn.jsdelivr.net/npm/us-atlas@3/states-10m.json"
ATLAS_TRUTH_URL = "https://cdn.jsdelivr.net/npm/us-atlas@3/states-albers-10m.json"

WIDTH, HEIGHT = 975.0, 610.0
K = 1300.0          # d3 scale for a 975x610 albersUsa frame
TX, TY = 487.5, 305.0

# Visvalingam area threshold in square pixels.  0.06 px^2 keeps every visible
# wiggle at 975px wide while cutting the path payload roughly in half.
SIMPLIFY_AREA = 0.06
# A ring is never dropped; if simplification would take it below this many
# distinct points we keep its N most significant points instead.
MIN_RING_POINTS = 3
COORD_DP = 1        # decimal places kept in the emitted path strings

RADIANS = math.pi / 180.0

# --- Puerto Rico inset -------------------------------------------------------
# d3.geoAlbersUsa has no PR.  PR matters here because OEWS publishes PR as a
# state-equivalent, so dropping it would silently lose a whole geography.
# It gets its own conic-equal-area centred on the island, at the SAME scale as
# the lower 48 (so its area stays comparable), parked in the empty ocean to the
# south-east of Florida.  The placement is asserted to be inside the viewBox and
# non-overlapping with every other state's bounding box before we emit.
PR_PARALLELS = (17.9, 18.4)
PR_ROTATE = 66.0
PR_CENTER = (0.0, 18.2)
PR_SCALE = K
# Candidate anchor points for the PR inset's bbox centre, tried in order.
PR_ANCHORS = [(905.0, 555.0), (905.0, 520.0), (880.0, 580.0), (930.0, 560.0)]


# ---------------------------------------------------------------------------
# TopoJSON decoding
# ---------------------------------------------------------------------------
def decode_arcs(topo):
    """Delta-decode quantized topojson arcs into absolute coordinate lists."""
    tr = topo.get("transform")
    out = []
    for arc in topo["arcs"]:
        pts = []
        if tr:
            sx, sy = tr["scale"]
            tx, ty = tr["translate"]
            x = y = 0
            for dx, dy in arc:
                x += dx
                y += dy
                pts.append((x * sx + tx, y * sy + ty))
        else:
            pts = [(p[0], p[1]) for p in arc]
        out.append(pts)
    return out


def geom_polygons(geom):
    """Yield each polygon (list of arc-index rings) of a Polygon/MultiPolygon."""
    if geom["type"] == "Polygon":
        yield geom["arcs"]
    elif geom["type"] == "MultiPolygon":
        for poly in geom["arcs"]:
            yield poly
    elif geom["type"] is None:
        return
    else:
        raise ValueError("unexpected geometry type " + str(geom["type"]))


def ring_arc_indices(ring):
    for idx in ring:
        yield (~idx if idx < 0 else idx)


def assemble_ring(ring, arcs):
    """Stitch arc indices into one closed ring of points."""
    pts = []
    for idx in ring:
        a = arcs[~idx][::-1] if idx < 0 else arcs[idx]
        if pts:
            pts.extend(a[1:])
        else:
            pts.extend(a)
    return pts


# ---------------------------------------------------------------------------
# Projection: d3.geoConicEqualArea / d3.geoAlbersUsa, re-implemented
# ---------------------------------------------------------------------------
class ConicEqualArea:
    """d3.geoConicEqualArea(parallels, rotate=[r,0], center, scale, translate).

    d3 computes  X = tx + k*(px - cpx),  Y = ty - k*(py - cpy)
    where (px,py) = raw(rotate(lon,lat)) and (cpx,cpy) = raw(center) - note the
    centre is passed through the RAW projection, not through the rotation, which
    is exactly why geoAlbers pairs rotate([96,0]) with center([-0.6, 38.7]).
    """

    def __init__(self, parallels, rotate_lambda, center, scale, translate):
        phi0, phi1 = (p * RADIANS for p in parallels)
        sy0 = math.sin(phi0)
        n = (sy0 + math.sin(phi1)) / 2.0
        if abs(n) < 1e-10:
            raise ValueError("cylindrical degenerate case not needed here")
        self.n = n
        self.C = 1.0 + sy0 * (2.0 * n - sy0)
        self.r0 = math.sqrt(self.C) / n
        self.rot = rotate_lambda
        self.k = scale
        self.tx, self.ty = translate
        self.cpx, self.cpy = self._raw(center[0] * RADIANS, center[1] * RADIANS)

    def _raw(self, lam, phi):
        v = self.C - 2.0 * self.n * math.sin(phi)
        if v < 0:
            v = 0.0
        r = math.sqrt(v) / self.n
        return (r * math.sin(lam * self.n), self.r0 - r * math.cos(lam * self.n))

    def __call__(self, lon, lat):
        lam = lon + self.rot
        # normalize into [-180, 180) so antimeridian-crossing Aleutian rings stay
        # continuous once rotated
        lam = (lam + 180.0) % 360.0 - 180.0
        px, py = self._raw(lam * RADIANS, lat * RADIANS)
        return (self.tx + self.k * (px - self.cpx), self.ty - self.k * (py - self.cpy))


LOWER48 = ConicEqualArea((29.5, 45.5), 96.0, (-0.6, 38.7), K, (TX, TY))
ALASKA = ConicEqualArea((55.0, 65.0), 154.0, (-2.0, 58.5), K * 0.35,
                        (TX - 0.307 * K, TY + 0.201 * K))
HAWAII = ConicEqualArea((8.0, 18.0), 157.0, (-3.0, 19.9), K,
                        (TX - 0.205 * K, TY + 0.212 * K))
PUERTO_RICO = ConicEqualArea(PR_PARALLELS, PR_ROTATE, PR_CENTER, PR_SCALE, (0.0, 0.0))

EPS = 1e-6
# d3.geoAlbersUsa's own clipExtent rectangles, in output pixels.
D3_CLIP = {
    "l48": (TX - 0.455 * K, TY - 0.238 * K, TX + 0.455 * K, TY + 0.238 * K),
    "ak": (TX - 0.425 * K + EPS, TY + 0.120 * K + EPS,
           TX - 0.214 * K - EPS, TY + 0.234 * K - EPS),
    "hi": (TX - 0.214 * K + EPS, TY + 0.166 * K + EPS,
           TX - 0.115 * K - EPS, TY + 0.234 * K - EPS),
}
# d3's Alaska rect starts at x = -65, so the westernmost Aleutians land OUTSIDE a
# "0 0 975 610" viewBox - us-atlas' own states-albers-10m.json has bbox minX
# -57.66 for exactly this reason, and every renderer simply crops them away.
# We intersect d3's rects with the viewBox so every emitted path is provably
# inside the frame.  This removes only geometry that no viewer could ever see.
CLIP = {k: (max(r[0], 0.0), max(r[1], 0.0), min(r[2], WIDTH), min(r[3], HEIGHT))
        for k, r in D3_CLIP.items()}
PROJ = {"l48": LOWER48, "ak": ALASKA, "hi": HAWAII, "pr": PUERTO_RICO}
GROUP_OF_FIPS = {"02": "ak", "15": "hi", "72": "pr"}


# ---------------------------------------------------------------------------
# Sutherland-Hodgman rectangle clip (matches d3's clipRectangle closely enough
# for rendering; it is what keeps the far-western Aleutians out of the frame)
# ---------------------------------------------------------------------------
def clip_ring(pts, rect):
    x0, y0, x1, y1 = rect

    def clip_edge(poly, inside, intersect):
        if not poly:
            return []
        out = []
        prev = poly[-1]
        prev_in = inside(prev)
        for cur in poly:
            cur_in = inside(cur)
            if cur_in:
                if not prev_in:
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif prev_in:
                out.append(intersect(prev, cur))
            prev, prev_in = cur, cur_in
        return out

    def ix_x(a, b, xc):
        t = (xc - a[0]) / (b[0] - a[0])
        return (xc, a[1] + t * (b[1] - a[1]))

    def ix_y(a, b, yc):
        t = (yc - a[1]) / (b[1] - a[1])
        return (a[0] + t * (b[0] - a[0]), yc)

    poly = pts[:-1] if len(pts) > 1 and pts[0] == pts[-1] else pts[:]
    poly = clip_edge(poly, lambda p: p[0] >= x0, lambda a, b: ix_x(a, b, x0))
    poly = clip_edge(poly, lambda p: p[0] <= x1, lambda a, b: ix_x(a, b, x1))
    poly = clip_edge(poly, lambda p: p[1] >= y0, lambda a, b: ix_y(a, b, y0))
    poly = clip_edge(poly, lambda p: p[1] <= y1, lambda a, b: ix_y(a, b, y1))
    if len(poly) < 3:
        return []
    return poly + [poly[0]]


# ---------------------------------------------------------------------------
# Visvalingam-Whyatt simplification, endpoints pinned
# ---------------------------------------------------------------------------
def tri_area(a, b, c):
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0


def simplify(pts, min_area, min_points):
    """Drop interior points whose effective triangle area < min_area.

    Never drops the first or last point (arc topology), and never lets the arc
    fall below min_points.  Returns (points, n_dropped).
    """
    n = len(pts)
    if n <= max(2, min_points):
        return pts, 0
    prev = list(range(-1, n - 1))
    nxt = list(range(1, n + 1))
    alive = [True] * n
    heap = []
    for i in range(1, n - 1):
        a = tri_area(pts[i - 1], pts[i], pts[i + 1])
        heapq.heappush(heap, (a, i))
    area_at = {}
    live = n
    dropped = 0
    while heap:
        a, i = heapq.heappop(heap)
        if not alive[i] or i in area_at and area_at[i] != a:
            continue
        if a >= min_area:
            break
        if live <= min_points:
            break
        p, q = prev[i], nxt[i]
        if p < 0 or q >= n:
            continue
        alive[i] = False
        live -= 1
        dropped += 1
        nxt[p] = q
        prev[q] = p
        for j in (p, q):
            if 0 < j < n - 1 and alive[j]:
                na = tri_area(pts[prev[j]], pts[j], pts[nxt[j]])
                area_at[j] = na
                heapq.heappush(heap, (na, j))
    return [pts[i] for i in range(n) if alive[i]], dropped


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def signed_area(ring):
    s = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        s += x0 * y1 - x1 * y0
    return s / 2.0


def ring_centroid(ring):
    """Area-weighted centroid of a closed ring; returns (cx, cy, signed_area)."""
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a /= 2.0
    if abs(a) < 1e-12:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
    return (cx / (6.0 * a), cy / (6.0 * a), a)


def point_in_ring(pt, ring):
    x, y = pt
    inside = False
    for i in range(len(ring) - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        if (y0 > y) != (y1 > y):
            xi = x0 + (y - y0) / (y1 - y0) * (x1 - x0)
            if x < xi:
                inside = not inside
    return inside


def seg_dist2(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    if d2 > 0:
        t = ((px - ax) * dx + (py - ay) * dy) / d2
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        ax, ay = ax + t * dx, ay + t * dy
    return (px - ax) ** 2 + (py - ay) ** 2


def signed_dist_to_polygon(pt, rings):
    """+inside / -outside distance from pt to the polygon (rings[0] = shell)."""
    px, py = pt
    best = float("inf")
    for ring in rings:
        for i in range(len(ring) - 1):
            best = min(best, seg_dist2(px, py, ring[i][0], ring[i][1],
                                       ring[i + 1][0], ring[i + 1][1]))
    d = math.sqrt(best)
    inside = point_in_ring(pt, rings[0]) and not any(point_in_ring(pt, r) for r in rings[1:])
    return d if inside else -d


def polylabel(rings, precision=0.4):
    """Pole of inaccessibility (Mapbox polylabel) - the point furthest from any
    edge, i.e. where a text label actually fits.  Used for label_pos."""
    xs = [p[0] for p in rings[0]]
    ys = [p[1] for p in rings[0]]
    minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
    w, h = maxx - minx, maxy - miny
    cell = min(w, h)
    if cell == 0:
        return (minx, miny)
    half = cell / 2.0

    def cell_tuple(cx, cy, hh):
        d = signed_dist_to_polygon((cx, cy), rings)
        return (-(d + hh * math.sqrt(2)), cx, cy, hh, d)

    heap = []
    y = miny
    while y < maxy:
        x = minx
        while x < maxx:
            heapq.heappush(heap, cell_tuple(x + half, y + half, half))
            x += cell
        y += cell
    cx0, cy0, _a = ring_centroid(rings[0])
    best = cell_tuple(cx0, cy0, 0.0)
    c = cell_tuple(minx + w / 2, miny + h / 2, 0.0)
    if c[4] > best[4]:
        best = c
    it = 0
    while heap and it < 200000:
        it += 1
        top = heapq.heappop(heap)
        if top[4] > best[4]:
            best = top
        if -top[0] - best[4] <= precision:
            continue
        hh = top[3] / 2.0
        for sx in (-1, 1):
            for sy in (-1, 1):
                heapq.heappush(heap, cell_tuple(top[1] + sx * hh, top[2] + sy * hh, hh))
    return (best[1], best[2])


def spherical_area(ring):
    """Signed area of a lon/lat ring on the unit sphere, in steradians.

    Standard formula; only the RATIO between states matters here, and that is
    what proves the projection is genuinely equal-area.
    """
    total = 0.0
    for i in range(len(ring) - 1):
        lon0, lat0 = ring[i]
        lon1, lat1 = ring[i + 1]
        dlon = (lon1 - lon0) * RADIANS
        total += dlon * (2.0 + math.sin(lat0 * RADIANS) + math.sin(lat1 * RADIANS))
    return total / 2.0


def bbox_of(rings):
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_overlap(a, b, pad=0.0):
    return not (a[2] + pad < b[0] or b[2] + pad < a[0]
                or a[3] + pad < b[1] or b[3] + pad < a[1])


def to_path(polys, dp=COORD_DP):
    """Absolute moveto + implicit-lineto subpaths: 'M x,y x,y ... Z M ... Z'."""
    parts = []
    for rings in polys:
        for ring in rings:
            r = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
            if len(r) < 3:
                continue
            coords = []
            last = None
            for x, y in r:
                s = f"{round(x, dp):g},{round(y, dp):g}"
                if s != last:
                    coords.append(s)
                    last = s
            if len(coords) < 3:
                continue
            parts.append("M" + " ".join(coords) + "Z")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    RAW_GEO.mkdir(parents=True, exist_ok=True)
    src = fetch(ATLAS_URL, RAW_GEO / "states-10m.json")
    truth_path = fetch(ATLAS_TRUTH_URL, RAW_GEO / "states-albers-10m.json")
    topo = json.loads(src.read_text(encoding="utf-8"))
    arcs_ll = decode_arcs(topo)
    geoms = topo["objects"]["states"]["geometries"]
    print(f"source           : {ATLAS_URL}")
    print(f"                   us-atlas@3 (ISC) over US Census cartographic boundaries (public domain)")
    print(f"topology         : {len(arcs_ll)} arcs, {len(geoms)} geometries")

    wanted = set(STATES.keys())              # 50 + DC + PR
    by_fips = {g["id"]: g for g in geoms}
    have = {f for f in by_fips if f in FIPS_TO_ABBR}
    skipped = sorted((by_fips[f]["id"], by_fips[f]["properties"]["name"])
                     for f in by_fips if f not in FIPS_TO_ABBR)
    missing = sorted(wanted - {FIPS_TO_ABBR[f] for f in have})
    print(f"geographies kept : {len(have)}  (50 states + DC + PR)")
    print(f"geographies EXCLUDED (not OEWS state-equivalents in this project): "
          + ", ".join(f"{i}={n}" for i, n in skipped))
    if missing:
        print(f"!! MISSING from atlas: {missing}")

    # --- assign every arc to a projection group, via the states that use it ---
    arc_group = {}
    conflicts = []
    for fips, g in by_fips.items():
        if fips not in FIPS_TO_ABBR:
            continue
        grp = GROUP_OF_FIPS.get(fips, "l48")
        for poly in geom_polygons(g):
            for ring in poly:
                for ai in ring_arc_indices(ring):
                    if arc_group.setdefault(ai, grp) != grp:
                        conflicts.append((ai, arc_group[ai], grp))
    assert not conflicts, f"arcs shared across projection groups: {conflicts[:5]}"

    # --- project + simplify each arc once (topology preserved) ---
    arcs_px = {}
    pts_in = pts_out = 0
    for ai, grp in arc_group.items():
        proj = PROJ[grp]
        pxs = [proj(lon, lat) for lon, lat in arcs_ll[ai]]
        pts_in += len(pxs)
        simp, _ = simplify(pxs, SIMPLIFY_AREA, MIN_RING_POINTS)
        arcs_px[ai] = simp
        pts_out += len(simp)
    print(f"arcs projected   : {len(arcs_px)}  points {pts_in:,} -> {pts_out:,} "
          f"after Visvalingam (area < {SIMPLIFY_AREA} px^2)")

    def assemble(fips, arcs_source, clips=CLIP):
        """Build the projected polygons of one state from a given arc table."""
        grp = GROUP_OF_FIPS.get(fips, "l48")
        rect = clips.get(grp)
        polys, dropped_rings = [], 0
        for poly in geom_polygons(by_fips[fips]):
            rings = []
            for ring in poly:
                pts = assemble_ring(ring, arcs_source)
                if pts[0] != pts[-1]:
                    pts = pts + [pts[0]]
                if rect is not None:
                    bb = bbox_of([pts])
                    if (bb[0] < rect[0] or bb[1] < rect[1]
                            or bb[2] > rect[2] or bb[3] > rect[3]):
                        pts = clip_ring(pts, rect)
                        if not pts:
                            dropped_rings += 1
                            continue
                if len(pts) < 4:
                    dropped_rings += 1
                    continue
                rings.append(pts)
            if not rings:
                continue
            # make holes wind opposite the shell so nonzero AND evenodd fill agree
            shell_sign = 1.0 if signed_area(rings[0]) >= 0 else -1.0
            for i in range(1, len(rings)):
                if (1.0 if signed_area(rings[i]) >= 0 else -1.0) == shell_sign:
                    rings[i] = rings[i][::-1]
            polys.append(rings)
        return polys, dropped_rings

    # ---- Puerto Rico inset placement -------------------------------------
    fips_order = sorted(f for f in by_fips if f in FIPS_TO_ABBR)
    prelim = {}
    for fips in fips_order:
        if fips == "72":
            continue
        polys, _ = assemble(fips, arcs_px)
        prelim[fips] = bbox_of([r for p in polys for r in p])
    if "72" in by_fips:
        pr_raw, _ = assemble("72", arcs_px)
        pr_bb = bbox_of([r for p in pr_raw for r in p])
        pr_w, pr_h = pr_bb[2] - pr_bb[0], pr_bb[3] - pr_bb[1]
        chosen = None
        for ax, ay in PR_ANCHORS:
            dx = ax - (pr_bb[0] + pr_bb[2]) / 2.0
            dy = ay - (pr_bb[1] + pr_bb[3]) / 2.0
            nb = (pr_bb[0] + dx, pr_bb[1] + dy, pr_bb[2] + dx, pr_bb[3] + dy)
            if nb[0] < 2 or nb[1] < 2 or nb[2] > WIDTH - 2 or nb[3] > HEIGHT - 2:
                continue
            if any(bbox_overlap(nb, b, pad=6.0) for b in prelim.values()):
                continue
            chosen = (dx, dy, ax, ay)
            break
        if chosen is None:
            raise SystemExit("could not place the Puerto Rico inset without overlap; "
                             "widen PR_ANCHORS or drop PR")
        dx, dy, ax, ay = chosen
        PUERTO_RICO.tx += dx
        PUERTO_RICO.ty += dy
        for ai, grp in arc_group.items():
            if grp == "pr":
                arcs_px[ai] = [(x + dx, y + dy) for x, y in arcs_px[ai]]
        print(f"Puerto Rico inset: placed at ({ax:.0f},{ay:.0f}), "
              f"{pr_w:.1f}x{pr_h:.1f}px, same scale as the lower 48")

    # ---- build the output ------------------------------------------------
    states_out = []
    total_dropped = 0
    for fips in fips_order:
        polys, dropped = assemble(fips, arcs_px)
        total_dropped += dropped
        abbr = FIPS_TO_ABBR[fips]
        name = STATES[abbr][1]
        rings_all = [r for p in polys for r in p]
        bb = bbox_of(rings_all)
        # area-weighted centroid over shells minus holes
        num_x = num_y = den = 0.0
        area_px = 0.0
        for rings in polys:
            for i, ring in enumerate(rings):
                cx, cy, a = ring_centroid(ring)
                sgn = 1.0 if i == 0 else -1.0
                w = abs(a) * sgn
                num_x += cx * w
                num_y += cy * w
                den += w
                area_px += w
        if abs(den) < 1e-9:
            cx = (bb[0] + bb[2]) / 2.0
            cy = (bb[1] + bb[3]) / 2.0
        else:
            cx, cy = num_x / den, num_y / den
        # label position: pole of inaccessibility of the LARGEST polygon
        biggest = max(polys, key=lambda rings: abs(signed_area(rings[0])))
        lx, ly = polylabel(biggest)
        states_out.append({
            "fips": fips,
            "abbr": abbr,
            "name": name,
            "path": to_path(polys),
            "centroid": [round(cx, 1), round(cy, 1)],
            "label_pos": [round(lx, 1), round(ly, 1)],
            "bbox": [round(v, 1) for v in bb],
            "area_px": round(abs(area_px), 1),
            "inset": GROUP_OF_FIPS.get(fips, None),
        })
    d3_dropped = sum(assemble(f, arcs_px, D3_CLIP)[1] for f in fips_order)
    print(f"rings removed by d3.geoAlbersUsa's own clipExtent rects: {d3_dropped} "
          f"(far-western Aleutians + NW Hawaiian islands)")
    print(f"rings additionally removed by cropping to the 975x610 viewBox: "
          f"{total_dropped - d3_dropped} (Aleutian islets d3 also places at x<0, "
          f"i.e. off-frame and unrenderable)")

    # ---- validation ------------------------------------------------------
    print("\n--- validation ---")
    bad = []
    for s in states_out:
        x0, y0, x1, y1 = s["bbox"]
        if x0 < -0.5 or y0 < -0.5 or x1 > WIDTH + 0.5 or y1 > HEIGHT + 0.5:
            bad.append((s["abbr"], s["bbox"]))
    print(f"shapes emitted        : {len(states_out)} (expected 51 + PR = 52)")
    print(f"outside viewBox       : {len(bad)} {bad if bad else ''}")
    assert not bad, f"states outside the {WIDTH:.0f}x{HEIGHT:.0f} viewBox: {bad}"
    assert len(states_out) == 52, f"expected 52 shapes, got {len(states_out)}"

    # Ground truth: compare our unsimplified projection against us-atlas'
    # own d3-projected states-albers-10m.json, state by state.
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    t_arcs = decode_arcs(truth)
    t_by = {g["id"]: g for g in truth["objects"]["states"]["geometries"]}
    unsimp = {ai: [PROJ[g](lon, lat) for lon, lat in arcs_ll[ai]]
              for ai, g in arc_group.items()}
    deltas = []
    for fips in fips_order:
        if fips not in t_by:
            continue
        mine, _ = assemble(fips, unsimp, D3_CLIP)
        mb = bbox_of([r for p in mine for r in p])
        tp = []
        for poly in geom_polygons(t_by[fips]):
            for ring in poly:
                tp.append(assemble_ring(ring, t_arcs))
        tb = bbox_of(tp)
        deltas.append((max(abs(a - b) for a, b in zip(mb, tb)), FIPS_TO_ABBR[fips]))
    deltas.sort(reverse=True)
    print(f"vs states-albers-10m  : max bbox delta {deltas[0][0]:.3f}px ({deltas[0][1]}), "
          f"median {sorted(d for d, _ in deltas)[len(deltas) // 2]:.3f}px "
          f"over {len(deltas)} states")
    assert deltas[0][0] < 0.5, f"projection disagrees with d3 ground truth: {deltas[:5]}"

    for s in states_out:
        x0, y0, x1, y1 = s["bbox"]
        lx, ly = s["label_pos"]
        assert x0 - 0.6 <= lx <= x1 + 0.6 and y0 - 0.6 <= ly <= y1 + 0.6, \
            f"label_pos outside bbox for {s['abbr']}"

    # Equal-area check.  d3's conicEqualArea preserves area on the unit sphere,
    # so painted_px / (that inset's own scale^2 * steradians) must be 1.0 for
    # EVERY group.  This catches a wrong parallel, a wrong rotation or a wrong
    # inset scale in a way that a bounding box never would.  The insets come out
    # a hair under 1.0 because the clip rectangles trim outlying islands.
    assert abs(ALASKA.k - K * 0.35) < 1e-9, "Alaska inset must be 0.35x lower 48"
    assert HAWAII.k == K and PUERTO_RICO.k == K
    print("\n  equal-area check  (painted px^2 / (scale^2 * steradians)):")
    ratios = defaultdict(list)
    for fips in fips_order:
        grp = GROUP_OF_FIPS.get(fips, "l48")
        sr = 0.0
        for poly in geom_polygons(by_fips[fips]):
            for j, ring in enumerate(poly):
                pts = assemble_ring(ring, arcs_ll)
                if pts[0] != pts[-1]:
                    pts = pts + [pts[0]]
                a = abs(spherical_area(pts))
                sr += a if j == 0 else -a
        px = next(s["area_px"] for s in states_out if s["fips"] == fips)
        k = PROJ[grp].k
        ratios[grp].append((px / (k * k * sr) if sr > 0 else float("nan"),
                            FIPS_TO_ABBR[fips]))
    for grp, expect in (("l48", 1.0), ("ak", 1.0), ("hi", 1.0), ("pr", 1.0)):
        vals = sorted(ratios.get(grp, []))
        if not vals:
            continue
        print(f"    {grp:<4} expect {expect:.4f}  min {vals[0][0]:.4f} ({vals[0][1]})"
              f"  max {vals[-1][0]:.4f} ({vals[-1][1]})  n={len(vals)}")
        tol = 0.02 if grp == "l48" else 0.06   # insets lose clipped islands
        offenders = [v for v in vals if abs(v[0] - expect) > tol]
        assert not offenders, f"{grp} equal-area ratios off by >{tol}: {offenders[:5]}"

    ranked = sorted(states_out, key=lambda s: (s["bbox"][2] - s["bbox"][0]) *
                    (s["bbox"][3] - s["bbox"][1]), reverse=True)
    print("\n  5 largest bounding boxes            5 smallest bounding boxes")
    print("  abbr    w x h        area_px         abbr    w x h        area_px")
    for big, small in zip(ranked[:5], ranked[-5:][::-1]):
        def fmt(s):
            w = s["bbox"][2] - s["bbox"][0]
            h = s["bbox"][3] - s["bbox"][1]
            return f"{s['abbr']:>4}  {w:6.1f} x {h:5.1f}  {s['area_px']:9,.0f}"
        print("  " + fmt(big) + "     " + fmt(small))

    out = {
        "viewBox": f"0 0 {WIDTH:.0f} {HEIGHT:.0f}",
        "width": WIDTH,
        "height": HEIGHT,
        "source": {
            "geometry": ATLAS_URL,
            "atlas": "us-atlas@3 (ISC licence); geometry from US Census Bureau "
                     "cartographic boundary files 1:10,000,000 (public domain)",
            "validation": ATLAS_TRUTH_URL,
        },
        "projection": {
            "name": "d3.geoAlbersUsa (re-implemented in pipeline/build_geo.py)",
            "scale": K,
            "translate": [TX, TY],
            "insets": {
                "ak": "conicEqualArea parallels 55/65 rotate 154 center [-2,58.5], scale x0.35",
                "hi": "conicEqualArea parallels 8/18 rotate 157 center [-3,19.9]",
                "pr": "NOT part of d3.geoAlbersUsa; custom inset, conicEqualArea "
                      f"parallels {PR_PARALLELS[0]}/{PR_PARALLELS[1]} rotate {PR_ROTATE:.0f} "
                      f"center [0,{PR_CENTER[1]}], same scale as the lower 48",
            },
            "simplify_area_px2": SIMPLIFY_AREA,
        },
        "states": states_out,
    }
    path = write_json(out, DATA / "geo_states.json")

    kb = path.stat().st_size / 1024
    print(f"\nrows in {len(geoms)} geometries -> rows out {len(states_out)} states")
    print(f"path payload: {sum(len(s['path']) for s in states_out) / 1024:,.0f} KB of "
          f"{kb:,.0f} KB total")
    if kb > 500:
        print(f"!! WARNING: {kb:,.0f} KB exceeds the ~500 KB target; raise SIMPLIFY_AREA")


if __name__ == "__main__":
    main()
