# SPDX-License-Identifier: GPL-3.0-or-later
"""The Retopo Draw overlay outside Retopo Draw: tinted faces (and edges in Object Mode) on
retopo meshes while you work with Blender's own tools (G, R, S, inset...). In Edit Mode
Blender's own verts, edges and selection are left alone."""
import types

import bmesh
import bpy
import gpu
from mathutils import Vector

from . import draw

RETOPO_TAG = "om3d_retopo"

_state = {"dirty": set(), "geo": {}, "views": {}, "handle": None, "err": None, "all_dirty": True}


def mark_dirty(*_args):
    _state["all_dirty"] = True


def _targets(context):
    """Every visible retopo mesh, whichever object is active. The toggle alone decides."""
    s = getattr(context.scene, "om3d_topo", None)
    if s is None or not s.overlay_outside:
        return None, []
    if getattr(context.window_manager, "om3d_topo_running", False):
        return s, []  # Retopo Draw draws its own
    out = []
    for obj in context.view_layer.objects:
        if (obj.type == "MESH" and obj.get(RETOPO_TAG) and obj != s.reference
                and obj.mode in {"EDIT", "OBJECT"} and obj.visible_get()):
            out.append(obj)
    return s, out


def _build(obj, s):
    mw = obj.matrix_world
    nmat = mw.to_3x3().inverted_safe().transposed()
    edit = obj.mode == "EDIT"
    if edit:
        bm, own = bmesh.from_edit_mesh(obj.data), False
    else:
        bm, own = bmesh.new(), True
        bm.from_mesh(obj.data)
    try:
        off = s.surface_offset
        if off > 0.0:
            bm.normal_update()

            def P(v):
                return mw @ v.co + (nmat @ v.normal).normalized() * off
        else:
            def P(v):
                return mw @ v.co
        fdata = {}

        def fd(f):
            d = fdata.get(f)
            if d is None:
                f.normal_update()
                d = fdata[f] = (mw @ f.calc_center_median(), (nmat @ f.normal).normalized())
            return d

        g = types.SimpleNamespace(ov_tris=[], ov_tri_faces=[], ov_edges=[], ov_edge_faces=[],
                                  dots=[], verts=[], ghost_tris=[], ghost_edges=[], edge_len=0.0)
        # Same look as Retopo Draw (full tint, black edges). In Edit Mode, selected faces
        # and edges are left out so Blender's own selection colors show through.
        for lt in bm.calc_loop_triangles():
            f = lt[0].face
            if f.hide or (edit and f.select):
                continue
            g.ov_tris += [P(l.vert) for l in lt]
            g.ov_tri_faces.append(fd(f))
        total = 0.0
        for e in bm.edges:
            if e.hide:
                continue
            a, b = P(e.verts[0]), P(e.verts[1])
            total += (a - b).length
            if edit and e.select:
                continue
            g.ov_edges += [a, b]
            g.ov_edge_faces.append([fd(f) for f in e.link_faces])
        n = len(bm.edges)
        g.edge_len = total / n if n else 0.0
        for v in bm.verts:
            if v.hide:
                continue
            if not v.link_edges:
                g.dots.append(P(v))
            elif edit and not v.select:
                g.verts.append(P(v))  # selected ones are left to Blender
        m = obj.modifiers.get("OM3D Mirror")
        if s.mirror and m is not None and m.show_viewport:
            ref = s.reference
            ax = {"X": 0, "Y": 1, "Z": 2}[s.mirror_axis]
            ri, rm = ref.matrix_world.inverted_safe(), ref.matrix_world

            def R(p):
                w = ri @ p
                w[ax] = -w[ax]
                return rm @ w
            g.ghost_tris = [R(p) for p in g.ov_tris]
            g.ghost_edges = [R(p) for p in g.ov_edges]
        return g
    finally:
        if own:
            bm.free()


def _draw():
    context = bpy.context
    try:
        s, objs = _targets(context)
        if not objs:
            return
        rv3d = context.region_data
        if rv3d is None:
            return
        if _state["all_dirty"]:
            _state["geo"], _state["views"], _state["all_dirty"] = {}, {}, False
        for obj in objs:
            _draw_obj(context, rv3d, s, obj)
    except ReferenceError:
        pass
    except Exception:
        import traceback
        msg = traceback.format_exc()
        if _state["err"] != msg:
            _state["err"] = msg
            print("OM3D overlay error:\n" + msg)


