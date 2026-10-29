# SPDX-License-Identifier: GPL-3.0-or-later
import math

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

MODE_BLUE = (0.45, 0.78, 1.0)
COL_FACE = (0.05, 0.38, 1.0)
COL_FACE_EDGE = (0.70, 0.90, 1.0, 1.0)
COL_DOT = (0.25, 0.85, 1.0, 1.0)
COL_HOVER = (1.0, 0.8, 0.1, 1.0)
COL_DELETE = (1.0, 0.25, 0.2, 1.0)
COL_FILL = (0.3, 1.0, 0.45, 0.30)
COL_FILL_EDGE = (0.3, 1.0, 0.45, 1.0)
COL_EXTEND = (1.0, 0.55, 0.1, 1.0)
COL_RING = (1.0, 0.9, 0.2, 1.0)
COL_PREVIEW = (0.95, 0.97, 1.0, 0.95)
COL_MARK = (0.2, 1.0, 0.95, 1.0)
COL_SNAP = (1.0, 0.35, 0.85, 1.0)

DIRTY = [True]  # set by the operator and a depsgraph handler when the mesh changes
EXTERNAL = [False]  # set on any depsgraph update; Retopo Draw then checks for off-surface verts
LAST_ERROR = [None]  # last draw exception, printed once instead of every frame

_cache = {}
_view_cache = {}


def _shader(name, fallback="UNIFORM_COLOR"):
    if name not in _cache:
        try:
            _cache[name] = gpu.shader.from_builtin(name)
        except Exception:
            _cache[name] = gpu.shader.from_builtin(fallback)
    return _cache[name]


def _points(coords, size, color):
    if not coords:
        return
    sh = _shader("POINT_UNIFORM_COLOR")
    gpu.state.point_size_set(size)
    batch = batch_for_shader(sh, "POINTS", {"pos": coords})
    sh.uniform_float("color", color)
    batch.draw(sh)


def _lines(coords, width, color):
    if not coords:
        return
    sh = _shader("POLYLINE_UNIFORM_COLOR")
    region = bpy.context.region
    try:
        sh.uniform_float("viewportSize", (region.width, region.height))
        sh.uniform_float("lineWidth", width)
    except Exception:
        pass
    batch = batch_for_shader(sh, "LINES", {"pos": coords})
    sh.uniform_float("color", color)
    batch.draw(sh)


def _tris(coords, color):
    if not coords:
        return
    sh = _shader("UNIFORM_COLOR")
    batch = batch_for_shader(sh, "TRIS", {"pos": coords})
    sh.uniform_float("color", color)
    batch.draw(sh)


def _quad_fill(quad, color):
    _tris([quad[0], quad[1], quad[2], quad[0], quad[2], quad[3]], color)


def _loop_lines(pts):
    out = []
    for i in range(len(pts)):
        out += [pts[i], pts[(i + 1) % len(pts)]]
    return out


# ---------------------------------------------------------------- 3D overlay
def _front_tris(op, persp, eye, toward):
    """Triangles whose face points toward the viewer."""
    out = []
    pts = op.ov_tris
    for i, (c, n) in enumerate(op.ov_tri_faces):
        if n.dot((eye - c) if persp else toward) > 0.0:
            out += pts[3 * i:3 * i + 3]
    return out


def _front_edges(op, persp, eye, toward):
    """Edges with at least one face turned toward the viewer (wire edges always)."""
    out = []
    pts = op.ov_edges
    for i, faces in enumerate(op.ov_edge_faces):
        if faces:
            if not any(n.dot((eye - c) if persp else toward) > 0.0 for c, n in faces):
                continue
        out += [pts[2 * i], pts[2 * i + 1]]
    return out


def _report(exc):
    import traceback
    msg = traceback.format_exc()
    if LAST_ERROR[0] != msg:
        LAST_ERROR[0] = msg
        print("OM3D Retopo Draw overlay error:\n" + msg)


def draw_view(op):
    try:
        _draw_view(op)
    except ReferenceError:
        pass  # operator already gone
    except Exception as exc:
        _report(exc)


