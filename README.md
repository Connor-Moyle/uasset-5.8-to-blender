# uasset-5.8-to-blender
UAsset Bridge — .uasset import/export for Blender (UE 5.8)
Adds File ▸ Import ▸ Unreal Asset (.uasset) and File ▸ Export ▸ Unreal Asset (.uasset) to Blender.

.uasset is an undocumented, engine-version-specific format that depends on the project it belongs to, so the add-on runs Unreal Editor in the background to read and write it. You need Unreal Engine 5.8 installed and a .uproject to work in.

Import:  .uasset ──UnrealEditor-Cmd──▶ .fbx ──▶ Blender
Export:  Blender ──▶ .fbx ──UnrealEditor-Cmd──▶ .uasset (saved in your project)
Install
Zip this folder (or use uasset_bridge.zip).
Blender ▸ Edit ▸ Preferences ▸ Get Extensions ▸ ⌄ ▸ Install from Disk… ▸ pick the zip.
In the add-on's preferences set:
Unreal Engine Folder, e.g. C:\Program Files\Epic Games\UE_5.8
Unreal Project, e.g. D:\MyGame\MyGame.uproject
The project must have Python Editor Script Plugin enabled (Edit ▸ Plugins in Unreal).
Supported assets
Import (.uasset → Blender)	Export (Blender → .uasset)
Static Mesh, Skeletal Mesh (+skeleton), Animation Sequence, Texture2D	Static Mesh, Skeletal Mesh, Animation (onto an existing Skeleton)
Notes
Close the Unreal Editor for that project first, or saving may fail because files are locked.
Each operation starts Unreal headless, which takes about 20–60 s. Blender will freeze until it finishes.
Export target inside <Project>/Content/: the asset is created at exactly that path. Target outside the project: the asset is created in /Game/BlenderExports/ and then copied to the chosen file. Its references (materials, skeleton) still point into that project.
Importing a .uasset from outside the project: it is copied temporarily into Content/__BlenderBridge/Staged/. It must be an uncooked editor asset saved by a compatible engine version (≤ 5.8). Cooked/packaged game files (.uasset + .uexp from a shipped game) can't be opened by the editor.
Materials come through as basic materials. Unreal shader graphs don't map to Blender nodes.
Logs: errors report the path to unreal.log in your temp folder.
