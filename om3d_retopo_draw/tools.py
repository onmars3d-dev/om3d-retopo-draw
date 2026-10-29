# SPDX-License-Identifier: GPL-3.0-or-later
"""Grid Fill, Bridge and Pen Loops for the Retopo Draw modal (mixed into the operator)."""
from mathutils import Vector

from . import core
from . import fills

TOOL_STATES = {"PEN_STROKE", "PEN_EDIT", "PEN_DRAG", "GRID", "BRIDGE"}
WHEEL = {"WHEELUPMOUSE", "WHEELDOWNMOUSE"}
PEN_STEPS = 12
SIDE_NAMES = {True: {"CENTER": "center", "IN": "inside", "OUT": "outside"},
              False: {"CENTER": "center", "IN": "side 1", "OUT": "side 2"}}


class TopoToolsMixin:
    """Needs the operator's helpers: bm, _snap, _ray_ref, _screen, _surface_normal, ..."""

    # ------------------------------------------------------------ shared
    def _tools_init(self):
        self.d_held = False
        self.stroke = None
        self.pen = None
        self.grid = None
        self.bridge = None
        self.bridge_a = None
        self.preview_lines = None
        self.preview_pts = None
        self.preview_hot = None
        self.mark_lines = None

    def _tool_clear(self):
        self.state = "IDLE"
        self.stroke = None
        self.pen = None
        self.grid = None
        self.bridge = None
        self.preview_lines = None
        self.preview_pts = None
        self.preview_hot = None

    def _warn(self, msg):
        self.hint = msg
        try:
            self.report({"WARNING"}, msg)
        except Exception:
            pass

    def _poly_segments(self, pts, closed):
        segs = []
        n = len(pts)
        for i in range(n if closed else n - 1):
            segs += [pts[i], pts[(i + 1) % n]]
        return segs

    def _orient(self, f):
        mw = self.obj.matrix_world
        core.orient_face(f, mw, self._surface_normal(mw @ f.calc_center_median()))

    def _new_face(self, verts):
        uniq = []
        for v in verts:
            if v not in uniq:
                uniq.append(v)
        if len(uniq) < 3:
            return None
        try:
            f = self.bm.faces.new(uniq)
        except ValueError:
            return None
        self._orient(f)
        return f

    def _refresh_mark(self):
        a = self.bridge_a
        if a is None:
            self.mark_lines = None
            return
        e = a[2]
        if not e.is_valid:
            self.bridge_a = None
            self.mark_lines = None
            return
        self.mark_lines = [self._to_world(e.verts[0]), self._to_world(e.verts[1])]

    def _pick_border(self):
        s = self.settings
        self._project_all()
        near = self._visible_near(s.fill_radius)
        return core.pick_open_edge(near, self.mouse, s.pick_radius * 2)

    # ------------------------------------------------------------ modal for tool states
    def _modal_tool(self, event):
        st = self.state
        et = event.type
        if et in WHEEL and (event.ctrl or event.shift or event.alt) and st != "PEN_STROKE":
            self._tool_wheel(event, 1 if et == "WHEELUPMOUSE" else -1)
            self.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if et in self.NAV_EVENTS or (et.startswith("NUMPAD_") and et != "NUMPAD_ENTER"):
            return {"PASS_THROUGH"}

        self.mouse = self._event_mouse(event)

        if et in self.MOVE_EVENTS:
            if st == "PEN_STROKE":
                if (self.mouse - self.stroke[-1]).length > 3.0:
                    self.stroke.append(self.mouse.copy())
            elif st == "PEN_DRAG":
                hit = self._ray_ref(self.mouse)
                if hit is not None:
                    self.pen["ctrl"][self.pen["drag"]] = hit
                    self._pen_update()
            elif st == "PEN_EDIT":
                self._pen_hover()
            self.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if et == "LEFTMOUSE":
            if st == "PEN_STROKE" and event.value == "RELEASE":
                self._pen_finish_stroke()
            elif st == "PEN_DRAG" and event.value == "RELEASE":
                self.state = "PEN_EDIT"
                self._pen_update()
            elif st == "PEN_EDIT" and event.value == "PRESS":
                self._pen_click(event)
            elif st in {"GRID", "BRIDGE"} and event.value == "PRESS":
                self._tool_commit()
            self.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if et == "D" and event.value == "RELEASE":
            self.d_held = False
        if event.value == "PRESS" and st != "PEN_STROKE":
            if et in {"RET", "NUMPAD_ENTER", "SPACE"}:
                self._tool_commit()
            elif et in {"ESC", "RIGHTMOUSE"}:
                if st == "BRIDGE":
                    self.bridge_a = None
                    self.mark_lines = None
                self._tool_clear()
                self.hint = ""
            elif et == "C" and st == "PEN_EDIT":
                order = ["CENTER", "IN", "OUT"]
                p = self.pen
                p["side"] = order[(order.index(p["side"]) + 1) % 3]
                self._pen_update()
            self.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _tool_wheel(self, event, d):
        st = self.state
        if st in {"PEN_EDIT", "PEN_DRAG"}:
            p = self.pen
            if event.ctrl:
                step = 2 if p["closed"] else 1  # closed loops stay even so Grid Fill works
                p["segments"] = max(4 if p["closed"] else 1, min(256, p["segments"] + d * step))
            elif event.shift:
                p["rings"] = max(0, min(12, p["rings"] + d))
            elif event.alt:
                p["ring_w"] *= 1.1 if d > 0 else 1 / 1.1
            self._pen_update()
        elif st == "GRID":
            g = self.grid
            n = len(g["loop"])
            if event.ctrl:
                g["span"] = max(1, min(n // 2 - 1, g["span"] + d))
            elif event.shift:
                g["offset"] = (g["offset"] + d) % n
            self._grid_update()
        elif st == "BRIDGE":
            if event.ctrl:
                self.bridge["rows"] = max(1, min(64, self.bridge["rows"] + d))
                self._bridge_update()

    def _tool_commit(self):
        st = self.state
        if st in {"PEN_EDIT", "PEN_DRAG"}:
            self._pen_commit()
        elif st == "GRID":
            self._grid_commit()
        elif st == "BRIDGE":
            self._bridge_commit()

    # ------------------------------------------------------------ grid fill
    def _hole_under_mouse(self, near):
        """The closed hole whose outline surrounds the mouse (smallest one), or None."""
        seen, best = set(), None
        for v in near:
            for e in v.link_edges:
                if e in seen or len(e.link_faces) != 1:
                    continue
                loop = fills.border_cycle(e)
                if loop is None:
                    seen.add(e)
                    continue
                n = len(loop)
                for i in range(n):
                    x = self.bm.edges.get((loop[i], loop[(i + 1) % n]))
                    if x is not None:
                        seen.add(x)
                poly = [self.proj.get(w) for w in loop]
                if None in poly or not core.point_in_poly(self.mouse, poly):
                    continue
                if self._is_outer_border(loop):
                    continue
                if self._on_mirror_seam(loop):
                    continue  # the open side along the symmetry line isn't a hole
                if best is None or n < len(best):
                    best = loop
        return best

    def _on_mirror_seam(self, loop):
        """With Mirror on: does this border run along the symmetry plane (an edge
        with both ends on the seam)? The mirrored half closes it, so it's no hole."""
        if not getattr(self.settings, "mirror", False) or not hasattr(self, "_plane_eps"):
            return False
        try:
            on = [abs(self._plane_coord(self._to_world(v))) <= self._plane_eps * 10 for v in loop]
        except Exception:
            return False
        n = len(on)
        return any(on[i] and on[(i + 1) % n] for i in range(n))

    def _grid_start(self, loop=None):
        """F over a border (or Shift+click inside a hole). Returns True if used."""
        if loop is None:
            edge = self._pick_border()
            if edge is None:
                return False
            loop = fills.border_cycle(edge)
        if loop is None:
            self._warn("GRID FILL: that border isn't a closed hole")
            return True
        n = len(loop)
        if n < 4 or n % 2:
            self._warn(f"GRID FILL: hole has {n} edges, needs an even count (4 or more)")
            return True
        if self._is_outer_border(loop):
            self._warn("GRID FILL: that's the outer border, hover the edge of a hole")
            return True
        cos = [self._to_world(v) for v in loop]
        span, offset = fills.best_grid_layout(cos)
        self.grid = {"loop": loop, "span": span, "offset": offset}
        self.state = "GRID"
        self._grid_update()
        return True

    def _is_outer_border(self, loop):
        poly = [self.proj.get(v) for v in loop]
        if None in poly:
            poly = [self._screen(self._to_world(v)) for v in loop]
            if None in poly:
                return False
        faces = {f for v in loop for f in v.link_faces}
        if not faces:
            return False
        inside = 0
        for f in faces:
            c = self._screen(self.obj.matrix_world @ f.calc_center_median())
            if c is not None and core.point_in_poly(c, poly):
                inside += 1
        return inside > len(faces) / 2

    def _grid_update(self):
        g = self.grid
        loop = g["loop"]
        cos = [self._to_world(v) for v in loop]
        grid = fills.coons_grid(cos, g["span"], g["offset"])
        pts = [[cos[e] if isinstance(e, int) else self._snap(e) for e in col] for col in grid]
        # relax the interior so it spreads evenly instead of following the raw blend
        su, tv = len(pts) - 1, len(pts[0]) - 1
        for _ in range(12):
            nxt = [col[:] for col in pts]
            for u in range(1, su):
                for v in range(1, tv):
                    avg = (pts[u - 1][v] + pts[u + 1][v] + pts[u][v - 1] + pts[u][v + 1]) * 0.25
                    nxt[u][v] = self._snap(avg)
            pts = nxt
        for ci, col in enumerate(grid):
            for vi, e in enumerate(col):
                if not isinstance(e, int):
                    col[vi] = pts[ci][vi]
        g["grid"] = grid
        segs = []
        s, t = len(pts) - 1, len(pts[0]) - 1
        for u in range(s + 1):
            segs += self._poly_segments(pts[u], False)
        for v in range(t + 1):
            segs += self._poly_segments([pts[u][v] for u in range(s + 1)], False)
        self.preview_lines = segs
        self.preview_pts = [pts[0][0]]  # first corner, to see the rotation
        self.hint = (f"GRID FILL {s} x {t}   Ctrl+Wheel: span   Shift+Wheel: rotate   "
                     f"Enter / LMB: apply   Esc: cancel")

    def _grid_commit(self):
        g = self.grid
        loop = g["loop"]
        if not all(v.is_valid for v in loop):
            self._tool_clear()
            return
        inv = self.obj.matrix_world.inverted_safe()
        bm = self.bm
        vg = [[loop[e] if isinstance(e, int) else bm.verts.new(inv @ e) for e in col]
              for col in g["grid"]]
        self._mirror_fix_new([x for col, gcol in zip(vg, g["grid"]) for x, e in zip(col, gcol)
                              if not isinstance(e, int)])
        s, t = len(vg) - 1, len(vg[0]) - 1
        for u in range(s):
            for v in range(t):
                self._new_face((vg[u][v], vg[u + 1][v], vg[u + 1][v + 1], vg[u][v + 1]))
        self._commit()
        self._undo_push("Grid fill")
        self._tool_clear()
        self.hint = ""

    # ------------------------------------------------------------ bridge
    def _bridge_key(self):
        """B over a border edge, then B over another. Bridges the parts of the two
        borders that face each other, grown out from the two edges you pointed at."""
        edge = self._pick_border()
        if edge is None:
            if self.bridge_a is not None:
                self._warn("BRIDGE: hover a border edge (it highlights) and press B")
                return True
            return False
        verts, closed = core.boundary_chain(edge)
        if self.bridge_a is None:
            self.bridge_a = (verts, closed, edge)
            self._refresh_mark()
            self.hint = "BRIDGE: hover the facing border edge and press B (Esc cancels)"
            return True
        a, a_closed, a_edge = self.bridge_a
        if not a_edge.is_valid or not all(v.is_valid for v in a):
            self.bridge_a = None
            self.mark_lines = None
            return True
        if edge == a_edge:
            self.bridge_a = None
            self.mark_lines = None
            self.hint = ""
            return True
        same = set(verts) == set(a)  # e.g. the two arms of a U-shaped border

        def idx(chain, e, closed_):
            n = len(chain)
            i0, i1 = chain.index(e.verts[0]), chain.index(e.verts[1])
            if (i0 + 1) % n == i1 and (closed_ or i0 + 1 == i1):
                return i0
            if (i1 + 1) % n == i0 and (closed_ or i1 + 1 == i0):
                return i1
            return None

        ia = idx(a, a_edge, a_closed)
        ib = idx(verts, edge, closed)
        if ia is None or ib is None:
            self._warn("BRIDGE: couldn't follow those borders")
            return True
        PA = [self._to_world(v) for v in a]
        PB = [self._to_world(v) for v in verts]
        res = fills.bridge_spans(PA, a_closed, ia, PB, closed, ib, same=same)
        if res is None:
            self._warn("BRIDGE: those edges don't face each other")
            return True
        sa, sb, span_closed = res
        A = [a[i] for i in sa]
        B = [verts[i] for i in sb]
        if set(A) & set(B):
            self._warn("BRIDGE: those edges touch, pick two that face each other across a gap")
            return True
        mw = self.obj.matrix_world
        gap = sum(((mw @ x.co) - (mw @ y.co)).length for x, y in zip(A, B)) / len(A)
        alen = fills.polyline_length([mw @ v.co for v in A], span_closed) / max(len(A) - (0 if span_closed else 1), 1)
        rows = max(1, min(64, round(gap / alen))) if alen > 0 else 1
        self.bridge = {"a": A, "b": B, "closed": span_closed, "rows": rows}
        self.state = "BRIDGE"
        self._bridge_update()
        return True

    def _bridge_rows(self):
        br = self.bridge
        A = [self._to_world(v) for v in br["a"]]
        B = [self._to_world(v) for v in br["b"]]
        R = br["rows"]
        rows = [A]
        for j in range(1, R):
            f = j / R
            rows.append([self._snap(x.lerp(y, f)) for x, y in zip(A, B)])
        rows.append(B)
        return rows

    def _bridge_update(self):
        br = self.bridge
        rows = self._bridge_rows()
        segs = []
        for r in rows:
            segs += self._poly_segments(r, br["closed"])
        for i in range(len(rows[0])):
            segs += self._poly_segments([r[i] for r in rows], False)
        self.preview_lines = segs
        self.preview_pts = None
        self.hint = f"BRIDGE {br['rows']} rows   Ctrl+Wheel: rows   Enter / LMB: apply   Esc: cancel"

    def _bridge_commit(self):
        br = self.bridge
        if not all(v.is_valid for v in br["a"] + br["b"]):
            self._tool_clear()
            return
        rows = self._bridge_rows()
        inv = self.obj.matrix_world.inverted_safe()
        bm = self.bm
        vrows = [br["a"]] + [[bm.verts.new(inv @ p) for p in r] for r in rows[1:-1]] + [br["b"]]
        self._mirror_fix_new([x for r in vrows[1:-1] for x in r])
        m = len(br["a"])
        segs = m if br["closed"] else m - 1
        for j in range(len(vrows) - 1):
            for i in range(segs):
                k = (i + 1) % m
                self._new_face((vrows[j][i], vrows[j][k], vrows[j + 1][k], vrows[j + 1][i]))
        self._commit()
        self._undo_push("Bridge")
        self.bridge_a = None
        self.mark_lines = None
        self._tool_clear()
        self.hint = ""

    # ------------------------------------------------------------ pen loops
    def _pen_begin(self):
        self.stroke = [self.mouse.copy()]
        self.state = "PEN_STROKE"
        self.hint = "PEN: drawing... release to finish (end near the start for a loop)"

    def _pen_finish_stroke(self):
        pts, self.stroke = self.stroke, None
        self.state = "IDLE"
        if not pts or len(pts) < 2:
            return
        length = fills.polyline_length(pts, False)
        if length < 20:
            return
        closed = length > 80 and (pts[0] - pts[-1]).length < max(25.0, 0.08 * length)
        if closed:
            length = fills.polyline_length(pts, True)
            k = max(4, min(16, round(length / 80)))
        else:
            k = max(2, min(12, round(length / 80) + 1))
        ctrl = [self._ray_ref(p) for p in fills.resample(pts, k, closed)]
        ctrl = [c for c in ctrl if c is not None]
        if len(ctrl) < (3 if closed else 2):
            self._warn("PEN: the stroke has to be drawn on the reference mesh")
            return
        if closed:
            segs = max(6, min(64, 2 * round(length / 90)))
        else:
            segs = max(2, min(64, round(length / 45)))
        self.pen = {"ctrl": ctrl, "closed": closed, "segments": segs, "rings": 1,
                    "ring_w": None, "side": "CENTER", "hot": None, "drag": None}
        self.state = "PEN_EDIT"
        self._pen_update()

    def _pen_update(self):
        p = self.pen
        ctrl, closed = p["ctrl"], p["closed"]
        dense = [self._snap(x) for x in fills.catmull_rom(ctrl, closed, PEN_STEPS)]
        p["curve"] = dense
        seg = p["segments"]
        samples = [self._snap(x) for x in fills.resample(dense, seg if closed else seg + 1, closed)]
        spacing = fills.polyline_length(samples, closed) / max(seg, 1)
        if p["ring_w"] is None:
            p["ring_w"] = spacing
        m = len(samples)
        side = []
        for i, s in enumerate(samples):
            if closed:
                tng = samples[(i + 1) % m] - samples[(i - 1) % m]
            else:
                tng = samples[min(i + 1, m - 1)] - samples[max(i - 1, 0)]
            sv = tng.cross(self._surface_normal(s))
            side.append(sv.normalized() if sv.length > 1e-12 else Vector())
        sgn = 1.0
        if closed:
            poly = [self._screen(s) for s in samples]
            q = self._screen(samples[0] + side[0] * spacing * 0.25)
            if None not in poly and q is not None and not core.point_in_poly(q, poly):
                sgn = -1.0
        R, w, mode = p["rings"], p["ring_w"], p["side"]
        capped = False
        if closed and R > 0 and mode != "OUT":
            # keep inner rings from crossing the middle of the loop
            centre = sum(samples, Vector()) / m
            radius = sum((s - centre).length for s in samples) / m
            inward = R * w if mode == "IN" else R * w / 2
            if inward > 0.7 * radius > 0:
                w *= 0.7 * radius / inward
                capped = True
        if R == 0:
            rows = [samples]
        else:
            rows = []
            for r in range(R + 1):
                if mode == "CENTER":
                    off = (r - R / 2) * w * sgn
                elif mode == "IN":
                    off = r * w * sgn
                else:
                    off = -r * w * sgn
                rows.append([self._snap(s + sv * off) if off else s for s, sv in zip(samples, side)])
        p["rows"] = rows
        segs = []
        for r in rows:
            segs += self._poly_segments(r, closed)
        if len(rows) > 1:
            for i in range(m):
                segs += self._poly_segments([r[i] for r in rows], False)
        self.preview_lines = segs
        self.preview_pts = list(ctrl)
        self.preview_hot = ctrl[p["hot"]] if p["hot"] is not None and p["hot"] < len(ctrl) else None
        kind = "LOOP" if closed else "STRIP"
        what = "edge loop only" if R == 0 else f"{R} ring{'s' if R != 1 else ''}"
        if capped:
            what += " (width capped)"
        self.hint = (f"PEN {kind}: {seg} segments, {what}, {SIDE_NAMES[closed][mode]}   "
                     f"Ctrl/Shift/Alt+Wheel: segments/rings/width   C: side   "
                     f"drag points, Ctrl+LMB add, Ctrl+Shift+LMB remove   Enter: bake   Esc: cancel")

    def _pen_hover(self):
        p = self.pen
        best, best_d = None, self.settings.pick_radius * 1.5
        for i, c in enumerate(p["ctrl"]):
            sc = self._screen(c)
            if sc is None:
                continue
            d = (sc - self.mouse).length
            if d <= best_d:
                best, best_d = i, d
        if best != p["hot"]:
            p["hot"] = best
            self.preview_hot = p["ctrl"][best] if best is not None else None

    def _pen_click(self, event):
        p = self.pen
        self._pen_hover()
        ctrl = p["ctrl"]
        if event.ctrl and event.shift:
            if p["hot"] is not None and len(ctrl) > (3 if p["closed"] else 2):
                del ctrl[p["hot"]]
                p["hot"] = None
                self._pen_update()
        elif event.ctrl:
            dense = p["curve"]
            best, best_d = None, float("inf")
            for i, c in enumerate(dense):
                sc = self._screen(c)
                if sc is None:
                    continue
                d = (sc - self.mouse).length
                if d < best_d:
                    best, best_d = i, d
            if best is not None and best_d < self.settings.pick_radius * 3:
                nsegs = len(ctrl) if p["closed"] else len(ctrl) - 1
                seg_i = min(best // PEN_STEPS, nsegs - 1)
                ctrl.insert(seg_i + 1, dense[best].copy())
                p["hot"] = seg_i + 1
                self._pen_update()
        elif p["hot"] is not None:
            p["drag"] = p["hot"]
            self.state = "PEN_DRAG"

    def _pen_commit(self):
        p = self.pen
        rows, closed = p["rows"], p["closed"]
        inv = self.obj.matrix_world.inverted_safe()
        bm = self.bm
        vrows = [[bm.verts.new(inv @ x) for x in r] for r in rows]
        self._mirror_fix_new([x for r in vrows for x in r])
        m = len(rows[0])
        segs = m if closed else m - 1
        if len(vrows) == 1:
            r = vrows[0]
            for i in range(segs):
                try:
                    bm.edges.new((r[i], r[(i + 1) % m]))
                except ValueError:
                    pass
        else:
            for j in range(len(vrows) - 1):
                for i in range(segs):
                    k = (i + 1) % m
                    self._new_face((vrows[j][i], vrows[j][k], vrows[j + 1][k], vrows[j + 1][i]))
        self._commit()
        self._undo_push("Pen loop" if closed else "Pen strip")
        self._tool_clear()
        self.hint = ""
