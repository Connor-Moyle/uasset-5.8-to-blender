First release of UAsset Bridge: import and export Unreal Engine 5.8 `.uasset` files in Blender (tested on Blender 5.2).

### Features
- **File ▸ Import / Export ▸ Unreal Asset (.uasset)**
- Static meshes, skeletal meshes, animations, textures and Blueprints
- **Materials & textures** both ways: base color, normal (DirectX⇄OpenGL handled), roughness, metallic, AO, packed ORM/ARM/RMA maps, emissive, opacity
- **Bones & rigging:** full skeleton, skin weights, morph targets/shape keys
- **Separate parts:** other meshes on the same skeleton merge onto one rig. Blueprint mesh components keep their layout and material overrides. On export, combine into one asset or make one asset per object.
- **Animations:** Animation Sequences ⇄ Actions

### Install
1. Download **uasset_bridge.zip** below.
2. Blender ▸ Edit ▸ Preferences ▸ Get Extensions ▸ ⌄ ▸ **Install from Disk…**
3. In the add-on preferences, set your UE 5.8 folder and `.uproject`.

Requires Unreal Engine 5.8 installed (runs headless in the background). See the README for details and limitations.