def _draw_obj(context, rv3d, s, obj):
    name = obj.name
    g = _state["geo"].get(name)
    if g is None or g.mode != obj.mode:
        g = _state["geo"][name] = _build(obj, s)
        g.mode = obj.mode
    persp = rv3d.is_perspective
    eye = rv3d.view_matrix.inverted().translation
    toward = rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))
    try:
        user_off = context.space_data.overlay.retopology_offset
    except Exception:
        user_off = 0.0
    off = max(user_off, rv3d.view_distance * 0.006, g.edge_len * 0.2)

    def lifted(pts):
        if persp:
            return [p + (eye - p).normalized() * off for p in pts]
        return [p + toward * off for p in pts]

    cull = s.cull_backfaces
    key = (context.region.as_pointer(), name)
    vkey = (tuple(tuple(r) for r in rv3d.view_matrix), round(off, 7), cull)
    vc = _state["views"].get(key)
    if vc is None or vc["key"] != vkey:
        tris = draw._front_tris(g, persp, eye, toward) if cull else g.ov_tris
        edges = draw._front_edges(g, persp, eye, toward) if cull else g.ov_edges
        vc = {"key": vkey, "tris": lifted(tris), "edges": lifted(edges),
              "dots": lifted(g.dots), "verts": lifted(g.verts),
              "gtris": lifted(g.ghost_tris), "gedges": lifted(g.ghost_edges)}
        _state["views"][key] = vc

    op = s.face_opacity
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("LESS_EQUAL")
    gpu.state.depth_mask_set(False)
    if op > 0.0:
        draw._tris(vc["tris"], (*s.face_color, op))
        if vc["gtris"]:
            draw._tris(vc["gtris"], (*s.face_color, op * 0.35))
    if s.edge_opacity > 0.0:
        draw._lines(vc["edges"], 2.0, (*s.edge_color, s.edge_opacity))
        if vc["gedges"]:
            draw._lines(vc["gedges"], 1.0, (*s.edge_color, s.edge_opacity * 0.35))
    # vertex select mode: unselected verts on top of the tint, in Blender's vertex color
    if vc["verts"] and obj.mode == "EDIT" and context.tool_settings.mesh_select_mode[0]:
        th = context.preferences.themes[0].view_3d
        size = th.vertex_size * 2.0 * context.preferences.system.ui_scale
        draw._points(vc["verts"], max(size, 4.0), (*th.vertex, 1.0))
    if vc["dots"]:
        draw._points(vc["dots"], 9.0, draw.COL_DOT)
    gpu.state.point_size_set(1.0)
    gpu.state.depth_mask_set(True)
    gpu.state.depth_test_set("NONE")
    gpu.state.blend_set("NONE")


def header_button(self, context):
    """Viewport header toggle, shown once a reference is picked."""
    s = getattr(context.scene, "om3d_topo", None)
    if s is None or s.reference is None:
        return
    self.layout.prop(s, "overlay_outside", text="", icon="MOD_SHRINKWRAP")


# ---------------------------------------------------------------- Blender's Retopology overlay
_saved = {}  # SpaceView3D pointer -> the user's own show_retopology


def _view_spaces():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                yield area, area.spaces.active


def _retopo_wanted(context):
    if getattr(context.window_manager, "om3d_topo_running", False):
        return True  # Retopo Draw shows it while the mode is on
    s, objs = _targets(context)
    return any(o.mode == "EDIT" for o in objs)


def claim_retopology(space):
    """Retopo Draw starting: save the user's value (once) and switch the overlay on."""
    try:
        key = space.as_pointer()
        if key not in _saved:
            _saved[key] = space.overlay.show_retopology
        space.overlay.show_retopology = True
    except Exception:
        pass


def _sync_retopology():
    """Turn on Blender's Retopology overlay while a retopo mesh is in plain Edit Mode,
    so its own verts, edges and selection draw in front of the reference. The user's
    setting comes back when they leave Edit Mode, turn the toggle off, or start Retopo Draw."""
    try:
        want = _retopo_wanted(bpy.context)
        for area, sp in _view_spaces():
            key = sp.as_pointer()
            ov = sp.overlay
            if want:
                if key not in _saved:
                    _saved[key] = ov.show_retopology
                if not ov.show_retopology:
                    ov.show_retopology = True
                    area.tag_redraw()
            elif key in _saved:
                ov.show_retopology = _saved.pop(key)
                area.tag_redraw()
    except Exception:
        pass
    return 0.25


def _restore_all():
    try:
        for area, sp in _view_spaces():
            key = sp.as_pointer()
            if key in _saved:
                sp.overlay.show_retopology = _saved.pop(key)
    except Exception:
        pass
    _saved.clear()


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    mark_dirty()


def on_setting_changed(self, context):
    mark_dirty()
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def register():
    if _state["handle"] is None:
        _state["handle"] = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_VIEW")
    bpy.types.VIEW3D_HT_header.append(header_button)
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    if not bpy.app.timers.is_registered(_sync_retopology):
        bpy.app.timers.register(_sync_retopology, first_interval=0.25, persistent=True)


def unregister():
    try:
        bpy.types.VIEW3D_HT_header.remove(header_button)
    except Exception:
        pass
    if _state["handle"] is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_state["handle"], "WINDOW")
        except Exception:
            pass
        _state["handle"] = None
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    if bpy.app.timers.is_registered(_sync_retopology):
        bpy.app.timers.unregister(_sync_retopology)
    _restore_all()
