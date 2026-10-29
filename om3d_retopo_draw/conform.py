# SPDX-License-Identifier: GPL-3.0-or-later
"""Live Conform: keep a retopo mesh on the reference while editing in plain Edit Mode.

Two halves work together:
- While you drag with G / R / S, Blender's own snapping (Face Nearest, each vert
  on its own, never onto the mesh itself) keeps verts on the reference live.
- After any other tool (inset, extrude, loop cut, bevel, bridge...), the verts it
  left off the surface are snapped onto it. No undo step is pushed, so Ctrl+Z
  takes back the tool and the conform together.

Only meshes Retopo Draw has worked on (tagged RETOPO_TAG) are touched, so editing
any other mesh in the scene is left alone.
"""
import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

RETOPO_TAG = "om3d_retopo"
SNAP_KEYS = ("use_snap", "snap_elements", "use_snap_self", "use_snap_edit", "use_snap_nonedit",
             "use_snap_translate", "use_snap_rotate", "use_snap_scale", "use_snap_project")

_state = {
    "baseline": {},      # object name -> rounded coords of verts that were already off the surface
    "edit_obj": None,    # name of the object in Edit Mode at the last update
    "snap_saved": None,  # the user's own snapping settings, restored when conform lets go
    "busy": False,
    "bvh": None,
    "bvh_key": None,
}


# ---------------------------------------------------------------- helpers
def _settings(context):
    return getattr(context.scene, "om3d_topo", None)


def is_retopo(obj, s):
    return (obj is not None and obj.type == "MESH" and s is not None and s.reference is not None
            and obj != s.reference and bool(obj.get(RETOPO_TAG)))


def tag(obj):
    if obj is not None and obj.type == "MESH":
        obj[RETOPO_TAG] = True


def _topo_running(context):
    return bool(getattr(context.window_manager, "om3d_topo_running", False))


def _other_modal_running(context):
    for win in context.window_manager.windows:
        try:
            if len(win.modal_operators):
                return True
        except Exception:
            pass
    return False


def _bvh(context, ref):
    key = (ref.name, ref.data.name, len(ref.data.polygons),
           tuple(round(x, 6) for row in ref.matrix_world for x in row))
    if _state["bvh_key"] != key or _state["bvh"] is None:
        _state["bvh"] = BVHTree.FromObject(ref, context.evaluated_depsgraph_get())
        _state["bvh_key"] = key
    return _state["bvh"]


def _eps(ref):
    bb = [Vector(c) for c in ref.bound_box]
    return max((bb[6] - bb[0]).length * 1e-4, 1e-7)


def _key(co):
    return (round(co.x, 5), round(co.y, 5), round(co.z, 5))


class _Snapper:
    """Snap a world point onto the reference, keeping mirror-seam verts on the seam."""

    def __init__(self, context, s, obj):
        ref = s.reference
        self.bvh = _bvh(context, ref)
        self.ref_mw = ref.matrix_world.copy()
        self.ref_inv = self.ref_mw.inverted_safe()
        self.mw = obj.matrix_world.copy()
        self.inv = self.mw.inverted_safe()
        self.eps = _eps(ref)
        self.axis = {"X": 0, "Y": 1, "Z": 2}[s.mirror_axis] if s.mirror else None

    def dist(self, co_local):
        loc, _n, _i, d = self.bvh.find_nearest(self.ref_inv @ (self.mw @ co_local))
        return loc, d

    def snap(self, co_local):
        r = self.ref_inv @ (self.mw @ co_local)
        on_seam = self.axis is not None and abs(r[self.axis]) <= self.eps
        loc, _n, _i, _d = self.bvh.find_nearest(r)
        if loc is None:
            return co_local
        if on_seam:
            for _ in range(3):  # settle onto the surface while staying on the seam
                loc = loc.copy()
                loc[self.axis] = 0.0
                nxt, _n, _i, _d = self.bvh.find_nearest(loc)
                if nxt is None:
                    break
                loc = nxt
            loc = loc.copy()
            loc[self.axis] = 0.0
        return self.inv @ (self.ref_mw @ loc)


def conform_verts(context, obj, verts, s=None, respect_baseline=False):
    """Snap the given BMVerts of obj (in Edit Mode) onto the reference. Returns the count moved."""
    s = s or _settings(context)
    if s is None or s.reference is None:
        return 0
    sn = _Snapper(context, s, obj)
    exempt = _state["baseline"].get(obj.name, set()) if respect_baseline else set()
    moved = 0
    for v in verts:
        if not v.is_valid or v.hide:
            continue
        loc, d = sn.dist(v.co)
        if loc is None or d <= sn.eps:
            continue
        if exempt and _key(v.co) in exempt:
            continue
        v.co = sn.snap(v.co)
        moved += 1
    return moved


def _make_baseline(context, obj, s):
    """Verts already off the surface when editing starts are left where they are."""
    try:
        bm = bmesh.from_edit_mesh(obj.data)
    except Exception:
        return
    sn = _Snapper(context, s, obj)
    base = set()
    for v in bm.verts:
        loc, d = sn.dist(v.co)
        if loc is None or d > sn.eps:
            base.add(_key(v.co))
    _state["baseline"][obj.name] = base


# ---------------------------------------------------------------- Blender snapping while dragging
def _snap_wanted(context):
    s = _settings(context)
    obj = context.edit_object
    return bool(s is not None and s.live_conform and is_retopo(obj, s))


