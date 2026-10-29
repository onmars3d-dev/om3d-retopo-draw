# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure geometry helpers. No viewport access, so these can be tested headless.

Screen-space data is passed in as a dict {BMVert: Vector2}.
"""
import itertools
import math

import bmesh
from mathutils import Vector

REUSE_EDGE_BONUS = 40.0  # px, favors quads that attach to existing boundary edges
DOT_BONUS = 80.0  # px, favors quads that use the user's dots (that's what dots are for)
MAX_CORNER_DEG = 160.0  # corners straighter than this make a sliver quad


# ---------------------------------------------------------------- queries
def is_open_vert(v):
    """A vert that can still take a new face: loose, wire, or on a boundary."""
    if not v.link_edges:
        return True
    return any(len(e.link_faces) < 2 for e in v.link_edges)


def is_open_edge(e):
    return len(e.link_faces) < 2


def is_border_vert(v):
    return any(len(e.link_faces) < 2 for e in v.link_edges)


def point_in_poly(p, poly):
    x, y = p.x, p.y
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i].x, poly[i].y
        xj, yj = poly[j].x, poly[j].y
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
                inside = not inside
        j = i
    return inside


def order_around_centroid(points):
    c = sum(points, Vector((0.0, 0.0))) / len(points)
    return sorted(range(len(points)),
                  key=lambda i: math.atan2(points[i].y - c.y, points[i].x - c.x))


def is_convex(poly, min_area=4.0):
    n = len(poly)
    sign = 0
    area = 0.0
    for i in range(n):
        a, b, c = poly[i], poly[(i + 1) % n], poly[(i + 2) % n]
        cross = (b - a).cross(c - b)
        if abs(cross) < 1e-6:
            return False
        s = 1 if cross > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
        area += a.cross(b)
    return abs(area) * 0.5 >= min_area


def segment_param(p, a, b):
    """(distance, t) of point p to segment a-b, t in [0, 1] from a."""
    ab = b - a
    denom = ab.length_squared
    if denom == 0.0:
        return (p - a).length, 0.0
    t = max(0.0, min(1.0, (p - a).dot(ab) / denom))
    return (p - (a + ab * t)).length, t


def dist_point_segment(p, a, b):
    return segment_param(p, a, b)[0]


# ---------------------------------------------------------------- picking
def pick_vert(proj, mouse, radius, exclude=()):
    best, best_d = None, radius
    for v, co in proj.items():
        if v in exclude:
            continue
        d = (co - mouse).length
        if d <= best_d:
            best, best_d = v, d
    return best


def pick_edge(proj, mouse, radius, open_only=False):
    """Nearest edge (both verts projected) under the mouse. Returns (edge, t)."""
    seen = set()
    best, best_d, best_t = None, radius, 0.5
    for v in proj:
        for e in v.link_edges:
            if e in seen:
                continue
            seen.add(e)
            if open_only and not is_open_edge(e):
                continue
            a, b = proj.get(e.verts[0]), proj.get(e.verts[1])
            if a is None or b is None:
                continue
            d, t = segment_param(mouse, a, b)
            if d <= best_d:
                best, best_d, best_t = e, d, t
    return best, best_t


def pick_open_edge(proj, mouse, radius):
    return pick_edge(proj, mouse, radius, open_only=True)[0]


def pick_face(proj, mouse):
    """Face whose projected outline contains the mouse."""
    faces = {f for v in proj for f in v.link_faces}
    for f in faces:
        poly = [proj.get(v) for v in f.verts]
        if None in poly:
            continue
        if point_in_poly(mouse, poly):
            return f
    return None


# ---------------------------------------------------------------- fill
def _has_flat_corner(poly):
    """A corner of the screen-space quad is nearly a straight line (sliver quad)."""
    n = len(poly)
    for i in range(n):
        a = poly[i - 1] - poly[i]
        b = poly[(i + 1) % n] - poly[i]
        if a.length < 1e-6 or b.length < 1e-6:
            return True
        if math.degrees(a.angle(b)) > MAX_CORNER_DEG:
            return True
    return False


def find_fill_quad(bm, proj, mouse, radius, max_candidates=8, accept=None):
    """Best quad of open verts surrounding the mouse, or None.

    Returns verts ordered around the quad. Rejects quads that would
    duplicate a face, cross an existing edge, overlap an existing face,
    or attach to an edge that already has two faces.
    """
    cands = [(v, co) for v, co in proj.items()
             if is_open_vert(v) and (co - mouse).length <= radius]
    cands.sort(key=lambda c: (c[1] - mouse).length)
    cands = cands[:max_candidates]
    if len(cands) < 4:
        return None

    nearby_faces = {f for v, _ in cands for f in v.link_faces}
    face_polys = []
    for f in nearby_faces:
        poly = [proj.get(v) for v in f.verts]
        if None in poly:
            continue
        if point_in_poly(mouse, poly):
            return None  # hovering an existing face
        face_polys.append(sum(poly, Vector((0.0, 0.0))) / len(poly))

    best, best_score = None, float("inf")
    for combo in itertools.combinations(cands, 4):
        pts = [c[1] for c in combo]
        order = order_around_centroid(pts)
        verts = [combo[i][0] for i in order]
        poly = [pts[i] for i in order]
        if not is_convex(poly) or not point_in_poly(mouse, poly):
            continue
        if bm.faces.get(verts) is not None:
            continue
        if bm.edges.get((verts[0], verts[2])) or bm.edges.get((verts[1], verts[3])):
            continue
        ok, reuse = True, 0
        for i in range(4):
            e = bm.edges.get((verts[i], verts[(i + 1) % 4]))
            if e is not None:
                if len(e.link_faces) >= 2:
                    ok = False
                    break
                reuse += 1
        if not ok:
            continue
        if any(point_in_poly(c, poly) for c in face_polys):
            continue
        if accept is not None and not accept(verts, reuse):
            continue
        if _has_flat_corner(poly):
            continue
        perim = sum((poly[i] - poly[(i + 1) % 4]).length for i in range(4))
        dots = sum(1 for v in verts if not v.link_edges)
        score = perim - reuse * REUSE_EDGE_BONUS - dots * DOT_BONUS
        if score < best_score:
            best, best_score = verts, score
    return best


def orient_face(face, obj_mw, surface_normal_world):
    """Flip the face if it points into the reference surface."""
    face.normal_update()
    nmat = obj_mw.to_3x3().inverted_safe().transposed()
    n_world = (nmat @ face.normal).normalized()
    if n_world.dot(surface_normal_world) < 0.0:
        face.normal_flip()


# ---------------------------------------------------------------- border loops
def boundary_chain(edge):
    """Ordered verts of the border edge loop through `edge`.

    Follows border edges the way Maya's border loop does: it passes through
    regular border verts (3 edges, 2 of them on the border) and stops at
    corners or poles. Returns (verts, closed). A wire edge or a non-border
    edge returns just its own two verts.
    """
    nf = len(edge.link_faces)
    if nf > 1:
        return list(edge.verts), False
    # border edges pass through 3-edge verts; wire edges (pen edge loops) through 2-edge verts
    valence = 3 if nf == 1 else 2

    def border_edges(v):
        return [e for e in v.link_edges if len(e.link_faces) == nf]

    def walk(v, prev):
        out, seen = [], {v}
        while True:
            if len(v.link_edges) != valence:
                return out, False
            nxt = [e for e in border_edges(v) if e is not prev]
            if len(nxt) != 1:
                return out, False
            e = nxt[0]
            if e is edge:
                return out, True
            v = e.other_vert(v)
            if v in seen:
                return out, False
            seen.add(v)
            out.append(v)
            prev = e

    a, b = edge.verts
    fwd, closed = walk(b, edge)
    if closed:
        # walk ended back at `a`, which it appended last
        return [a, b] + fwd[:-1], True
    back, _ = walk(a, edge)
    verts = list(reversed(back)) + [a, b] + fwd
    # guard against a walk that wrapped into itself
    uniq = []
    for v in verts:
        if v in uniq:
            break
        uniq.append(v)
    return uniq, False


def edge_loop(edge):
    """Edges of the edge loop through an interior edge (Maya style).

    At each 4-edge vert the loop continues on the edge that shares no face with
    the current one; it stops at poles and borders. Returns (edges, closed).
    """
    def step(e, v):
        if len(v.link_edges) != 4:
            return None
        faces = set(e.link_faces)
        cands = [x for x in v.link_edges if x is not e and not (set(x.link_faces) & faces)]
        return cands[0] if len(cands) == 1 else None

    loop, seen = [edge], {edge}
    e, v = edge, edge.verts[1]
    while True:
        n = step(e, v)
        if n is None or n in seen:
            break
        loop.append(n)
        seen.add(n)
        e, v = n, n.other_vert(v)
    if step(e, v) is edge and len(loop) > 2:
        return loop, True
    back = []
    e, v = edge, edge.verts[0]
    while True:
        n = step(e, v)
        if n is None or n in seen:
            break
        back.append(n)
        seen.add(n)
        e, v = n, n.other_vert(v)
    return back[::-1] + loop, False


def extrude_chain(bm, verts, closed):
    """Duplicate `verts` and bridge them with quads. Returns (new_verts, faces) or None."""
    new = [bm.verts.new(v.co) for v in verts]
    n = len(verts)
    segs = n if closed else n - 1
    faces = []
    try:
        for i in range(segs):
            j = (i + 1) % n
            faces.append(bm.faces.new((verts[i], verts[j], new[j], new[i])))
    except ValueError:
        for v in new:
            if v.is_valid:
                bm.verts.remove(v)
        return None
    return new, faces


# ---------------------------------------------------------------- edge rings
def _opposite(edge, face):
    for loop in face.loops:
        if loop.edge is edge:
            return loop, loop.link_loop_next.link_loop_next
    return None, None


def edge_ring(edge):
    """Edge ring through quads. Returns (edges, faces, closed).

    faces[i] sits between edges[i] and edges[i+1]; when closed the last
    face sits between edges[-1] and edges[0].
    """
    def walk(e, f):
        es, fs, seen = [], [], {e}
        while f is not None and len(f.verts) == 4:
            _l, lo = _opposite(e, f)
            if lo is None:
                break
            o = lo.edge
            fs.append(f)
            if o is edge:
                return es, fs, True
            if o in seen:
                fs.pop()
                break
            seen.add(o)
            es.append(o)
            nxt = [g for g in o.link_faces if g is not f]
            f = nxt[0] if len(nxt) == 1 else None
            e = o
        return es, fs, False

    faces = list(edge.link_faces)
    if not faces:
        return [edge], [], False
    fe, ff, closed = walk(edge, faces[0])
    if closed:
        return [edge] + fe, ff, True
    be, bf = [], []
    if len(faces) == 2:
        be, bf, _ = walk(edge, faces[1])
    return list(reversed(be)) + [edge] + fe, list(reversed(bf)) + ff, False


def ring_sides(edges, faces, a0, start=0):
    """For each ring edge, the vert on the same side as a0 (a vert of edges[start]).

    Returns (A, B) lists aligned with edges.
    """
    def step(e_from, f, a_from):
        l, lo = _opposite(e_from, f)
        return lo.link_loop_next.vert if a_from is l.vert else lo.vert

    n = len(edges)
    A = [None] * n
    A[start] = a0
    for i in range(start, n - 1):
        A[i + 1] = step(edges[i], faces[i], A[i])
    for i in range(start - 1, -1, -1):
        A[i] = step(edges[i + 1], faces[i], A[i + 1])
    B = [e.other_vert(a) for e, a in zip(edges, A)]
    return A, B


def insert_loop(bm, edges, faces, A, B, t):
    """Split every ring edge at t (from A to B) and connect across the faces."""
    n = len(edges)
    new = []
    for e, a, b in zip(edges, A, B):
        co = a.co.lerp(b.co, t)
        _ne, nv = bmesh.utils.edge_split(e, a, t)
        nv.co = co
        new.append(nv)
    for i, f in enumerate(faces):
        v1, v2 = new[i], new[(i + 1) % n]
        if f.is_valid:
            bmesh.utils.face_split(f, v1, v2)
    return new


# ---------------------------------------------------------------- relax / delete
def relax_positions(weights, relax_border=False):
    """Laplacian relax. weights: {BMVert: 0..1}. Returns {BMVert: new local co}.

    Border verts are pinned unless relax_border, in which case they only
    slide toward their two border neighbours so the outline stays put.
    """
    out = {}
    for v, w in weights.items():
        if w <= 0.0 or not v.link_edges:
            continue
        if is_border_vert(v):
            if not relax_border or len(v.link_edges) != 3:
                continue  # corners and poles always stay put
            nbrs = [e.other_vert(v) for e in v.link_edges if len(e.link_faces) < 2]
            if len(nbrs) != 2:
                continue
        else:
            nbrs = [e.other_vert(v) for e in v.link_edges]
        avg = sum((n.co for n in nbrs), Vector()) / len(nbrs)
        out[v] = v.co.lerp(avg, w)
    return out


def delete_face_clean(bm, face):
    """Remove a face plus any of its edges and verts left unused."""
    edges = list(face.edges)
    verts = list(face.verts)
    bm.faces.remove(face)
    for e in edges:
        if e.is_valid and not e.link_faces:
            bm.edges.remove(e)
    for v in verts:
        if v.is_valid and not v.link_edges:
            bm.verts.remove(v)


def remove_loose_verts(bm):
    loose = [v for v in bm.verts if not v.link_edges]
    for v in loose:
        bm.verts.remove(v)
    return len(loose)
