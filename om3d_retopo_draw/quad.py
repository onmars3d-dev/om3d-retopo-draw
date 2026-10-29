# SPDX-License-Identifier: GPL-3.0-or-later
"""Maya Quad Draw extras: quad strips, edge loop delete / drag, drag-delete across faces."""
import bmesh
from mathutils import Vector

from . import core
from . import fills

DRAG_THRESHOLD = 4.0  # px
QUAD_STATES = {"CS_EDGE", "DEL_DRAG", "STRIP_STROKE"}


class TopoQuadMixin:

    # ------------------------------------------------------------ edge loops
    def _loop_info(self, e):
        """(edges, border, faces_to_remove) for the edge loop through e."""
        if len(e.link_faces) == 2:
            edges, _closed = core.edge_loop(e)
            return edges, False, []
        verts, closed = core.boundary_chain(e)
        n = len(verts)
        edges = []
        for i in range(n if closed else n - 1):
            x = self.bm.edges.get((verts[i], verts[(i + 1) % n]))
            if x is not None:
                edges.append(x)
        faces = []
        for x in edges:
            for f in x.link_faces:
                if f not in faces:
                    faces.append(f)
        return edges, True, faces

    def _loop_lines(self, info):
        """Just the edge loop itself (a border loop's faces aren't outlined)."""
        segs = []
        for x in info[0]:
            if x.is_valid:
                segs += [self._to_world(x.verts[0]), self._to_world(x.verts[1])]
        return segs

    def _delete_loop(self, info):
        edges, border, faces = info
        bm = self.bm
        if not border:
            edges = [x for x in edges if x.is_valid]
            if edges:
                bmesh.ops.dissolve_edges(bm, edges=edges, use_verts=True, use_face_split=False)
        elif faces:
            for f in faces:
                if f.is_valid:
                    core.delete_face_clean(bm, f)
        else:  # wire chain
            verts = {v for x in edges for v in x.verts}
            for x in edges:
                if x.is_valid:
                    bm.edges.remove(x)
            for v in verts:
                if v.is_valid and not v.link_edges:
                    bm.verts.remove(v)

    # ------------------------------------------------------------ Ctrl+Shift press
    def _cs_press(self):
        self.cs_press = self.mouse.copy()
        if self.hover_edge is not None and self.hover_edge.is_valid:
            self.cs_loop = self._loop_info(self.hover_edge)
            self.state = "CS_EDGE"
        else:
            self.del_count = 1 if self._delete(push=False) else 0
            self.state = "DEL_DRAG"
            self.hint = "DELETE: drag across faces"

    def _cs_mmb_press(self):
        """Ctrl+Shift+MMB on an edge: drag its whole edge loop."""
        e = self.hover_edge
        if e is None or not e.is_valid:
            return False
        edges = [x for x in self._loop_info(e)[0] if x.is_valid]
        verts = []
        for x in edges:
            for v in x.verts:
                if v not in verts:
                    verts.append(v)
        self.drag_loop_edges = edges
        self._start_multi_drag(verts, "edge loop")
        self.multi_button = "MIDDLEMOUSE"
        self.hint = "MOVE EDGE LOOP"
        return True

    # ------------------------------------------------------------ quad strips
    def _strip_begin(self):
        self.stroke = [self.mouse.copy()]
        self.state = "STRIP_STROKE"
        self.hint = f"QUAD STRIP: drag, release to build ({self.settings.strip_width}px wide)"

    def _strip_finish(self):
        pts, self.stroke = self.stroke, None
        w = float(self.settings.strip_width)
        length = fills.polyline_length(pts, False) if pts else 0.0
        if length < w * 0.5:
            return
        seg = max(1, round(length / w))
        centre = fills.resample(pts, seg + 1, False)
        rows = ([], [])
        for i, c in enumerate(centre):
            d = centre[min(i + 1, seg)] - centre[max(i - 1, 0)]
            if d.length < 1e-6:
                d = Vector((1.0, 0.0))
            d.normalize()
            side = Vector((-d.y, d.x)) * (w * 0.5)
            rows[0].append(self._ray_ref(c + side))
            rows[1].append(self._ray_ref(c - side))
        keep = [i for i in range(seg + 1) if rows[0][i] is not None and rows[1][i] is not None]
        if len(keep) < 2:
            return
        bm = self.bm
        inv = self.obj.matrix_world.inverted_safe()
        cols = {i: (bm.verts.new(inv @ rows[0][i]), bm.verts.new(inv @ rows[1][i])) for i in keep}
        for i in keep:
            if i + 1 in cols:
                a, b = cols[i], cols[i + 1]
                self._new_face((a[0], b[0], b[1], a[1]))
        new = [v for pair in cols.values() for v in pair]
        for v in new:
            if v.is_valid and not v.link_edges:
                bm.verts.remove(v)
        self._mirror_fix_new(new)
        # the strip's end edges weld onto existing verts, so a strip can start or end on the mesh
        new_set = set(new)
        for i in (keep[0], keep[-1]):
            for v in cols[i]:
                if v.is_valid:
                    t = self._weld(v, exclude=new_set)
                    if t is not None:
                        new_set.add(t)
        self._commit()
        self._undo_push("Quad strip")

    # ------------------------------------------------------------ modal hooks
    def _quad_move(self):
        st = self.state
        if st == "STRIP_STROKE":
            if (self.mouse - self.stroke[-1]).length > 3.0:
                self.stroke.append(self.mouse.copy())
        elif st == "CS_EDGE":
            if (self.mouse - self.cs_press).length > DRAG_THRESHOLD:
                # dragging off the edge deletes faces as you go instead of the loop
                self.del_count = 0
                self.state = "DEL_DRAG"
                self.hint = "DELETE: drag across faces"
        elif st == "DEL_DRAG":
            # any face under the cursor goes, even right next to an edge or vert
            self._project_all()
            f = core.pick_face(self._near(self.settings.fill_radius), self.mouse)
            if f is not None:
                mw = self.obj.matrix_world
                slack = max(((mw @ x.verts[0].co) - (mw @ x.verts[1].co)).length for x in f.edges) * 0.5
                if not self._point_visible(mw @ f.calc_center_median(), slack):
                    f = None
            self.draw_face = [self._to_world(v) for v in f.verts] if f is not None else None
            if f is not None:
                core.delete_face_clean(self.bm, f)
                self._commit()
                self.del_count += 1
                self.draw_face = None

    def _quad_release(self):
        st = self.state
        self.state = "IDLE"
        if st == "STRIP_STROKE":
            self._strip_finish()
        elif st == "CS_EDGE":
            self._delete_loop(self.cs_loop)
            self._commit()
            self._undo_push("Delete loop")
        elif st == "DEL_DRAG":
            if self.del_count:
                self._undo_push("Delete")
