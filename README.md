# OM3D Retopo Draw

A Blender add-on for drawing retopology straight onto a high-poly reference mesh. Drop dots, fill quads, extend borders, insert loops and relax, all while the new mesh stays stuck to the surface. If you've used a quad-draw style retopo tool in other 3D apps, it should feel familiar.

Made by Aunmar at [OnMars3D](https://onmars3d.com). Built with Claude.

## Requirements

- Blender 4.2 or newer (developed and tested on Blender 5.2)
- A high-poly reference mesh and an empty (or started) retopo mesh

## Install

1. Download the latest zip from the Releases page, or zip the `om3d_retopo_draw` folder yourself.
2. In Blender go to Edit > Preferences > Get Extensions, open the menu at the top right and choose Install from Disk.
3. Pick the zip. The panel shows up in the 3D Viewport sidebar (N) under the OM3D tab.

## Quick start

1. Add a new empty mesh object for your retopo and make it active.
2. In the OM3D tab, set Reference to your high-poly mesh.
3. Click Retopo Draw. The viewport shows RETOPO DRAW MODE.
4. Click on the surface to drop dots, then hold Shift and click between four dots or verts to fill a quad.
5. Press Esc, Enter or right-click to leave the mode.

## Controls in Retopo Draw mode

The extend key can be Tab or Alt (set in the panel). It's shown as Tab below.

**Off the mesh**

| Input | Action |
| --- | --- |
| LMB | Drop a dot, or drag a dot (snaps to verts and edges) |
| Shift+LMB | Fill a quad between dots and verts. Inside a hole: 4 edges fill as one quad, an even count opens Grid Fill |
| Tab+LMB drag | Draw a quad strip (Tab+Wheel changes its width) |
| D+drag | Pen: draw a loop or strip |
| Ctrl+Shift+LMB | Delete a dot |

**On the mesh**

| Input | Action |
| --- | --- |
| LMB drag | Move a vert, edge or face (snaps live) |
| Ctrl+LMB | Insert an edge loop |
| Shift+drag | Relax |
| Tab+LMB drag | Extend a border edge |
| Tab+MMB drag | Extend a whole border loop |
| Ctrl+Shift+LMB | Delete a vert or face, click an edge to delete its loop, drag to delete faces |
| Ctrl+Shift+MMB drag | Move an edge loop |
| F | Grid fill the hole under the cursor |
| B, then B | Bridge the facing parts of two borders (Ctrl+Wheel for rows, Enter to apply) |
| Ctrl+Shift+RMB | Options popup |
| Ctrl+Z | Undo |

## Working in plain Edit Mode

You don't have to stay in Retopo Draw. With Live Conform on, Blender's own tools keep the retopo mesh on the reference surface: G, R, S, extrude (E then S is a quick way to copy an edge loop outward), inset, loop cut and bevel. The panel also has:

- **Conform Selected** snaps the selected verts, edges or faces back onto the surface.
- **Bridge Selected** bridges two selected edge loops with divisions that follow the surface.

## Panel settings

- **Lock Reference** (icon next to Reference) makes the reference unselectable so clicks only hit the retopo mesh.
- **Mirror** with an X, Y or Z axis adds a mirror across the reference's center. Verts on the symmetry line stay on it.
- **Surface Offset** pushes the retopo mesh slightly off the surface.
- **Snap to Edges** lets dragged verts snap onto nearby edges as well as verts.
- **Live Conform** keeps Edit Mode tools on the surface.
- **Display:** face and edge color and opacity, Retopo Overlay (the same look outside the mode, also a button in the viewport header), Backface Culling and Keep Verts on Surface.
- **Sizes and Relax:** strip width, pick, fill and weld radius, relax radius and strength.

## Known limitations

- Very large jumps off the surface conform to the nearest point, which can fold geometry onto ridges. Work in small moves.
- Shift fill picks the best quad around the cursor from nearby open verts. If it picks the wrong one, drop a dot where you want the corner, since quads that use your dots win.

## About this tool

I built this for my own workflow and I'm sharing it as a starting point. Use it, change it, make it yours. It's provided as is, so test it on a copy of your scene first.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