def _apply_snapping(context, on):
    ts = context.scene.tool_settings
    if on and _state["snap_saved"] is None:
        saved = {}
        for k in SNAP_KEYS:
            if hasattr(ts, k):
                v = getattr(ts, k)
                saved[k] = set(v) if isinstance(v, set) else v
        _state["snap_saved"] = saved
        ts.use_snap = True
        ts.snap_elements = {"FACE_NEAREST"}
        ts.use_snap_self = False
        ts.use_snap_edit = True
        ts.use_snap_nonedit = True
        ts.use_snap_translate = True
        ts.use_snap_rotate = True
        ts.use_snap_scale = True
    elif not on and _state["snap_saved"] is not None:
        for k, v in _state["snap_saved"].items():
            try:
                setattr(ts, k, v)
            except Exception:
                pass
        _state["snap_saved"] = None


# ---------------------------------------------------------------- the deferred conform pass
def _pass():
    context = bpy.context
    try:
        if _other_modal_running(context) and not _topo_running(context):
            return 0.1  # a tool is still running (a G drag, an inset): wait for it to finish
        s = _settings(context)
        obj = context.edit_object
        if (s is None or not s.live_conform or _topo_running(context) or not is_retopo(obj, s)):
            return None
        _state["busy"] = True
        bm = bmesh.from_edit_mesh(obj.data)
        if conform_verts(context, obj, bm.verts, s, respect_baseline=True):
            bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        _state["busy"] = False
    return None


@bpy.app.handlers.persistent
def on_depsgraph(scene, depsgraph):
    if _state["busy"]:
        return
    context = bpy.context
    try:
        s = _settings(context)
        obj = context.edit_object
        name = obj.name if obj is not None else None
        # entering Edit Mode on a mesh: note which verts already sit off the surface
        if name != _state["edit_obj"]:
            _state["edit_obj"] = name
            if obj is not None and s is not None and s.live_conform and is_retopo(obj, s):
                _make_baseline(context, obj, s)
        _apply_snapping(context, _snap_wanted(context) and not _topo_running(context))
        if obj is None or s is None or not s.live_conform or _topo_running(context) or not is_retopo(obj, s):
            return
        if not any(u.id.original == obj.data or u.id.original == obj for u in depsgraph.updates):
            return
        if not bpy.app.timers.is_registered(_pass):
            bpy.app.timers.register(_pass, first_interval=0.05)
    except Exception:
        import traceback
        traceback.print_exc()


def on_setting_changed(self, context):
    """Live Conform toggled: rebuild the baseline and switch Blender's snapping."""
    obj = context.edit_object
    if self.live_conform and is_retopo(obj, self):
        _make_baseline(context, obj, self)
    _apply_snapping(context, _snap_wanted(context) and not _topo_running(context))


def register():
    if on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(on_depsgraph)


def unregister():
    if on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(on_depsgraph)
    if bpy.app.timers.is_registered(_pass):
        bpy.app.timers.unregister(_pass)
    try:
        _apply_snapping(bpy.context, False)
    except Exception:
        pass


# ---------------------------------------------------------------- operators
class OM3D_OT_conform_selected(bpy.types.Operator):
    """Snap the selected verts, edges or faces onto the reference surface"""
    bl_idname = "om3d.conform_selected"
    bl_label = "Conform Selected"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = _settings(context)
        obj = context.edit_object
        return obj is not None and obj.type == "MESH" and s is not None and s.reference is not None \
            and obj != s.reference

    def execute(self, context):
        s = _settings(context)
        obj = context.edit_object
        tag(obj)
        bm = bmesh.from_edit_mesh(obj.data)
        verts = [v for v in bm.verts if v.select]
        if not verts:
            self.report({"WARNING"}, "Nothing selected")
            return {"CANCELLED"}
        _state["busy"] = True
        try:
            n = conform_verts(context, obj, verts, s)
            bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
        finally:
            _state["busy"] = False
        self.report({"INFO"}, f"Conformed {n} of {len(verts)} selected verts")
        return {"FINISHED"}


class OM3D_OT_bridge_conform(bpy.types.Operator):
    """Bridge the selected edge loops and lay the new divisions on the reference surface"""
    bl_idname = "om3d.bridge_conform"
    bl_label = "Bridge Selected"
    bl_options = {"REGISTER", "UNDO"}

    cuts: bpy.props.IntProperty(name="Divisions", default=2, min=0, max=64,
                                description="Edge loops added across the bridge")
    smoothness: bpy.props.FloatProperty(name="Smoothness", default=1.0, min=0.0, max=2.0,
                                        description="How much the bridge curves before it is conformed")

    @classmethod
    def poll(cls, context):
        return OM3D_OT_conform_selected.poll(context)

    def execute(self, context):
        s = _settings(context)
        obj = context.edit_object
        tag(obj)
        bm = bmesh.from_edit_mesh(obj.data)
        before = {_key(v.co) for v in bm.verts}  # element refs don't survive the bridge op
        _state["busy"] = True
        try:
            try:
                bpy.ops.mesh.bridge_edge_loops(number_cuts=self.cuts, interpolation="SURFACE",
                                               smoothness=self.smoothness)
            except RuntimeError as e:
                self.report({"WARNING"}, str(e).strip().splitlines()[-1])
                return {"CANCELLED"}
            bm = bmesh.from_edit_mesh(obj.data)
            new = [v for v in bm.verts if _key(v.co) not in before]
            n = conform_verts(context, obj, new, s)
            bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
        finally:
            _state["busy"] = False
        self.report({"INFO"}, f"Bridged with {self.cuts} divisions, {n} new verts conformed")
        return {"FINISHED"}
