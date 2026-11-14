# Changelog

## 0.8.5
- Mirror and Surface Offset only add their modifiers to a mesh Retopo Draw has worked on. Before, changing them with an unrelated mesh active put the modifiers on that mesh.
- Retopo Draw never draws into a linked duplicate of the reference (which shares its mesh) or into linked library data. It makes a new retopo mesh instead.
- Opening a file, closing the window or disabling the add-on while Retopo Draw is running now cleans up properly: no leftover viewport drawing, status bar text or stuck "on" state.
- Your own snapping settings are kept in the file while Live Conform overrides them, so saving and reopening in the middle no longer loses them. Live Conform also works when "Exclude Non-Selectable" snapping is on and the reference is locked.
- Disabling the add-on unlocks a locked reference, so the high poly isn't left unclickable.
- Numpad Enter exits the mode, like Enter.
- Shift+D and Alt+D work as usual while the mode is on (D on its own still draws a pen loop). A Tab or D released outside the viewport no longer stays "held".
- A clear message when the reference was deleted from the scene.
- A reference with negative scale no longer switches off Keep Verts on Surface.

## 0.8.4
- Shift fill prefers quads that use your dots and refuses thin sliver quads.
- The extension id and folder are now `om3d_retopo_draw`, to match the name. Blender sees this as a new add-on, so remove the old `om3d_topo_draw` install first. Settings and keymaps start fresh.

## 0.8.3
- With Mirror on, the open border along the symmetry line is no longer treated as a hole.

## 0.8.2
- Shift fill inside a hole can close a one-quad notch.

## 0.8.1
- Shift fill with a dot inside a hole builds the dot's quad instead of offering Grid Fill.

## 0.8.0
- Renamed to OM3D Retopo Draw (previously OM3D Topo Draw). The extension id stays `om3d_topo_draw`.

## 0.7.x
- Lock Reference toggle.
- B, B bridges the facing parts of two borders, edge counts don't need to match.
- Retopo Overlay: the tool's look outside the mode, with a viewport header toggle, and Blender's own selection left visible in Edit Mode.
- Shift+click inside a hole fills it (one quad or Grid Fill).
- Ctrl+Shift highlights only the edge loop.
- Live Conform in plain Edit Mode, Conform Selected, Bridge Selected.
- Ctrl+Shift+LMB deletes, Ctrl+Shift+MMB moves edge loops.

## 0.6.0
- Mirror, live snapping to verts and edges, surface offset, Tab+MMB loop extend.

## 0.5.0
- Quad-draw parity pass and a Grid Fill layout fix.

## 0.4.0
- Edge and face drag, edge delete, Keep Verts on Surface, backface culling.

## 0.3.x
- Grid Fill, Bridge, Pen loops and strips, face and edge color and opacity.

## 0.2.0
- Loop extend, loop insert, relax, Clear Dots, Alt as extend key, native undo.

## 0.1.x
- Dots, Shift fill, Tab extend.
