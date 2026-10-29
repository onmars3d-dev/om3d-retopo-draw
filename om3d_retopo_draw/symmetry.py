# SPDX-License-Identifier: GPL-3.0-or-later
"""Mirror constraint and live snapping while dragging."""
import bmesh
from mathutils import Vector

from . import core

AXES = {"X": 0, "Y": 1, "Z": 2}


class TopoSymmetryMixin:

    # ------------------------------------------------------------ mirror plane
    # The plane is the reference object's local X (Y, Z) = 0 plane, the same one
    # the Mirror modifier uses (its mirror object is the reference).
    def _mirror_init(self):
        bb = [Vector(c) for c in self.ref.bound_box]
        diag = (bb[6] - bb[0]).length or 1.0
        self._plane_eps = diag * 1e-5
        self.mirror_ghost_tris = []
        self.mirror_ghost_edges = []

    def _mirror_on(self):
        return self.settings.mirror

    def _axis(self):
        return AXES[self.settings.mirror_axis]

    def _plane_coord(self, world):
        return (self.ref_inv @ world)[self._axis()]

    def _to_plane(self, world):
        w = self.ref_inv @ world
        w[self._axis()] = 0.0
        return self.ref_mw @ w

    def _reflect(self, world):
        w = self.ref_inv @ world
        w[self._axis()] = -w[self._axis()]
        return self.ref_mw @ w

    def _on_plane(self, v):
        return abs(self._plane_coord(self._to_world(v))) <= self._plane_eps

    def _near_plane_px(self, world):
        # compare against the center line as it runs over the surface, not the plane
        # point straight across (in an angled view that one can sit far off on screen)
        if abs(self._plane_coord(world)) <= self._plane_eps:
            return True
        a, b = self._screen(world), self._screen(self._snap_in_plane(world))
        return a is not None and b is not None and (a - b).length <= self.settings.weld_radius

    def _snap_in_plane(self, world):
        """Surface point on the mirror plane near `world` (a point on the plane)."""
        p = self._to_plane(world)
        a = self._axis()
        e = Vector((0.0, 0.0, 0.0))
        e[a] = 1.0
        nw = (self.ref_nmat @ e).normalized()
        if self.rv3d.is_perspective:
            eye = self.rv3d.view_matrix.inverted().translation
            d = (p - eye).normalized()
        else:
            d = (self.rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))).normalized()
        dp = d - nw * d.dot(nw)
        if dp.length < 1e-4:
            dp = -(self._surface_normal(p) - nw * self._surface_normal(p).dot(nw))
        if dp.length < 1e-6:
            return p
        dp.normalize()
        bb = [Vector(c) for c in self.ref.bound_box]
        reach = (bb[6] - bb[0]).length * max(self.ref.scale) * 2.0 + 1.0
        origin = self.ref_inv @ (p - dp * reach)
        dl = (self.ref_inv.to_3x3() @ dp).normalized()
        best, best_d = None, None
        o = origin
        for _ in range(8):  # walk the hits along the in-plane ray, keep the one nearest p
            loc, _n, _i, dist = self.bvh.ray_cast(o, dl)
            if loc is None:
                break
            wl = self.ref_mw @ loc
            dd = (wl - p).length
            if best_d is None or dd < best_d:
                best, best_d = wl, dd
            o = loc + dl * 1e-5
        if best is None:
            return p
        return self._to_plane(best)

    def _mirror_fix(self, v, locked=False, side=0.0, near=True):
        """Keep v on its side of the mirror plane, or on the plane itself.

        locked: v started on the plane, so it slides along it.
        side:   sign of v's side when the drag started; crossing over clamps to the plane.
        near:   dropping within the weld radius of the plane snaps onto it.
        """
        if not self._mirror_on() or not v.is_valid:
            return False
        w = self._to_world(v)
        c = self._plane_coord(w)
        onto = locked or (side and c * side < 0.0) or (near and self._near_plane_px(w))
        if onto:
            v.co = self._to_local(self._snap_in_plane(w))
        return onto

    def _mirror_state(self, verts):
        """Per-vert (locked, side) at the start of a drag."""
        out = {}
        if not self._mirror_on():
            return out
        for v in verts:
            c = self._plane_coord(self._to_world(v))
            locked = abs(c) <= self._plane_eps
            out[v] = (locked, 0.0 if locked else (1.0 if c > 0 else -1.0))
        return out

    def _mirror_fix_new(self, verts):
        """New geometry (strips, pens, fills): snap verts close to the plane onto it."""
        if not self._mirror_on():
            return
        for v in verts:
            if v.is_valid:
                self._mirror_fix(v, near=True)

    def _mirror_ghost(self):
        """Reflected copy of the overlay, drawn faintly on the other side."""
        if not self._mirror_on():
            self.mirror_ghost_tris = []
            self.mirror_ghost_edges = []
            return
        self.mirror_ghost_tris = [self._reflect(p) for p in self.ov_tris]
        self.mirror_ghost_edges = [self._reflect(p) for p in self.ov_edges]

    # ------------------------------------------------------------ live snapping
    def _snap_begin(self, moving):
        """Cache what a dragged vert can snap to: visible verts, and edges not touching it."""
        moving = set(moving)
        self._project_all()
        verts = {u: c for u, c in self.proj.items()
                 if u not in moving and self._visible(self._to_world(u), c)}
        edges = []
        if self.settings.snap_edges:
            seen = set()
            for u in verts:
                for e in u.link_edges:
                    if e in seen:
                        continue
                    seen.add(e)
                    a, b = e.verts
                    if a in moving or b in moving:
                        continue
                    pa, pb = self.proj.get(a), self.proj.get(b)
                    if pa is not None and pb is not None:
                        edges.append((e, pa, pb))
        self.snap_verts = verts
        self.snap_edges = edges
        self.snap_hits = {}
        self.snap_pts = []
        self.snap_segs = []

    def _snap_find(self, sp, used=()):
        """Snap target for screen point sp: ('vert', u) or ('edge', e, t), or None."""
        r = self.settings.weld_radius
        best, best_d = None, r
        for u, c in self.snap_verts.items():
            if u in used or not u.is_valid:
                continue
            d = (c - sp).length
            if d <= best_d:
                best, best_d = ("vert", u), d
        if best is not None:
            return best
        best_d = r
        for e, pa, pb in self.snap_edges:
            if not e.is_valid:
                continue
            d, t = core.segment_param(sp, pa, pb)
            if d <= best_d and 0.08 < t < 0.92:
                best, best_d = ("edge", e, t), d
        return best

    def _snap_apply(self, v, sp, used=()):
        """Move v onto its snap target if one is in range. Returns True when snapped."""
        hit = self._snap_find(sp, used)
        self.snap_hits.pop(v, None)
        if hit is None:
            return False
        if hit[0] == "vert":
            v.co = hit[1].co.copy()
            self.snap_pts.append(self._to_world(hit[1]))
        else:
            e, t = hit[1], hit[2]
            a, b = e.verts
            v.co = a.co.lerp(b.co, t)
            self.snap_segs += [self._to_world(a), self._to_world(b)]
            self.snap_pts.append(self._to_world(v))
        self.snap_hits[v] = hit
        return True

    def _snap_reset_marks(self):
        self.snap_pts = []
        self.snap_segs = []

    def _snap_commit(self):
        """Merge every snapped vert into its target (verts first, then edge splits)."""
        hits, self.snap_hits = getattr(self, "snap_hits", {}), {}
        self._snap_reset_marks()
        bm = self.bm
        merged = 0
        for v, hit in hits.items():
            if hit[0] == "vert" and v.is_valid and hit[1].is_valid and v is not hit[1]:
                bmesh.ops.pointmerge(bm, verts=[v, hit[1]], merge_co=hit[1].co.copy())
                merged += 1
        for v, hit in hits.items():
            if hit[0] != "edge" or not v.is_valid or not hit[1].is_valid:
                continue
            e, t = hit[1], hit[2]
            if v in e.verts:
                continue
            a = e.verts[0]
            _ne, nv = bmesh.utils.edge_split(e, a, t)
            nv.co = v.co.copy()
            bmesh.ops.pointmerge(bm, verts=[v, nv], merge_co=nv.co.copy())
            merged += 1
        return merged