def _draw_view(op):
    ctx = bpy.context
    if ctx.area != op.area or ctx.region != op.region:
        return
    if op.obj.mode != "EDIT":
        return  # left Edit Mode; the modal finishes on its next event
    if DIRTY[0]:
        op.rebuild_overlay()
        DIRTY[0] = False
        _view_cache.clear()

    # Flat retopo quads dip under a curved high poly, so pull everything toward
    # the eye (along each point's view ray, so nothing slides on screen).
    rv3d = ctx.region_data
    persp = rv3d.is_perspective
    eye = rv3d.view_matrix.inverted().translation
    toward = rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))
    try:
        user_off = op.area.spaces.active.overlay.retopology_offset
    except Exception:
        user_off = 0.0
    # big flat quads sag further under the surface, so scale with edge length
    off = max(user_off, rv3d.view_distance * 0.006, op.ov_edge_len * 0.2)

    def lifted(pts):
        if persp:
            return [p + (eye - p).normalized() * off for p in pts]
        return [p + toward * off for p in pts]

    cull = op.settings.cull_backfaces
    key = (tuple(tuple(r) for r in rv3d.view_matrix), round(off, 7), cull)
    if _view_cache.get("key") != key:
        _view_cache["key"] = key
        tris = _front_tris(op, persp, eye, toward) if cull else op.ov_tris
        _view_cache["tris"] = lifted(tris)
        edges = _front_edges(op, persp, eye, toward) if cull else op.ov_edges
        _view_cache["edges"] = lifted(edges)
        _view_cache["gtris"] = lifted(getattr(op, "mirror_ghost_tris", []))
        _view_cache["gedges"] = lifted(getattr(op, "mirror_ghost_edges", []))
        _view_cache["dots"] = lifted(op.ov_dots)
    opacity = op.settings.face_opacity

    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("LESS_EQUAL")
    gpu.state.depth_mask_set(False)
    if opacity > 0.0:
        _tris(_view_cache["tris"], (*op.settings.face_color, opacity))
    if op.settings.edge_opacity > 0.0:
        _lines(_view_cache["edges"], 2.0, (*op.settings.edge_color, op.settings.edge_opacity))
    _points(_view_cache["dots"], 9.0, COL_DOT)
    # the mirrored half, faint, so the whole shape reads while you work on one side
    if _view_cache.get("gtris"):
        _tris(_view_cache["gtris"], (*op.settings.face_color, opacity * 0.35))
    if _view_cache.get("gedges") and op.settings.edge_opacity > 0.0:
        _lines(_view_cache["gedges"], 1.0, (*op.settings.edge_color, op.settings.edge_opacity * 0.35))

    gpu.state.depth_test_set("NONE")
    hover_col = COL_DELETE if op.delete_mode else COL_HOVER

    if op.draw_quad:
        q = lifted(op.draw_quad)
        _quad_fill(q, COL_FILL)
        _lines(_loop_lines(q), 2.0, COL_FILL_EDGE)
    if op.draw_face:
        f = lifted(op.draw_face)
        fcol = COL_DELETE if op.delete_mode else COL_HOVER
        if len(f) == 4:
            _quad_fill(f, (*fcol[:3], 0.35 if op.delete_mode else 0.2))
        _lines(_loop_lines(f), 3.0, fcol)
    if op.draw_lines:
        col = {"RING": COL_RING, "HOVER": COL_HOVER, "DELETE": COL_DELETE}.get(op.draw_lines_kind, COL_EXTEND)
        _lines(lifted(op.draw_lines), 3.0, col)
    if op.draw_vert is not None:
        _points(lifted([op.draw_vert]), 14.0, hover_col)
    if getattr(op, "snap_segs", None):
        _lines(lifted(op.snap_segs), 4.0, COL_SNAP)
    if getattr(op, "snap_pts", None):
        _points(lifted(op.snap_pts), 18.0, COL_SNAP)

    # grid fill / bridge / pen previews
    if getattr(op, "mark_lines", None):
        _lines(lifted(op.mark_lines), 4.0, COL_MARK)
    if getattr(op, "preview_lines", None):
        _lines(lifted(op.preview_lines), 2.0, COL_PREVIEW)
    if getattr(op, "preview_pts", None):
        _points(lifted(op.preview_pts), 12.0, COL_EXTEND)
    if getattr(op, "preview_hot", None) is not None:
        _points(lifted([op.preview_hot]), 17.0, COL_HOVER)

    gpu.state.point_size_set(1.0)
    gpu.state.depth_mask_set(True)
    gpu.state.blend_set("NONE")


# ---------------------------------------------------------------- 2D overlay
def draw_px(op):
    try:
        _draw_px(op)
    except ReferenceError:
        pass
    except Exception as exc:
        _report(exc)


def _rect_lines(x0, y0, x1, y1):
    pts = [(x0, y0), (x1, y0), (x1, y0), (x1, y1), (x1, y1), (x0, y1), (x0, y1), (x0, y0)]
    return [(x, y, 0.0) for x, y in pts]


