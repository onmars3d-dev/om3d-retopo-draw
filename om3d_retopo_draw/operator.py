# SPDX-License-Identifier: GPL-3.0-or-later
import bpy
import bmesh
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import core
from . import draw
from .tools import TopoToolsMixin, TOOL_STATES
from .edit import TopoEditMixin
from .quad import TopoQuadMixin, QUAD_STATES
from .symmetry import TopoSymmetryMixin
from . import mods
from . import conform
from . import overlay as ov_mod

NAV_EVENTS = {
    "MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE",
    "TRACKPADPAN", "TRACKPADZOOM", "MOUSEROTATE", "MOUSESMARTZOOM",
    "NDOF_MOTION", "HOME",
}
MOVE_EVENTS = {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}
MOD_KEYS = {"LEFT_SHIFT", "RIGHT_SHIFT", "LEFT_CTRL", "RIGHT_CTRL",
            "LEFT_ALT", "RIGHT_ALT", "OSKEY"}
DRAG_THRESHOLD = 4.0  # px


def ext_key_name(settings):
    return "Alt" if settings.extend_key == "ALT" else "Tab"


def status_text(settings):
    k = ext_key_name(settings)
    return (f"LMB: dot / move   Shift+LMB: fill   Shift+drag: relax   {k}+drag: extend   "
            f"Ctrl+{k}+drag: extend loop   Ctrl+LMB: insert loop   Ctrl+Shift+LMB: delete   "
            f"F: grid fill hole   B, B: bridge   D+drag: pen loop   "
            f"Ctrl+Z: undo   Esc / Enter / RMB: exit")


def _mark_dirty(_scene=None, _depsgraph=None):
    draw.DIRTY[0] = True
    draw.EXTERNAL[0] = True


def _tag_all(context):
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


