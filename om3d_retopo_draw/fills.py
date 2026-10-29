# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure helpers for Grid Fill, Bridge and Pen Loops. No viewport access."""
import math

from mathutils import Vector


# ---------------------------------------------------------------- holes
def border_cycle(edge):
    """Every border edge around the hole (or outline) through `edge`.

    Returns the ordered verts of the closed border, or None if the border
    is not a simple closed cycle (open wire, bow-tie vert, etc.).
    """
    if len(edge.link_faces) != 1:
        return None
    start = edge.verts[0]
    verts = [start]
    v, prev = edge.verts[1], edge
    while v is not start:
        verts.append(v)
        nxt = [e for e in v.link_edges if e is not prev and len(e.link_faces) == 1]
        if len(nxt) != 1 or len(verts) > 100000:
            return None
        prev = nxt[0]
        v = prev.other_vert(v)
    return verts


def best_grid_offset(cos, span):
    """Rotate the grid corners onto the sharpest turns of the border."""
    n = len(cos)
    t = n // 2 - span

    def sharp(i):
        a = cos[(i - 1) % n] - cos[i]
        b = cos[(i + 1) % n] - cos[i]
        if a.length < 1e-12 or b.length < 1e-12:
            return 0.0
        return math.pi - a.angle(b)

    sh = [sharp(i) for i in range(n)]
    return max(range(n), key=lambda o: sh[o] + sh[(o + span) % n]
               + sh[(o + span + t) % n] + sh[(o + 2 * span + t) % n])


def best_grid_layout(cos):
    """Pick span and corner offset together so the grid corners land on the
    sharpest turns of the hole, with a small preference for square-ish grids."""
    n = len(cos)
    half = n // 2

    def sharp(i):
        a = cos[(i - 1) % n] - cos[i]
        b = cos[(i + 1) % n] - cos[i]
        if a.length < 1e-12 or b.length < 1e-12:
            return 0.0
        return math.pi - a.angle(b)

    sh = [sharp(i) for i in range(n)]
    best = None
    for s in range(1, half):
        t = half - s
        for o in range(n):
            score = (sh[o] + sh[(o + s) % n] + sh[(o + s + t) % n] + sh[(o + 2 * s + t) % n]
                     - 0.15 * abs(s - t))
            if best is None or score > best[0]:
                best = (score, s, o)
    return best[1], best[2]


def coons_grid(cos, span, offset):
    """Grid over a closed border of len(cos) (even) points.

    Returns grid[u][v] for u in 0..span, v in 0..t where each entry is an
    int (index into the border) or a Vector (new interior point).
    """
    n = len(cos)
    s = span
    t = n // 2 - s

    def idx(k):
        return (offset + k) % n

    side0 = [idx(k) for k in range(0, s + 1)]                  # c0 -> c1
    side1 = [idx(k) for k in range(s, s + t + 1)]              # c1 -> c2
    side2 = [idx(k) for k in range(s + t, 2 * s + t + 1)]      # c2 -> c3
    side3 = [idx(k) for k in range(2 * s + t, 2 * s + 2 * t + 1)]  # c3 -> c0

    p00, p10 = cos[side0[0]], cos[side0[s]]
    p11, p01 = cos[side1[t]], cos[side3[0]]
    grid = []
    for u in range(s + 1):
        col = []
        for v in range(t + 1):
            if v == 0:
                col.append(side0[u])
            elif v == t:
                col.append(side2[s - u])
            elif u == 0:
                col.append(side3[t - v])
            elif u == s:
                col.append(side1[v])
            else:
                uu, vv = u / s, v / t
                c0, c1 = cos[side0[u]], cos[side2[s - u]]
                d0, d1 = cos[side3[t - v]], cos[side1[v]]
                p = ((1 - vv) * c0 + vv * c1 + (1 - uu) * d0 + uu * d1
                     - ((1 - uu) * (1 - vv) * p00 + uu * (1 - vv) * p10
                        + (1 - uu) * vv * p01 + uu * vv * p11))
                col.append(p)
        grid.append(col)
    return grid


# ---------------------------------------------------------------- bridge
def bridge_align(a, b, a_closed, b_closed):
    """Reorder border b so b[i] pairs with a[i]. None if they can't pair."""
    n = len(a)
    if len(b) != n or a_closed != b_closed:
        return None

    def cost(seq):
        return sum((x.co - y.co).length for x, y in zip(a, seq))

    if not a_closed:
        rb = b[::-1]
        return b if cost(b) <= cost(rb) else rb
    best = None
    for seq in (b, b[::-1]):
        for k in range(n):
            r = seq[k:] + seq[:k]
            c = cost(r)
            if best is None or c < best[0]:
                best = (c, r)
    return best[1]


