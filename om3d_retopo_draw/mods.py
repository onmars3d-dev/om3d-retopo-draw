# SPDX-License-Identifier: GPL-3.0-or-later
"""Mirror and Surface Offset modifiers on the retopo mesh."""
import bpy
from mathutils import Vector

MIRROR = "OM3D Mirror"
OFFSET = "OM3D Offset"


def retopo_object(context):
    """The mesh Retopo Draw works on: the active mesh that isn't the reference."""
    s = context.scene.om3d_topo
    obj = context.view_layer.objects.active
    if obj is None or obj.type != "MESH" or obj == s.reference:
        return None
    return obj


def _move_to(obj, mod, index):
    try:
        obj.modifiers.move(obj.modifiers.find(mod.name), index)
    except Exception:
        pass


def sync(context, obj=None):
    """Make the retopo object's modifier stack match the Retopo Draw settings."""
    s = context.scene.om3d_topo
    obj = obj or retopo_object(context)
    if obj is None:
        return
    ref = s.reference

    m = obj.modifiers.get(MIRROR)
    if s.mirror and ref is not None:
        if m is None:
            m = obj.modifiers.new(MIRROR, "MIRROR")
        m.use_axis = [s.mirror_axis == a for a in "XYZ"]
        m.use_bisect_axis = (False, False, False)
        m.mirror_object = ref
        m.use_clip = True
        m.use_mirror_merge = True
        bb = [Vector(c) for c in ref.bound_box]
        m.merge_threshold = max((bb[6] - bb[0]).length * 1e-4, 1e-6)
        m.show_in_editmode = True
        m.show_on_cage = False
        _move_to(obj, m, 0)
    elif m is not None:
        obj.modifiers.remove(m)

    o = obj.modifiers.get(OFFSET)
    if s.surface_offset > 0.0 and ref is not None:
        if o is None:
            o = obj.modifiers.new(OFFSET, "SHRINKWRAP")
        o.target = ref
        o.wrap_method = "NEAREST_SURFACEPOINT"
        o.wrap_mode = "ABOVE_SURFACE"
        o.offset = s.surface_offset
        o.show_in_editmode = True
        o.show_on_cage = False
        _move_to(obj, o, len(obj.modifiers) - 1)  # after the mirror
    elif o is not None:
        obj.modifiers.remove(o)


def on_setting_changed(self, context):
    try:
        sync(context)
    except Exception:
        import traceback
        traceback.print_exc()


# ---------------------------------------------------------------- Lock Reference
LOCK_KEY = "om3d_ref_locked"  # scene prop: name of the object we made unselectable


def sync_ref_lock(scene):
    """Make the reference unselectable while Lock Reference is on. Only undoes
    what we did: the previous reference gets its selectability back."""
    self = getattr(scene, "om3d_topo", None)
    if self is None:
        return
    ref = self.reference
    prev = bpy.data.objects.get(scene.get(LOCK_KEY, ""))
    if prev is not None and prev != ref:
        prev.hide_select = False
    if ref is not None and self.lock_reference:
        ref.hide_select = True
        try:
            ref.select_set(False)
        except RuntimeError:
            pass  # not in this view layer
        scene[LOCK_KEY] = ref.name
    else:
        if ref is not None and scene.get(LOCK_KEY) == ref.name:
            ref.hide_select = False
        if LOCK_KEY in scene:
            del scene[LOCK_KEY]