class OM3D_OT_retopo_draw(TopoSymmetryMixin, TopoQuadMixin, TopoEditMixin, TopoToolsMixin, bpy.types.Operator):
    """Toggle Retopo Draw mode: draw retopology on the reference mesh"""
    bl_idname = "om3d.retopo_draw"
    bl_label = "Retopo Draw"
    bl_options = {"REGISTER"}  # undo steps are pushed per action, see _undo_push
    NAV_EVENTS = NAV_EVENTS
    MOVE_EVENTS = MOVE_EVENTS

    # ------------------------------------------------------------ setup
    def invoke(self, context, event):
        wm = context.window_manager
        if wm.om3d_topo_running:
            # Button clicked again: ask the running session to exit.
            wm.om3d_topo_running = False
            _tag_all(context)
            return {"FINISHED"}

        if context.area is None or context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "Run Retopo Draw from the 3D Viewport")
            return {"CANCELLED"}
        settings = context.scene.om3d_topo
        ref = settings.reference
        if ref is None or ref.type != "MESH":
            self.report({"WARNING"}, "Pick a reference mesh in the OM3D sidebar tab first")
            return {"CANCELLED"}

        self.area = context.area
        self.region = next(r for r in self.area.regions if r.type == "WINDOW")
        self.rv3d = self.region.data
        self.settings = settings
        self.ref = ref
        self.obj = self._get_or_create_retopo(context, ref)

        depsgraph = context.evaluated_depsgraph_get()
        self.bvh = BVHTree.FromObject(ref, depsgraph)
        self.ref_mw = ref.matrix_world.copy()
        self.ref_inv = self.ref_mw.inverted_safe()
        self.ref_nmat = self.ref_mw.to_3x3().inverted_safe().transposed()
        conform.tag(self.obj)
        mods.sync_ref_lock(context.scene)
        try:
            mods.sync(context, self.obj)
        except Exception:
            import traceback
            traceback.print_exc()

        self._bm = None
        self.state = "IDLE"
        self.tab_held = False
        self.mouse = self._event_mouse(event)
        self.proj = {}
        self.hint = ""
        self.show_brush = False
        self.ext = None
        self.relax_moved = False
        self._clear_hover()
        self._tools_init()
        self.drag_multi = None
        self._surface_guard_init()
        self._mirror_init()
        self.snap_hits = {}
        self.snap_pts = []
        self.snap_segs = []
        self.snap_verts = {}
        self.snap_edges = []
        self.ext_button = None

        # cached overlay geometry, rebuilt by the draw callback when dirty
        self.ov_tris = []
        self.ov_edges = []
        self.ov_dots = []
        self.ov_edge_len = 0.0
        self.ov_edge_faces = []
        self.ov_tri_faces = []
        draw.DIRTY[0] = True

        # one owner for Blender's Retopology overlay: overlay.py saves the user's value
        # and gives it back once neither Retopo Draw nor plain Edit Mode needs it
        ov_mod.claim_retopology(self.area.spaces.active)

        self._h_view = bpy.types.SpaceView3D.draw_handler_add(
            draw.draw_view, (self,), "WINDOW", "POST_VIEW")
        self._h_px = bpy.types.SpaceView3D.draw_handler_add(
            draw.draw_px, (self,), "WINDOW", "POST_PIXEL")
        if _mark_dirty not in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.append(_mark_dirty)

        wm.om3d_topo_running = True
        context.workspace.status_text_set(status_text(settings))
        wm.modal_handler_add(self)
        self._refresh(event)
        _tag_all(context)
        return {"RUNNING_MODAL"}

    def _get_or_create_retopo(self, context, ref):
        obj = context.active_object
        if obj is not None and obj.mode != "OBJECT" and (obj == ref or obj.type != "MESH"):
            bpy.ops.object.mode_set(mode="OBJECT")
        if obj is None or obj == ref or obj.type != "MESH":
            me = bpy.data.meshes.new(ref.name + "_retopo")
            obj = bpy.data.objects.new(me.name, me)
            coll = context.collection or context.scene.collection
            coll.objects.link(obj)
            for o in context.selected_objects:
                o.select_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
        if obj.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")
        return obj

    # ------------------------------------------------------------ helpers
    @property
    def bm(self):
        # Keep ONE BMesh wrapper for the session. Since Blender 5.2 each fresh
        # from_edit_mesh() call invalidates element refs from the previous one.
        # A native undo replaces the edit mesh, which makes the wrapper invalid,
        # so it gets re-acquired here.
        bm = self._bm
        if bm is None or not bm.is_valid:
            bm = self._bm = bmesh.from_edit_mesh(self.obj.data)
        return bm

    def _commit(self, topology=True):
        bmesh.update_edit_mesh(self.obj.data, loop_triangles=topology, destructive=topology)
        draw.DIRTY[0] = True

    def _undo_push(self, what):
        try:
            bpy.ops.ed.undo_push(message="Retopo Draw: " + what)
        except Exception:
            pass

    def _event_mouse(self, event):
        return Vector((event.mouse_x - self.region.x, event.mouse_y - self.region.y))

    def _ext_held(self, event):
        if self.settings.extend_key == "ALT":
            return event.alt
        return self.tab_held

    def _clear_hover(self):
        self.hover_vert = None
        self.hover_edge = None
        self.hover_face = None
        self.hover_chain = None
        self.ext_edge = None
        self.hover_ring = None
        self.fill_quad = None
        self.fill_hole = None
        self.delete_mode = False
        self.draw_vert = None
        self.draw_face = None
        self.draw_quad = None
        self.draw_lines = None     # list of world-space segment endpoints
        self.draw_lines_kind = None

    def _to_world(self, v):
        return self.obj.matrix_world @ v.co

    def _to_local(self, world):
        return self.obj.matrix_world.inverted_safe() @ world

    def _view_ray(self, co2d):
        origin = view3d_utils.region_2d_to_origin_3d(self.region, self.rv3d, co2d)
        direction = view3d_utils.region_2d_to_vector_3d(self.region, self.rv3d, co2d)
        return origin, direction

    def _ray_ref(self, co2d):
        """World-space hit on the reference surface under a screen point."""
        origin, direction = self._view_ray(co2d)
        o = self.ref_inv @ origin
        d = (self.ref_inv.to_3x3() @ direction).normalized()
        loc, _n, _i, _d = self.bvh.ray_cast(o, d)
        return None if loc is None else self.ref_mw @ loc

    def _snap(self, world):
        loc, _n, _i, _d = self.bvh.find_nearest(self.ref_inv @ world)
        return world if loc is None else self.ref_mw @ loc

    def _surface_normal(self, world):
        _loc, n, _i, _d = self.bvh.find_nearest(self.ref_inv @ world)
        if n is None:
            return Vector((0.0, 0.0, 1.0))
        return (self.ref_nmat @ n).normalized()

    def _screen(self, world):
        return view3d_utils.location_3d_to_region_2d(self.region, self.rv3d, world)

    def _visible(self, world, co2d):
        origin, _ = self._view_ray(co2d)
        if (world - origin).length < 1e-9:
            return True
        o = self.ref_inv @ origin
        target = self.ref_inv @ world
        d_local = target - o
        dist_local = d_local.length
        loc, _n, _i, hit_d = self.bvh.ray_cast(o, d_local.normalized(), dist_local)
        if loc is None:
            return True
        return hit_d >= dist_local * 0.995 - 1e-5

    def _project_all(self):
        proj = {}
        w, h = self.region.width, self.region.height
        mw = self.obj.matrix_world
        for v in self.bm.verts:
            if v.hide:
                continue
            co = view3d_utils.location_3d_to_region_2d(self.region, self.rv3d, mw @ v.co)
            if co is None or not (0 <= co.x <= w and 0 <= co.y <= h):
                continue
            proj[v] = co
        self.proj = proj

    def _visible_near(self, radius):
        out = {}
        for v, co in self.proj.items():
            if (co - self.mouse).length <= radius and self._visible(self._to_world(v), co):
                out[v] = co
        return out

    def rebuild_overlay(self):
        """Called from the draw callback when the mesh changed."""
        bm = self.bm
        mw = self.obj.matrix_world
        nmat = mw.to_3x3().inverted_safe().transposed()
        off = self.settings.surface_offset
        if off > 0.0:
            # show the mesh where the Surface Offset modifier puts it
            bm.normal_update()

            def P(v):
                return mw @ v.co + (nmat @ v.normal).normalized() * off
        else:
            def P(v):
                return mw @ v.co
        tris = []
        tri_faces = []
        for lt in bm.calc_loop_triangles():
            if lt[0].face.hide:
                continue
            tris.extend(P(l.vert) for l in lt)
            tri_faces.append(lt[0].face)
        edges = []
        edge_faces = []  # per edge: [(centre, normal)] of its faces, for backface culling
        fdata = {}
        for e in bm.edges:
            if e.hide:
                continue
            edges.append(P(e.verts[0]))
            edges.append(P(e.verts[1]))
            lst = []
            for f in e.link_faces:
                d = fdata.get(f)
                if d is None:
                    f.normal_update()
                    d = fdata[f] = (mw @ f.calc_center_median(), (nmat @ f.normal).normalized())
                lst.append(d)
            edge_faces.append(lst)
        self.ov_edge_faces = edge_faces
        for f in set(tri_faces) - set(fdata):
            f.normal_update()
            fdata[f] = (mw @ f.calc_center_median(), (nmat @ f.normal).normalized())
        self.ov_tri_faces = [fdata[f] for f in tri_faces]
        self.ov_tris = tris
        self.ov_edges = edges
        n = len(edges) // 2
        self.ov_edge_len = (sum((edges[i * 2] - edges[i * 2 + 1]).length for i in range(n)) / n) if n else 0.0
        self.ov_dots = [P(v) for v in bm.verts if not v.link_edges and not v.hide]
        self._mirror_ghost()

    # ------------------------------------------------------------ hover
    def _segments(self, verts, closed):
        pts = [self._to_world(v) for v in verts]
        segs = []
        n = len(pts)
        for i in range(n if closed else n - 1):
            segs += [pts[i], pts[(i + 1) % n]]
        return segs

    def _refresh(self, event):
        self._clear_hover()
        self._project_all()
        self._refresh_mark()
        s = self.settings
        ctrl, shift, ext = event.ctrl, event.shift, self._ext_held(event)
        k = ext_key_name(s)
        self.show_brush = False
        self.show_strip = False

        if ctrl and shift:
            self.delete_mode = True
            self.hint = "DELETE: click or drag across faces   click an edge: whole loop"
            self.hover_vert, self.hover_edge, self.hover_face = self._pick_any()
            if self.hover_edge is not None:
                info = self._loop_info(self.hover_edge)
                self.draw_lines = self._loop_lines(info)
                self.draw_lines_kind = "DELETE"
                if info[2]:
                    self.hint = (f"DELETE BORDER LOOP: click removes its {len(info[2])} faces   "
                                 f"LMB drag: delete faces   MMB drag: move loop")
                else:
                    self.hint = (f"DELETE EDGE LOOP ({len(info[0])} edges): click   "
                                 f"LMB drag: delete faces   MMB drag: move loop")
        elif ext:
            near = self._visible_near(s.fill_radius)
            edge = core.pick_open_edge(near, self.mouse, s.pick_radius)
            if edge is not None:
                self.ext_edge = edge
                if ctrl:
                    verts, closed = core.boundary_chain(edge)
                    self.hint = f"EXTEND LOOP: drag ({len(verts) - (0 if closed else 1)} edges)"
                else:
                    verts, closed = list(edge.verts), False
                    self.hint = "EXTEND: LMB drag the edge   MMB drag: the whole border loop"
                self.hover_chain = (verts, closed)
                self.draw_lines = self._segments(verts, closed)
                self.draw_lines_kind = "EXTEND"
            else:
                self.show_strip = True
                self.hint = (f"{k}+drag: draw a quad strip ({s.strip_width}px)   {k}+Wheel: strip width   "
                             f"hover a border edge to extend")
        elif ctrl:
            near = self._visible_near(s.fill_radius)
            edge, t = core.pick_edge(near, self.mouse, s.pick_radius * 2)
            self.hint = "INSERT LOOP: hover an edge, click to cut"
            if edge is not None:
                t = min(max(t, 0.05), 0.95)
                if abs(t - 0.5) < 0.12:  # snap to the middle near the center
                    t = 0.5
                    self.hint = "INSERT LOOP (centered): click to cut"
                edges, faces, closed = core.edge_ring(edge)
                A, B = core.ring_sides(edges, faces, edge.verts[0], edges.index(edge))
                self.hover_ring = (edges, faces, closed, A, B, t)
                mw = self.obj.matrix_world
                pts = [mw @ a.co.lerp(b.co, t) for a, b in zip(A, B)]
                if len(pts) == 1:
                    self.draw_vert = pts[0]
                segs = []
                n = len(pts)
                for i in range(len(faces)):
                    segs += [pts[i], pts[(i + 1) % n]]
                self.draw_lines = segs
                self.draw_lines_kind = "RING"
        elif shift:
            self.hint = "FILL: click   RELAX: drag"
            near = self._visible_near(s.fill_radius)
            hole = self._hole_under_mouse(near)
            if hole is None:
                quad = core.find_fill_quad(self.bm, near, self.mouse, s.fill_radius)
            else:
                # inside a bigger hole only quads that clearly belong win over the hole
                # fill: one using your dot, or one closing a notch (3 border edges + 1 new)
                quad = core.find_fill_quad(
                    self.bm, near, self.mouse, s.fill_radius,
                    accept=lambda vs, reuse: reuse >= 3 or any(not v.link_edges for v in vs))
            if quad is not None:
                self.fill_quad = quad
            elif hole is not None and len(hole) == 4:
                self.fill_quad = hole
            elif hole is not None:
                # never patch part of a bigger hole with a stray quad
                self.fill_hole = hole
                pts = [self._to_world(v) for v in hole]
                self.draw_lines = self._poly_segments(pts, True)
                self.draw_lines_kind = "HOVER"
                n = len(hole)
                self.hint = (f"FILL HOLE ({n} edges): click to grid fill   RELAX: drag" if n % 2 == 0
                             else f"HOLE HAS {n} EDGES: grid fill needs an even count   RELAX: drag")
            else:
                self.fill_quad = None
        else:
            self.hint = ""
            self.hover_vert, self.hover_edge, self.hover_face = self._pick_any()
            e = self.hover_edge
            if e is not None:
                self.draw_lines = [self._to_world(v) for v in e.verts]
                self.draw_lines_kind = "HOVER"
                if core.is_open_edge(e):
                    self.hint = f"BORDER EDGE: drag to move   F: grid fill   B: bridge   {k}+drag: extend"
                else:
                    self.hint = "EDGE: drag to move"
            elif self.hover_face is not None:
                self.hint = "FACE: drag to move"

        if self.hover_vert is not None:
            self.draw_vert = self._to_world(self.hover_vert)
        if self.hover_face is not None:
            self.draw_face = [self._to_world(v) for v in self.hover_face.verts]
        if self.fill_quad is not None:
            self.draw_quad = [self._to_world(v) for v in self.fill_quad]
        if self.d_held:
            self.hint = "PEN: drag on the surface to draw a loop or strip"
        elif self.bridge_a is not None and not self.hint:
            self.hint = "BRIDGE: press B over the second border (Esc cancels)"
        self.area.tag_redraw()

    # ------------------------------------------------------------ dots / verts
    def _place_dot(self):
        hit = self._ray_ref(self.mouse)
        if hit is None:
            return False
        v = self.bm.verts.new(self._to_local(hit))
        self._mirror_fix(v, near=True)
        self._commit()
        self._start_vert_drag(v, new=True)
        return True

    def _start_vert_drag(self, v, new=False):
        self.drag_vert = v
        self.drag_new = new
        co = self._screen(self._to_world(v))
        self.drag_offset = (co - self.mouse) if co is not None else Vector((0.0, 0.0))
        self.press_mouse = self.mouse.copy()
        # a fresh dot can go anywhere (it snaps onto the center line when near it);
        # an existing vert that sits on the center line slides along it
        self.drag_mirror = (False, 0.0) if new else self._mirror_state([v]).get(v, (False, 0.0))
        self._snap_begin([v])
        self.state = "DRAG_VERT"

    def _drag_vert_move(self):
        v = self.drag_vert
        if v is None or not v.is_valid:
            return
        sp = self.mouse + self.drag_offset
        self._snap_reset_marks()
        if self._snap_apply(v, sp):
            self._commit(topology=False)
        else:
            hit = self._ray_ref(sp)
            if hit is not None:
                v.co = self._to_local(hit)
                locked, side = self.drag_mirror
                self._mirror_fix(v, locked=locked, side=side, near=not locked)
                self._commit(topology=False)
        self.draw_vert = self._to_world(v)

    def _weld(self, v, exclude):
        """Merge v into a nearby visible vert. Returns the vert it merged into."""
        self._project_all()
        co = self.proj.get(v)
        if co is None:
            return None
        cands = {u: c for u, c in self.proj.items()
                 if u not in exclude and u is not v
                 and (c - co).length <= self.settings.weld_radius
                 and self._visible(self._to_world(u), c)}
        target = core.pick_vert(cands, co, self.settings.weld_radius)
        if target is not None:
            bmesh.ops.pointmerge(self.bm, verts=[v, target], merge_co=target.co.copy())
        return target

    def _drag_vert_end(self):
        v = self.drag_vert
        moved = (self.mouse - self.press_mouse).length > DRAG_THRESHOLD
        if v is not None and v.is_valid and moved:
            self._snap_commit()
        else:
            self.snap_hits = {}
            self._snap_reset_marks()
        self._commit()
        self.state = "IDLE"
        self.drag_vert = None
        if self.drag_new:
            self._undo_push("Add dot")
        elif moved:
            self._undo_push("Move vert")

    # ------------------------------------------------------------ fill / delete
    def _fill(self, verts):
        if not verts or not all(v.is_valid for v in verts):
            return
        try:
            f = self.bm.faces.new(verts)
        except ValueError:
            return
        center = self.obj.matrix_world @ f.calc_center_median()
        core.orient_face(f, self.obj.matrix_world, self._surface_normal(center))
        self._commit()
        self._undo_push("Fill quad")

    def _delete(self, push=True):
        bm = self.bm
        if self.hover_vert is not None and self.hover_vert.is_valid:
            bmesh.ops.delete(bm, geom=[self.hover_vert], context="VERTS")
        elif self.hover_edge is not None and self.hover_edge.is_valid:
            self._delete_edge(self.hover_edge)
        elif self.hover_face is not None and self.hover_face.is_valid:
            core.delete_face_clean(bm, self.hover_face)
        else:
            return False
        self._commit()
        if push:
            self._undo_push("Delete")
        return True

    # ------------------------------------------------------------ extend
    def _extend_start(self):
        if self.hover_chain is None:
            return
        verts, closed = self.hover_chain
        if not all(v.is_valid for v in verts) or len(verts) < 2:
            return
        res = core.extrude_chain(self.bm, verts, closed)
        if res is None:
            return
        new, faces = res
        self.ext = {
            "base": verts, "new": new, "faces": faces, "closed": closed,
            "screen": [self._screen(self._to_world(v)) for v in verts],
        }
        self.press_mouse = self.mouse.copy()
        st = self._mirror_state(verts)
        self.ext["mirror"] = [st.get(v, (False, 0.0)) for v in verts]
        self._snap_begin(set(verts) | set(new))
        self._commit()
        self.state = "EXTEND"

    def _extend_move(self):
        ext = self.ext
        delta = self.mouse - self.press_mouse
        self._snap_reset_marks()
        used = set()
        for n, s, (locked, side) in zip(ext["new"], ext["screen"], ext["mirror"]):
            if s is None or not n.is_valid:
                continue
            if self._snap_apply(n, s + delta, used):
                hit = self.snap_hits[n]
                if hit[0] == "vert":
                    used.add(hit[1])
                continue
            hit = self._ray_ref(s + delta)
            if hit is not None:
                n.co = self._to_local(hit)
                self._mirror_fix(n, locked=locked, side=side, near=not locked)
        self._commit(topology=False)
        self.draw_lines = self._segments([n for n in ext["new"] if n.is_valid], ext["closed"])
        self.draw_lines_kind = "EXTEND"

    def _extend_end(self):
        ext, self.ext = self.ext, None
        bm = self.bm
        self.state = "IDLE"
        if (self.mouse - self.press_mouse).length <= DRAG_THRESHOLD:
            self.snap_hits = {}
            self._snap_reset_marks()
            for n in ext["new"]:
                if n.is_valid:
                    bm.verts.remove(n)
            self._commit()
            return
        mw = self.obj.matrix_world
        for f in ext["faces"]:
            if f.is_valid:
                core.orient_face(f, mw, self._surface_normal(mw @ f.calc_center_median()))
        # new verts that snapped onto existing verts or edges while dragging merge into them
        self._snap_commit()
        self._commit()
        self._undo_push("Extend loop" if len(ext["base"]) > 2 else "Extend")

    # ------------------------------------------------------------ edge loop insert
    def _insert_loop(self):
        if self.hover_ring is None:
            return
        edges, faces, closed, A, B, t = self.hover_ring
        if not all(e.is_valid for e in edges) or not all(f.is_valid for f in faces):
            return
        seam = [self._mirror_on() and self._on_plane(a) and self._on_plane(b) for a, b in zip(A, B)]
        new = core.insert_loop(self.bm, edges, faces, A, B, t)
        mw = self.obj.matrix_world
        inv = mw.inverted_safe()
        for v, on_seam in zip(new, seam):
            v.co = inv @ self._snap(mw @ v.co)
            if on_seam:
                self._mirror_fix(v, locked=True)
        self._commit()
        self._undo_push("Insert loop")

    # ------------------------------------------------------------ relax
    def _relax_step(self):
        s = self.settings
        r = float(s.relax_radius)
        self._project_all()
        weights = {}
        for v, co in self.proj.items():
            d = (co - self.mouse).length
            if d > r or not v.link_edges:
                continue
            if not self._visible(self._to_world(v), co):
                continue
            f = 1.0 - d / r
            weights[v] = f * f * (3.0 - 2.0 * f) * s.relax_strength
        new = core.relax_positions(weights, s.relax_border)
        if not new:
            return
        mw = self.obj.matrix_world
        inv = mw.inverted_safe()
        st = self._mirror_state(new.keys())
        for v, co in new.items():
            v.co = inv @ self._snap(mw @ co)
            locked, side = st.get(v, (False, 0.0))
            self._mirror_fix(v, locked=locked, side=side, near=False)
        self._commit(topology=False)
        self.relax_moved = True

    # ------------------------------------------------------------ modal
    def _in_viewport(self, event):
        x, y = event.mouse_x, event.mouse_y
        for r in self.area.regions:
            if r.type in {"UI", "TOOLS", "HEADER", "TOOL_HEADER", "ASSET_SHELF", "ASSET_SHELF_HEADER"}:
                if r.x <= x < r.x + r.width and r.y <= y < r.y + r.height:
                    return False
        r = self.region
        return r.x <= x < r.x + r.width and r.y <= y < r.y + r.height

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                self.report({"ERROR"}, "Retopo Draw hit an error and stopped. See the system console.")
            except Exception:
                pass
            return self._finish(context)

    def _modal(self, context, event):
        wm = context.window_manager
        try:
            alive = (self.obj.mode == "EDIT" and wm.om3d_topo_running
                     and self.area.type == "VIEW_3D"
                     and context.view_layer.objects.active == self.obj)
        except ReferenceError:
            alive = False
        if not alive:
            return self._finish(context)
        if self.state in TOOL_STATES:
            return self._modal_tool(event)
        if self.state == "IDLE":
            self._check_foreign(context)

        if (event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE"} and self.state == "IDLE"
                and self._ext_held(event) and self._in_viewport(event)):
            s = self.settings
            f = 1.1 if event.type == "WHEELUPMOUSE" else 1 / 1.1
            s.strip_width = max(8, min(400, round(s.strip_width * f) + (1 if f > 1 else -1)))
            self.mouse = self._event_mouse(event)
            self._refresh(event)
            return {"RUNNING_MODAL"}
        if event.type == "MIDDLEMOUSE":
            # Ctrl+Shift+MMB drag on an edge moves its edge loop (Maya)
            if (event.value == "PRESS" and self.state == "IDLE" and event.ctrl and event.shift
                    and self._in_viewport(event)):
                self.mouse = self._event_mouse(event)
                self._refresh(event)
                if self._cs_mmb_press():
                    self.area.tag_redraw()
                    return {"RUNNING_MODAL"}
            elif (event.value == "RELEASE" and self.state == "DRAG_MULTI"
                    and getattr(self, "multi_button", None) == "MIDDLEMOUSE"):
                self.mouse = self._event_mouse(event)
                self._multi_end()
                self._refresh(event)
                return {"RUNNING_MODAL"}
            # extend key + MMB drag on a border edge extends its whole border loop (Maya)
            if (event.value == "PRESS" and self.state == "IDLE" and self._ext_held(event)
                    and self._in_viewport(event)):
                self.mouse = self._event_mouse(event)
                self._refresh(event)
                e = self.ext_edge
                if e is not None and e.is_valid:
                    self.hover_chain = core.boundary_chain(e)
                    self.ext_button = "MIDDLEMOUSE"
                    self._extend_start()
                    if self.state == "EXTEND":
                        self.area.tag_redraw()
                        return {"RUNNING_MODAL"}
                    self.ext_button = None
            elif event.value == "RELEASE" and self.state == "EXTEND" and self.ext_button == "MIDDLEMOUSE":
                self.mouse = self._event_mouse(event)
                self.ext_button = None
                self._extend_end()
                self._refresh(event)
                return {"RUNNING_MODAL"}
        if event.type in NAV_EVENTS or event.type.startswith("NUMPAD_"):
            return {"PASS_THROUGH"}
        idle = self.state == "IDLE"
        if idle and not self._in_viewport(event):
            self.show_brush = False
            return {"PASS_THROUGH"}

        self.mouse = self._event_mouse(event)

        if event.type == "TAB" and self.settings.extend_key == "TAB":
            if event.value == "PRESS":
                self.tab_held = True
            elif event.value == "RELEASE":
                self.tab_held = False
            if idle:
                self._refresh(event)
            return {"RUNNING_MODAL"}

        if event.type in MOD_KEYS:
            if idle:
                self._refresh(event)
            return {"PASS_THROUGH"}

        if event.type == "D" and idle:
            if event.value == "PRESS":
                self.d_held = True
            elif event.value == "RELEASE":
                self.d_held = False
            self._refresh(event)
            return {"RUNNING_MODAL"}

        if event.type in MOVE_EVENTS:
            if self.state == "DRAG_VERT":
                self._drag_vert_move()
            elif self.state == "DRAG_MULTI":
                self._multi_move()
            elif self.state in QUAD_STATES:
                self._quad_move()
            elif self.state == "EXTEND":
                self._extend_move()
            elif self.state == "SHIFT_PRESS":
                if (self.mouse - self.press_mouse).length > DRAG_THRESHOLD:
                    self.state = "RELAX"
                    self.fill_quad = self.draw_quad = None
                    self.press_hole = None
                    self._relax_step()
            elif self.state == "RELAX":
                self._relax_step()
            else:
                self._refresh(event)
                return {"PASS_THROUGH"}
            self.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE":
            if event.value == "PRESS" and idle:
                ext = self._ext_held(event)
                if self.d_held:
                    self._pen_begin()
                    return {"RUNNING_MODAL"}
                elif event.ctrl and event.shift:
                    self._cs_press()
                elif ext:
                    if self.hover_chain is not None:
                        self.ext_button = "LEFTMOUSE"
                        self._extend_start()
                    else:
                        self._strip_begin()
                elif event.ctrl:
                    self._insert_loop()
                elif event.shift:
                    self.press_mouse = self.mouse.copy()
                    self.press_quad = self.fill_quad
                    self.press_hole = self.fill_hole
                    self.relax_moved = False
                    self.state = "SHIFT_PRESS"
                elif self.hover_vert is not None and self.hover_vert.is_valid:
                    self._start_vert_drag(self.hover_vert)
                elif self.hover_edge is not None and self.hover_edge.is_valid:
                    self._start_multi_drag(self.hover_edge.verts, "edge")
                elif self.hover_face is not None and self.hover_face.is_valid:
                    self._start_multi_drag(self.hover_face.verts, "face")
                else:
                    self._place_dot()
            elif event.value == "RELEASE":
                if self.state == "DRAG_VERT":
                    self._drag_vert_end()
                elif self.state == "DRAG_MULTI":
                    self._multi_end()
                elif self.state in QUAD_STATES:
                    self._quad_release()
                elif self.state == "EXTEND":
                    self._extend_end()
                elif self.state == "SHIFT_PRESS":
                    self.state = "IDLE"
                    hole = getattr(self, "press_hole", None)
                    self.press_hole = None
                    if hole is not None:
                        if len(hole) % 2 == 0 and all(v.is_valid for v in hole):
                            self._grid_start(hole)
                            return {"RUNNING_MODAL"}
                    else:
                        self._fill(self.press_quad)
                elif self.state == "RELAX":
                    self.state = "IDLE"
                    self._commit()
                    if self.relax_moved:
                        self._undo_push("Relax")
            if self.state == "IDLE":
                self._refresh(event)
            return {"RUNNING_MODAL"}

        if not idle:
            return {"RUNNING_MODAL"}  # swallow keys mid-drag

        if event.type == "RIGHTMOUSE" and event.value == "PRESS" and event.ctrl and event.shift:
            try:
                bpy.ops.wm.call_panel(name="OM3D_PT_retopo_options", keep_open=True)
            except Exception:
                pass
            return {"RUNNING_MODAL"}

        if event.value == "PRESS" and not (event.ctrl or event.shift or event.alt):
            if event.type == "F" and self._grid_start():
                self.area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "B" and self._bridge_key():
                self.area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "ESC" and self.bridge_a is not None:
                self.bridge_a = None
                self.mark_lines = None
                self.hint = ""
                self.area.tag_redraw()
                return {"RUNNING_MODAL"}

        if event.value == "PRESS" and (
                event.type in {"ESC", "RET", "NUMPAD_ENTER"} or event.type == "RIGHTMOUSE"):
            return self._finish(context)

        # Everything else (Ctrl+Z, K, Ctrl+R, G, E, X...) goes to Blender as usual.
        return {"PASS_THROUGH"}

    def _finish(self, context):
        for attr in ("_h_view", "_h_px"):
            h = getattr(self, attr, None)
            if h is not None:
                try:
                    bpy.types.SpaceView3D.draw_handler_remove(h, "WINDOW")
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            bpy.app.handlers.depsgraph_update_post.remove(_mark_dirty)
        except ValueError:
            pass
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass
        try:
            context.window_manager.om3d_topo_running = False
        except Exception:
            pass
        try:
            if self.obj.mode == "EDIT":
                self._commit()
        except Exception:
            pass
        self._bm = None
        _tag_all(context)
        return {"FINISHED"}


class OM3D_OT_clear_dots(bpy.types.Operator):
    """Delete every loose dot (vert with no edges) on the retopo mesh"""
    bl_idname = "om3d.clear_dots"
    bl_label = "Clear Dots"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.type == "MESH"
                and obj != context.scene.om3d_topo.reference)

    def execute(self, context):
        obj = context.active_object
        if obj.mode == "EDIT":
            bm = bmesh.from_edit_mesh(obj.data)
            n = core.remove_loose_verts(bm)
            bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
        else:
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            n = core.remove_loose_verts(bm)
            bm.to_mesh(obj.data)
            bm.free()
            obj.data.update()
        draw.DIRTY[0] = True
        self.report({"INFO"}, f"Cleared {n} dot{'s' if n != 1 else ''}")
        return {"FINISHED"}