def bridge_spans(PA, a_closed, ia, PB, b_closed, ib, same=False, max_ratio=1.6):
    """Facing runs of two borders, grown out from the two hovered edges.

    PA, PB are world positions along each border chain; the hovered edges are
    (ia, ia+1) and (ib, ib+1). Both runs grow one edge at a time, in lockstep,
    while the new pair of verts still faces each other (gap close to the
    starting gap, edges running the same way). Returns (index run on A,
    index run on B, closed) with equal lengths, or None.
    """
    na, nb = len(PA), len(PB)

    def wrap(i, n, closed):
        if closed:
            return i % n
        return i if 0 <= i < n else None

    a0, a1 = ia, wrap(ia + 1, na, a_closed)
    if a1 is None:
        return None
    # pair the A edge with the closest B edge near the hovered one (hovering one
    # edge off shouldn't skew the whole bridge), either way round
    best = None
    for shift in range(-3, 4):
        j0 = wrap(ib + shift, nb, b_closed)
        if j0 is None:
            continue
        for d in (1, -1):
            j1 = wrap(j0 + d, nb, b_closed)
            if j1 is None or (same and {j0, j1} & {a0, a1}):
                continue
            c = (PA[a0] - PB[j0]).length + (PA[a1] - PB[j1]).length + abs(shift) * 1e-6
            if best is None or c < best[0]:
                best = (c, j0, j1, d)
    if best is None:
        return None
    _c, b0, b1, db = best
    sa, sb = [a0, a1], [b0, b1]
    used = set(sa) | (set(sb) if same else set())
    used_b = set(sb) if not same else used
    base = _c / 2.0
    limit = max(base * max_ratio, 1e-9)

    def ok(pa_from, pa_to, pb_from, pb_to):
        gap = (PA[pa_to] - PB[pb_to]).length
        if gap > limit:
            return False
        da, dbv = PA[pa_to] - PA[pa_from], PB[pb_to] - PB[pb_from]
        return da.length > 1e-12 and dbv.length > 1e-12 and da.dot(dbv) > 0.0

    closed = False
    for _ in range(max(na, nb) * 2):
        grew = False
        # forward end
        na_i, nb_i = wrap(sa[-1] + 1, na, a_closed), wrap(sb[-1] + db, nb, b_closed)
        if na_i is not None and nb_i is not None:
            if na_i == sa[0] and nb_i == sb[0] and a_closed and b_closed:
                closed = True
                break
            if (na_i not in used and nb_i not in used_b and (not same or na_i != nb_i)
                    and ok(sa[-1], na_i, sb[-1], nb_i)):
                sa.append(na_i)
                sb.append(nb_i)
                used.add(na_i)
                used_b.add(nb_i)
                grew = True
        # back end
        na_i, nb_i = wrap(sa[0] - 1, na, a_closed), wrap(sb[0] - db, nb, b_closed)
        if na_i is not None and nb_i is not None:
            if (na_i not in used and nb_i not in used_b and (not same or na_i != nb_i)
                    and ok(sa[0], na_i, sb[0], nb_i)):
                sa.insert(0, na_i)
                sb.insert(0, nb_i)
                used.add(na_i)
                used_b.add(nb_i)
                grew = True
        if not grew:
            break
    return sa, sb, closed


# ---------------------------------------------------------------- curves
def _cr(p0, p1, p2, p3, t):
    t2, t3 = t * t, t * t * t
    return 0.5 * ((2.0 * p1) + (p2 - p0) * t
                  + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
                  + (3.0 * p1 - p0 - 3.0 * p2 + p3) * t3)


def catmull_rom(points, closed, steps=12):
    """Dense points on a Catmull-Rom spline through `points`.

    Open curves include both end points; closed curves don't repeat the start.
    Segment i of the control polygon covers dense[i*steps:(i+1)*steps].
    """
    n = len(points)
    if n < 2:
        return [p.copy() for p in points]
    out = []
    segs = n if closed else n - 1
    for i in range(segs):
        p0 = points[(i - 1) % n] if closed else points[max(i - 1, 0)]
        p1 = points[i]
        p2 = points[(i + 1) % n]
        p3 = points[(i + 2) % n] if closed else points[min(i + 2, n - 1)]
        for k in range(steps):
            out.append(_cr(p0, p1, p2, p3, k / steps))
    if not closed:
        out.append(points[-1].copy())
    return out


def polyline_length(pts, closed):
    n = len(pts)
    total = sum((pts[i + 1] - pts[i]).length for i in range(n - 1))
    if closed and n > 1:
        total += (pts[0] - pts[-1]).length
    return total


def resample(pts, count, closed):
    """`count` points evenly spaced by arc length (works for 2D or 3D)."""
    if not pts:
        return []
    if closed:
        pts = list(pts) + [pts[0]]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + (pts[i] - pts[i - 1]).length)
    total = cum[-1]
    if total <= 0.0 or count < 1:
        return [pts[0].copy() for _ in range(max(count, 0))]
    step = total / count if closed else total / max(count - 1, 1)
    out, j = [], 0
    for k in range(count):
        d = k * step
        while j < len(cum) - 2 and cum[j + 1] < d:
            j += 1
        seg = cum[j + 1] - cum[j]
        f = (d - cum[j]) / seg if seg > 0.0 else 0.0
        out.append(pts[j].lerp(pts[j + 1], min(max(f, 0.0), 1.0)))
    return out
