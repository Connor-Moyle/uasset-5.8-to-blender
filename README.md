# UAsset Bridge — `.uasset` ⇄ Blender for Unreal Engine 5.8

Import and export Unreal Engine `.uasset` files directly from Blender's **File** menu, including
**materials, textures, skeletons/bones, skin weights, animations and every separate part of a model**.

- **File ▸ Import ▸ Unreal Asset (.uasset)**
- **File ▸ Export ▸ Unreal Asset (.uasset)**

Tested with Blender 5.2. Requires Unreal Engine 5.8 (installed) and an Unreal project.

## How it works

`.uasset` is an undocumented, engine-version-specific format that depends on the project it
belongs to. No standalone tool can write valid 5.8 assets, so UAsset Bridge runs **Unreal Editor
headless in the background** and lets Unreal read and write its own files:

```
Import:  .uasset ─▶ Unreal (headless) ─▶ FBX parts + PNG textures + material data ─▶ Blender
Export:  Blender ─▶ FBX parts + PNG textures + material data ─▶ Unreal (headless) ─▶ .uasset
```

## What comes across

| | Import (Unreal → Blender) | Export (Blender → Unreal) |
|---|---|---|
| **Meshes** | Static Mesh, Skeletal Mesh (optionally LODs and collision) | Static Mesh, Skeletal Mesh |
| **Bones / rigging** | Full skeleton, skin weights, morph targets, sockets | Armature → Skeleton + Physics Asset, skin weights, shape keys |
| **Materials** | Rebuilt as Principled BSDF: base color, normal, roughness, metallic, AO/ORM packed maps, emissive, opacity, two-sided. Reads material instance parameters too. | Principled BSDF → new Unreal Material with the same textures wired up, assigned to the matching slots |
| **Textures** | Exported as PNG and packed into the .blend | Saved/copied and imported as Texture2D with the right sRGB / normal-map / mask settings |
| **Separate parts** | Other skeletal meshes on the same skeleton (e.g. head, body, hands, armour) are merged onto **one** rig. **Blueprints**: every mesh component is placed with its transform, hierarchy and material overrides. | **Combine** into one asset, or **one asset per object** (skeletal parts share one Skeleton) |
| **Animations** | Animation Sequences for the skeleton become Actions (first one active, others on muted NLA tracks) | Every Action → Animation Sequence |
| **Textures alone** | Texture2D → Blender image | — |

Normal maps are converted between DirectX (Unreal) and OpenGL (Blender) automatically in both directions.

## Install

1. Download `uasset_bridge.zip` from the [latest release](../../releases/latest).
2. In Blender go to **Edit ▸ Preferences ▸ Get Extensions ▸ ⌄ (top-right) ▸ Install from Disk…** and pick the zip.
3. Expand the add-on in **Preferences ▸ Add-ons** and set:
   - **Unreal Engine Folder**, e.g. `C:\Program Files\Epic Games\UE_5.8`
   - **Unreal Project**, e.g. `D:\MyGame\MyGame.uproject`
4. In Unreal, make sure the project has **Python Editor Script Plugin** enabled (Edit ▸ Plugins). It's on by default in UE5.

After installing, Blender keeps the add-on in:

| OS | Folder |
|---|---|
| Windows | `%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\uasset_bridge` |
| macOS | `~/Library/Application Support/Blender/5.2/extensions/user_default/uasset_bridge` |
| Linux | `~/.config/blender/5.2/extensions/user_default/uasset_bridge` |

## Import options

| Option | Default | Meaning |
|---|---|---|
| Materials & Textures | On | Rebuild Unreal materials with their textures |
| Extra Parts | Same Folder | Other skeletal meshes on the same skeleton: *None*, *Same Folder* (incl. sub-folders), *Whole Project* |
| Animations | Same Folder | Animations for the skeleton, same scopes as above |
| Max Animations | 50 | Safety cap (e.g. the Mannequin skeleton has hundreds) |
| Automatic Bone Orientation | Off | Nicer-looking bones in Blender. Leave **off** if you'll send the rig back to Unreal |
| Include LODs / Collision | Off | Bring LOD meshes / collision hulls as extra objects |

## Export options

| Option | Default | Meaning |
|---|---|---|
| Selected Objects Only | On | Selecting a mesh automatically includes its armature, and vice versa |
| Asset Type | Auto | Skeletal if there's an armature, else static. *Animation Only* needs **Existing Skeleton** |
| Separate Parts | Combine | *Combine Into One Asset* or *One Asset Per Object* |
| Existing Skeleton | — | Bind to an existing Unreal skeleton, e.g. `/Game/Characters/Mannequins/Meshes/SK_Mannequin` |
| Materials & Textures | On | Create Unreal materials/textures from your Principled BSDF materials |
| Animations | On | Export all Actions as Animation Sequences |

**Where exported assets go:** if you save inside `<Project>/Content/`, assets are created at that
path, with materials in `Materials/` and textures in `Textures/` next to them. If you save
anywhere else, they're created in `/Game/BlenderExports/` and the `.uasset` files are copied to
the folder you chose. Copied files still reference that project's assets.

## Good to know

- **Close the Unreal Editor** for that project first, otherwise files may be locked.
- Each import/export starts Unreal in the background. That takes about 20–60 s, and Blender
  freezes until it's done.
- `.uasset` files from **outside** the project are copied into `Content/__BlenderBridge/Staged/` to
  load them. They must be **editor (uncooked)** assets from UE ≤ 5.8. Files ripped from a
  packaged/shipped game (`.uasset` + `.uexp`/`.ubulk`) can't be opened by the editor.
- Materials are translated to a standard PBR setup. Complex Unreal shader graphs (custom
  nodes, world-position effects, layered materials) and **procedural Blender node setups**
  (noise textures, etc.) don't map across. Bake procedural materials to image textures before exporting.
- Texture roles are detected from material parameter names (`BaseColor`, `Normal`, `Roughness`…)
  and common suffixes (`_D`, `_BC`, `_N`, `_R`, `_M`, `_E`, `_ORM`, `_ARM`, `_RMA`…).
- On errors the message includes the path to `unreal.log` in your temp folder.

## Use without Blender's UI

`bridge.py` has no Blender dependency:

```python
import bridge
ue, proj = r"C:\Program Files\Epic Games\UE_5.8", r"D:\MyGame\MyGame.uproject"

# .uasset -> FBX + PNG textures + materials JSON in ./out
result, log = bridge.uasset_to_blender(ue, proj, r"D:\MyGame\Content\Props\SM_Chair.uasset", out_dir="out")

# FBX -> .uasset
bridge.blender_to_uasset(ue, proj, [{"fbx": "chair.fbx", "name": "SM_Chair", "mesh_type": "STATIC"}],
                         target_uasset=r"D:\MyGame\Content\Props\SM_Chair.uasset")
```

## Files

| File | Runs in | Purpose |
|---|---|---|
| `__init__.py` | Blender | Menu entries, import/export operators, rebuilding parts/rigs/animations |
| `materials.py` | Blender | Principled BSDF ⇄ Unreal material translation |
| `bridge.py` | Any Python | Finds Unreal, maps paths, runs the headless job |
| `unreal_scripts/uasset_job.py` | Unreal Editor | Does the actual `.uasset` reading/writing |

## License

GPL-3.0-or-later
