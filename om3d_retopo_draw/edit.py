# SPDX-License-Identifier: GPL-3.0-or-later
"""Edge / face picking and dragging, edge delete, and keeping verts on the surface."""
import bmesh

from . import core
from . import draw

DRAG_THRESHOLD = 4.0  # px


class TopoEditMixin:

    # ------------------------------------------------------------ picking
    def _near(self, radius):
        return {v: co for v, co in self.proj.items() if (co - self.mouse).length <= radius}

    def _point_visible(self, world, slack):
        """Is a point on an edge/face visible? Flat edges and faces dip under a curved
        surface, so the surface along the view ray may sit up to `slack` in front of it."""
        sc = self._screen(world)
        if sc is None:
            return False
        origin, _ = self._view_ray(sc)
        hit = self._ray_ref(sc)
        if hit is None:
            return True
        return (hit - origin).length >= (world - origin).length - slack

    def _pick_any(self):
        """Visible vert under the mouse, else edge, else face. Returns (vert, edge, face)."""
        s = self.settings
        near = self._near(s.pick_radius * 2)
        vis = {v: co for v, co in near.items() if self._visible(self._to_world(v), co)}
        v = core.pick_vert(vis, self.mouse, s.pick_radius)
        wide = self._near(s.fill_radius)
        e, t = core.pick_edge(wide, self.mouse, s.pick_radius)
        if e is not None:
            a, b = self._to_world(e.verts[0]), self._to_world(e.verts[1])
            if not self._point_visible((a + b) * 0.5, (a - b).length * 0.5):
                e = None
        # the middle half of a short edge still grabs the edge, not its end vert
        if e is not None and (v is None or 0.25 <= t <= 0.75):
            return None, e, None
        if v is not None:
            return v, None, None
        f = core.pick_face(wide, self.mouse)
        if f is not None:
            mw = self.obj.matrix_world
            slack = max(((mw @ x.verts[0].co) - (mw @ x.verts[1].co)).length for x in f.edges) * 0.5
            if self._point_visible(mw @ f.calc_center_median(), slack):
                return None, None, f
        return None, None, None

    # ------------------------------------------------------------ edge / face drag
    def _start_multi_drag(self, verts, kind):
        self.drag_multi = list(verts)
        self.drag_kind = kind
        self.drag_screen = [self._screen(self._to_world(v)) for v in self.drag_multi]
        self.press_mouse = self.mouse.copy()
        self.multi_mirror = self._mirror_state(self.drag_multi)
        if kind == "edge":
            self._snap_begin(self.drag_multi)  # an edge's ends snap like single verts
        else:
            self.snap_verts, self.snap_edges, self.snap_hits = {}, [], {}
        self.state = "DRAG_MULTI"

    def _multi_move(self):
        delta = self.mouse - self.press_mouse
        self._snap_reset_marks()
        used = set()
        for v, s0 in zip(self.drag_multi, self.drag_screen):
            if s0 is None or not v.is_valid:
                continue
            if self._snap_apply(v, s0 + delta, used):
                hit = self.snap_hits[v]
                if hit[0] == "vert":
                    used.add(hit[1])
                continue
            hit = self._ray_ref(s0 + delta)
            if hit is not None:
                v.co = self._to_local(hit)
                locked, side = self.multi_mirror.get(v, (False, 0.0))
                self._mirror_fix(v, locked=locked, side=side, near=not locked)
        self._commit(topology=False)
        pts = [self._to_world(v) for v in self.drag_multi if v.is_valid]
        if self.drag_kind == "edge":
            self.draw_lines, self.draw_lines_kind = pts, "HOVER"
            self.draw_face = None
        elif self.drag_kind == "edge loop":
            segs = []
            for x in getattr(self, "drag_loop_edges", []):
                if x.is_valid:
                    segs += [self._to_world(x.verts[0]), self._to_world(x.verts[1])]
            self.draw_lines, self.draw_lines_kind = segs, "HOVER"
            self.draw_face = None
        else:
            self.draw_face = pts
            self.draw_lines = None

    def _multi_end(self):
        verts, kind = self.drag_multi, self.drag_kind
        moved = (self.mouse - self.press_mouse).length > DRAG_THRESHOLD
        if moved:
            self._snap_commit()
        else:
            self.snap_hits = {}
            self._snap_reset_marks()
        self._commit()
        self.state = "IDLE"
        self.drag_multi = None
        self.multi_button = None
        if moved:
            self._undo_push("Move " + kind)

    # ------------------------------------------------------------ delete edge
    def _delete_edge(self, e):
        bm = self.bm
        if len(e.link_faces) == 2:
            # interior edge: dissolve it so the two faces merge (Quad Draw style);
            # verts left on a straight run of two edges dissolve too
            bmesh.ops.dissolve_edges(bm, edges=[e], use_verts=True, use_face_split=False)
        elif e.link_faces:
            # border edge: remove its face and anything left dangling
            core.delete_face_clean(bm, e.link_faces[0])
        else:
            verts = list(e.verts)
            bm.edges.remove(e)
            for v in verts:
                if v.is_valid and not v.link_edges:
                    bm.verts.remove(v)

    # ------------------------------------------------------------ keep verts on the surface
    def _surface_key(self, v):
        return tuple(round(c, 5) for c in v.co)

    def _surface_dist(self, v):
        _loc, _n, _i, d = self.bvh.find_nearest(self.ref_inv @ (self.obj.matrix_world @ v.co))
        return d

    def _surface_guard_init(self):
        dims = self.ref.dimensions
        scale = max(max(self.ref.scale), 1e-9)
        self._surf_eps = max(max(dims) / scale * 1e-4, 1e-6)
        draw.EXTERNAL[0] = False
        # verts already off the surface when the mode starts are left alone
        self._exempt = set()
        for v in self.bm.verts:
            d = self._surface_dist(v)
            if d is None or d > self._surf_eps:
                self._exempt.add(self._surface_key(v))

    def _other_modal_running(self, context):
        try:
            ops = context.window.modal_operators
        except Exception:
            return False
        return any(op.bl_idname not in {"OM3D_OT_retopo_draw", "om3d.retopo_draw"} for op in ops)

    def _check_foreign(self, context):
        """Snap verts that another tool (knife, loop cut, grab...) left off the surface."""
        # Our own actions keep every vert on the surface, so a pass after them finds
        # nothing to move; only verts made or moved by other tools get snapped.
        if not draw.EXTERNAL[0]:
            return
        if not self.settings.snap_foreign:
            draw.EXTERNAL[0] = False
            return
        if self._other_modal_running(context):
            return  # wait until that tool is finished
        draw.EXTERNAL[0] = False
        mw = self.obj.matrix_world
        inv = mw.inverted_safe()
        moved = 0
        for v in self.bm.verts:
            if v.hide:
                continue
            loc, _n, _i, d = self.bvh.find_nearest(self.ref_inv @ (mw @ v.co))
            if loc is None or d <= self._surf_eps:
                continue
            if self._surface_key(v) in self._exempt:
                continue
            on_seam = self._mirror_on() and abs(self._plane_coord(mw @ v.co)) <= self._plane_eps
            v.co = inv @ (self.ref_mw @ loc)
            if on_seam:
                self._mirror_fix(v, locked=True)
            moved += 1
        if moved:
            self._commit(topology=False)