def _draw_px(op):
    ctx = bpy.context
    if ctx.area != op.area or ctx.region != op.region:
        return
    region = ctx.region
    w, h = region.width, region.height

    # visible-area bounds (skip the overlapping sidebar / toolbar)
    x0, x1 = 0, w
    for r in op.area.regions:
        if r.type == "UI" and r.width > 1 and r.x > region.x:
            x1 = min(x1, r.x - region.x)
        if r.type == "TOOLS" and r.width > 1:
            x0 = max(x0, r.x + r.width - region.x)

    gpu.state.blend_set("ALPHA")
    sh = _shader("POLYLINE_UNIFORM_COLOR")
    try:
        sh.uniform_float("viewportSize", (w, h))
        sh.uniform_float("lineWidth", 4.0)
    except Exception:
        pass
    pad = 2
    batch = batch_for_shader(sh, "LINES", {"pos": _rect_lines(x0 + pad, pad, x1 - pad, h - pad)})
    sh.uniform_float("color", (*MODE_BLUE, 0.9))
    batch.draw(sh)

    # label
    font = 0
    blf.size(font, 18)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.8)
    title = "RETOPO DRAW MODE"
    tw, th = blf.dimensions(font, title)
    cx = (x0 + x1) * 0.5
    top = 0  # headers overlap the top of the viewport region
    for r in op.area.regions:
        if r.type in {"HEADER", "TOOL_HEADER"} and r.y >= region.y + h * 0.5:
            top += r.height
    y = h - top - 34
    # dark pill behind the label so it reads on any background
    bw = max(tw, blf.dimensions(font, op.hint)[0] * 0.7 if op.hint else 0) + 36
    bh = 54 if op.hint else 32
    bx0, bx1 = cx - bw * 0.5, cx + bw * 0.5
    by0, by1 = y - (bh - 26), y + 24
    _tris([(bx0, by0, 0), (bx1, by0, 0), (bx1, by1, 0), (bx0, by0, 0), (bx1, by1, 0), (bx0, by1, 0)],
          (0.03, 0.08, 0.15, 0.75))
    blf.size(font, 18)
    blf.color(font, *MODE_BLUE, 1.0)
    blf.position(font, cx - tw * 0.5, y, 0)
    blf.draw(font, title)
    if op.hint:
        blf.size(font, 12)
        hw, _hh = blf.dimensions(font, op.hint)
        blf.color(font, 1.0, 1.0, 1.0, 0.9)
        blf.position(font, cx - hw * 0.5, y - 20, 0)
        blf.draw(font, op.hint)
    blf.disable(font, blf.SHADOW)

    # pen stroke being drawn
    stroke = getattr(op, "stroke", None)
    if stroke and len(stroke) > 1:
        try:
            sh.uniform_float("lineWidth", 3.0)
        except Exception:
            pass
        pts = []
        for i in range(len(stroke) - 1):
            pts += [(stroke[i].x, stroke[i].y, 0.0), (stroke[i + 1].x, stroke[i + 1].y, 0.0)]
        batch = batch_for_shader(sh, "LINES", {"pos": pts})
        sh.uniform_float("color", COL_EXTEND)
        batch.draw(sh)

    # quad strip width
    if getattr(op, "show_strip", False) or op.state == "STRIP_STROKE":
        r = float(op.settings.strip_width) * 0.5
        m = op.mouse
        pts = []
        for i in range(32):
            a0 = 2 * math.pi * i / 32
            a1 = 2 * math.pi * (i + 1) / 32
            pts += [(m.x + r * math.cos(a0), m.y + r * math.sin(a0), 0.0),
                    (m.x + r * math.cos(a1), m.y + r * math.sin(a1), 0.0)]
        try:
            sh.uniform_float("lineWidth", 1.5)
        except Exception:
            pass
        batch = batch_for_shader(sh, "LINES", {"pos": pts})
        sh.uniform_float("color", (*COL_EXTEND[:3], 0.8))
        batch.draw(sh)

    # relax brush
    if op.state == "RELAX":
        r = float(op.settings.relax_radius)
        m = op.mouse
        n = 48
        pts = []
        for i in range(n):
            a0 = 2 * math.pi * i / n
            a1 = 2 * math.pi * (i + 1) / n
            pts += [(m.x + r * math.cos(a0), m.y + r * math.sin(a0), 0.0),
                    (m.x + r * math.cos(a1), m.y + r * math.sin(a1), 0.0)]
        try:
            sh.uniform_float("lineWidth", 1.5)
        except Exception:
            pass
        batch = batch_for_shader(sh, "LINES", {"pos": pts})
        alpha = 0.95 if op.state == "RELAX" else 0.45
        sh.uniform_float("color", (1.0, 1.0, 1.0, alpha))
        batch.draw(sh)

    gpu.state.blend_set("NONE")
