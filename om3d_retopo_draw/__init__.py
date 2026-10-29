# SPDX-License-Identifier: GPL-3.0-or-later
import os

import bpy

from .operator import OM3D_OT_retopo_draw, OM3D_OT_clear_dots, ext_key_name
from . import mods
from . import conform
from . import overlay


def _read_version():
    try:
        with open(os.path.join(os.path.dirname(__file__), "blender_manifest.toml"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "?"


VERSION = _read_version()

def _sync_ref_lock(self, context):
    mods.sync_ref_lock(context.scene)


class OM3D_RetopoSettings(bpy.types.PropertyGroup):
    reference: bpy.props.PointerProperty(
        name="Reference",
        description="High poly mesh to draw retopology on",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "MESH",
        update=_sync_ref_lock,
    )
    lock_reference: bpy.props.BoolProperty(
        name="Lock Reference", default=True, update=_sync_ref_lock,
        description="Make the reference unselectable in the viewport, so clicks and box "
                    "selects only pick the retopo mesh. Turn off to select it again")
    extend_key: bpy.props.EnumProperty(
        name="Extend Key",
        description="Key held while dragging a border edge to extend it",
        items=[("ALT", "Alt", "Alt+drag extends (Tab stays free for Edit Mode)"),
               ("TAB", "Tab", "Tab+drag extends, like Maya Quad Draw")],
        default="ALT",
    )
    face_opacity: bpy.props.FloatProperty(
        name="Face Opacity", default=0.6, min=0.0, max=1.0, subtype="FACTOR",
        description="How solid the retopo faces look while Retopo Draw is on")
    face_color: bpy.props.FloatVectorProperty(
        name="Face Color", subtype="COLOR_GAMMA", size=3, min=0.0, max=1.0,
        default=(0.05, 0.38, 1.0), description="Tint of the retopo faces")
    edge_color: bpy.props.FloatVectorProperty(
        name="Edge Color", subtype="COLOR_GAMMA", size=3, min=0.0, max=1.0,
        default=(0.0, 0.0, 0.0), description="Color of the retopo wireframe")
    edge_opacity: bpy.props.FloatProperty(
        name="Edge Opacity", default=1.0, min=0.0, max=1.0, subtype="FACTOR",
        description="How visible the retopo wireframe is (0 hides it)")
    overlay_outside: bpy.props.BoolProperty(
        name="Retopo Overlay", default=True, update=overlay.on_setting_changed,
        description="Show the Retopo Draw tint and edges on your retopo mesh outside Retopo Draw, in "
                    "Object and Edit Mode, whichever object is active. Selected parts show in orange. "
                    "Also a button in the viewport header")
    cull_backfaces: bpy.props.BoolProperty(
        name="Backface Culling", default=False,
        description="Hide retopo faces and edges that face away from you (Retopo Draw overlay only)")
    snap_foreign: bpy.props.BoolProperty(
        name="Keep Verts on Surface", default=True,
        description="Verts made or moved by other tools while Retopo Draw is on "
                    "(knife, loop cut, grab...) snap onto the reference")
    pick_radius: bpy.props.IntProperty(
        name="Pick Radius", default=14, min=4, max=60, subtype="PIXEL",
        description="How close the cursor has to be to grab a vert or edge")
    fill_radius: bpy.props.IntProperty(
        name="Fill Radius", default=300, min=50, max=1500, subtype="PIXEL",
        description="How far Shift fill looks for open verts")
    weld_radius: bpy.props.IntProperty(
        name="Weld Radius", default=12, min=2, max=60, subtype="PIXEL",
        description="Dropping a vert this close to another merges them")
    relax_radius: bpy.props.IntProperty(
        name="Relax Radius", default=70, min=10, max=600, subtype="PIXEL",
        description="Brush size for Shift+drag relax")
    relax_strength: bpy.props.FloatProperty(
        name="Relax Strength", default=0.5, min=0.05, max=1.0, subtype="FACTOR",
        description="How much each brush step smooths")
    relax_border: bpy.props.BoolProperty(
        name="Relax Border", default=False,
        description="Let relax slide border verts along the border (off pins them)")
    mirror: bpy.props.BoolProperty(
        name="Mirror", default=False, update=mods.on_setting_changed,
        description="Add a Mirror modifier (mirrored across the reference's center). "
                    "Verts on the center line stay on it and can't cross over")
    mirror_axis: bpy.props.EnumProperty(
        name="Mirror Axis", default="X", update=mods.on_setting_changed,
        items=[("X", "X", "Mirror across X"), ("Y", "Y", "Mirror across Y"), ("Z", "Z", "Mirror across Z")])
    surface_offset: bpy.props.FloatProperty(
        name="Surface Offset", default=0.0, min=0.0, soft_max=0.1, precision=4, step=0.1,
        subtype="DISTANCE", update=mods.on_setting_changed,
        description="Push the retopo mesh off the reference surface (a Shrinkwrap modifier "
                    "with an offset; apply it when you're done)")
    live_conform: bpy.props.BoolProperty(
        name="Live Conform", default=True, update=conform.on_setting_changed,
        description="In plain Edit Mode, keep the retopo mesh on the reference: G/R/S snap live, "
                    "and verts left off the surface by other tools (inset, extrude, loop cut, bevel) "
                    "snap back onto it")
    snap_edges: bpy.props.BoolProperty(
        name="Snap to Edges", default=True,
        description="A dragged vert or dot also snaps onto nearby edges and joins them on release")
    strip_width: bpy.props.IntProperty(
        name="Strip Width", default=40, min=8, max=400, subtype="PIXEL",
        description="Width of quad strips drawn with Alt/Tab+drag on empty surface")


def draw_settings(layout, s):
    """Settings shared by the sidebar panel and the Ctrl+Shift+RMB popup."""
    col = layout.column()
    row = col.row(align=True)
    row.label(text="Extend Key")
    row.prop(s, "extend_key", expand=True)
    box = col.box()
    box.label(text="Mesh")
    row = box.row(align=True)
    row.prop(s, "mirror", toggle=True)
    sub = row.row(align=True)
    sub.active = s.mirror
    sub.prop(s, "mirror_axis", expand=True)
    box.prop(s, "surface_offset")
    box.prop(s, "snap_edges")
    box.prop(s, "live_conform")
    row = box.row(align=True)
    row.operator("om3d.conform_selected", icon="MOD_SHRINKWRAP")
    row.operator("om3d.bridge_conform", icon="MOD_MESHDEFORM")
    box = col.box()
    box.label(text="Display")
    row = box.row(align=True)
    row.prop(s, "face_color", text="")
    row.prop(s, "face_opacity", text="Faces", slider=True)
    row = box.row(align=True)
    row.prop(s, "edge_color", text="")
    row.prop(s, "edge_opacity", text="Edges", slider=True)
    box.prop(s, "overlay_outside", icon="MOD_SHRINKWRAP")
    box.prop(s, "cull_backfaces")
    box.prop(s, "snap_foreign")
    box = col.box()
    box.label(text="Sizes")
    box.prop(s, "strip_width")
    box.prop(s, "pick_radius")
    box.prop(s, "fill_radius")
    box.prop(s, "weld_radius")
    box = col.box()
    box.label(text="Relax")
    box.prop(s, "relax_radius", text="Radius")
    box.prop(s, "relax_strength", text="Strength", slider=True)
    box.prop(s, "relax_border")


class OM3D_PT_retopo_draw(bpy.types.Panel):
    bl_label = "Retopo Draw"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OM3D"

    def draw_header_preset(self, context):
        self.layout.label(text=f"v{VERSION}")

    def draw_header(self, context):
        if context.window_manager.om3d_topo_running:
            self.layout.label(icon="RADIOBUT_ON")

    def draw(self, context):
        s = context.scene.om3d_topo
        running = context.window_manager.om3d_topo_running
        col = self.layout.column()
        row = col.row(align=True)
        row.prop(s, "reference")
        row.prop(s, "lock_reference", text="",
                 icon="RESTRICT_SELECT_ON" if s.lock_reference else "RESTRICT_SELECT_OFF")

        row = col.row()
        row.scale_y = 1.6
        if running:
            row.operator(OM3D_OT_retopo_draw.bl_idname, text="Retopo Draw ON  (click to exit)",
                         icon="CHECKMARK", depress=True)
        else:
            row.operator(OM3D_OT_retopo_draw.bl_idname, text="Retopo Draw", icon="GREASEPENCIL")
        col.operator(OM3D_OT_clear_dots.bl_idname, icon="TRASH")
        col.separator()
        draw_settings(col, s)

        k = ext_key_name(s)
        sections = (
            ("Off the mesh", (
                ("LMB", "click to drop dots"),
                ("LMB drag", "tweak dots (snaps to verts and edges)"),
                ("Ctrl+Shift+LMB", "delete dots"),
                (f"{k}+LMB drag", "draw quad strips"),
                (f"{k}+Wheel", "resize quad strips"),
                ("Shift+LMB", "click between dots: make a quad"),
                ("D+LMB drag", "pen loop / strip"),
            )),
            ("On the mesh", (
                ("LMB drag", "tweak vert / edge / face"),
                ("Ctrl+LMB", "click edge: insert loop (center snaps)"),
                ("Shift+LMB drag", "relax"),
                (f"{k}+LMB drag", "extend border edge"),
                (f"{k}+MMB drag", "extend border edge loop"),
                (f"Ctrl+{k}+LMB drag", "extend border edge loop"),
                ("Ctrl+Shift+LMB", "click or drag across faces: delete"),
                ("Ctrl+Shift+LMB", "click edge: delete edge loop"),
                ("Ctrl+Shift+MMB drag", "edge: move edge loop"),
                ("Ctrl+Shift+RMB", "options popup"),
                ("F", "over a hole: grid fill"),
                ("B, B", "on two borders: bridge"),
            )),
            ("Plain Edit Mode", (
                ("Live Conform on", "G/R/S, inset, extrude... stay on the surface"),
                ("Conform Selected", "snap selected verts/edges/faces"),
                ("Bridge Selected", "bridge two loops, divisions conformed"),
            )),
            ("Anywhere", (
                ("Ctrl+Z", "undo"),
                ("K, Ctrl+R, G...", "work as usual"),
                ("Esc / Enter / RMB", "exit"),
            )),
        )
        box = col.box()
        box.label(text="Shortcuts", icon="KEYINGSET")
        for title, rows in sections:
            box.label(text=title)
            grid = box.grid_flow(row_major=True, columns=2, even_columns=False, align=True)
            for key, what in rows:
                grid.label(text=key)
                grid.label(text=what)


class OM3D_PT_retopo_options(bpy.types.Panel):
    """Ctrl+Shift+RMB popup while Retopo Draw is on"""
    bl_label = "Retopo Draw Options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "HEADER"
    bl_ui_units_x = 14

    def draw(self, context):
        self.layout.label(text=f"OM3D Retopo Draw v{VERSION}")
        draw_settings(self.layout, context.scene.om3d_topo)


classes = (OM3D_RetopoSettings, OM3D_OT_retopo_draw, OM3D_OT_clear_dots,
           conform.OM3D_OT_conform_selected, conform.OM3D_OT_bridge_conform,
           OM3D_PT_retopo_draw, OM3D_PT_retopo_options)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.om3d_topo = bpy.props.PointerProperty(type=OM3D_RetopoSettings)
    bpy.types.WindowManager.om3d_topo_running = bpy.props.BoolProperty(default=False)
    conform.register()
    overlay.register()


def unregister():
    conform.unregister()
    overlay.unregister()
    from .operator import _mark_dirty
    if _mark_dirty in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_mark_dirty)
    del bpy.types.WindowManager.om3d_topo_running
    del bpy.types.Scene.om3d_topo
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
