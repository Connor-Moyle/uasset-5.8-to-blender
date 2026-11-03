"""
UAsset Bridge - import / export Unreal Engine 5 .uasset files in Blender.

Unreal's .uasset format is undocumented, engine-version specific and depends on the
project it lives in, so this add-on drives a headless Unreal Editor to do the actual
.uasset reading and writing (FBX + PNG are used as the hand-off formats):

  Import:  .uasset --(UnrealEditor-Cmd)--> FBX parts + PNG textures + material data --> Blender
  Export:  Blender --> FBX parts + PNG textures + material data --(UnrealEditor-Cmd)--> .uasset
"""

import math
import os
import re
import tempfile

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import bridge, materials


# --------------------------------------------------------------------------- prefs
class UASSET_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    engine_dir: StringProperty(
        name="Unreal Engine Folder",
        description="Engine install root, e.g. C:/Program Files/Epic Games/UE_5.8 "
                    "(or the UnrealEditor-Cmd executable itself)",
        subtype="DIR_PATH",
    )
    uproject: StringProperty(
        name="Unreal Project (.uproject)",
        description="Project used to load/save assets. It must have the 'Python Editor Script "
                    "Plugin' enabled (on by default in UE5)",
        subtype="FILE_PATH",
    )
    default_destination: StringProperty(
        name="Default Content Path",
        description="Where exported assets go when the target file is outside the project",
        default="/Game/BlenderExports",
    )

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "engine_dir")
        col.prop(self, "uproject")
        col.prop(self, "default_destination")
        try:
            exe = bridge.find_editor_cmd(self.engine_dir)
            col.label(text="Found: " + exe, icon="CHECKMARK")
        except bridge.BridgeError as e:
            col.label(text=str(e), icon="ERROR")


def get_prefs(context):
    return context.preferences.addons[__package__].preferences


SCOPE_ITEMS = [
    ("NONE", "None", "Only the asset you picked"),
    ("FOLDER", "Same Folder", "Assets on the same skeleton in the same folder (and sub-folders)"),
    ("ALL", "Whole Project", "Every asset in the project that uses the same skeleton"),
]


# --------------------------------------------------------------------------- import helpers
def _import_fbx(path, op):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(
        filepath=path,
        automatic_bone_orientation=op.automatic_bone_orientation,
        ignore_leaf_bones=op.ignore_leaf_bones,
        use_anim=True,
        use_image_search=False,
    )
    return [o for o in bpy.data.objects if o not in before]


def _base_name(name):
    return re.sub(r"\.\d{3}$", "", name)


def _apply_part_materials(objs, part, mat_infos, built, image_cache, overrides=None):
    """Replace FBX placeholder materials with full ones rebuilt from Unreal's data."""
    slot_names = [s.get("material") for s in part.get("slots", [])]
    for ob in objs:
        if ob.type != "MESH":
            continue
        for i, slot in enumerate(ob.material_slots):
            src = slot.material
            key = None
            if overrides and str(i) in overrides:
                key = overrides[str(i)]
            elif src and _base_name(src.name) in mat_infos:
                key = _base_name(src.name)
            elif i < len(slot_names) and slot_names[i] in mat_infos:
                key = slot_names[i]
            if not key:
                continue
            if key not in built:
                m = bpy.data.materials.get(key)
                if m is None or m.get("uasset_bridge_source") is None:
                    m = src if (src and _base_name(src.name) == key) else bpy.data.materials.new(key)
                materials.build_material(m, mat_infos[key], image_cache)
                built[key] = m
            slot.material = built[key]


def _armature_of(objs):
    return next((o for o in objs if o.type == "ARMATURE"), None)


def _delete(objs):
    datas = [o.data for o in objs if o.data is not None]
    for o in objs:
        bpy.data.objects.remove(o, do_unlink=True)
    for d in datas:
        if d.users == 0:
            if isinstance(d, bpy.types.Mesh):
                bpy.data.meshes.remove(d)
            elif isinstance(d, bpy.types.Armature):
                bpy.data.armatures.remove(d)


def _merge_onto_armature(objs, main_arm):
    """Re-parent a part's meshes to the main armature if it has all the bones they need."""
    arm = _armature_of(objs)
    if arm is None or main_arm is None or arm is main_arm:
        return objs
    main_bones = set(main_arm.data.bones.keys())
    if not set(arm.data.bones.keys()) <= main_bones:
        return objs                                  # extra bones -> keep its own rig
    keep = []
    for o in objs:
        if o is arm:
            continue
        if o.parent is arm:
            mw = o.matrix_world.copy()
            o.parent = main_arm
            o.matrix_world = mw
        for mod in getattr(o, "modifiers", []):
            if mod.type == "ARMATURE" and mod.object is arm:
                mod.object = main_arm
        keep.append(o)
    # remove the duplicate armature and any helper empties it brought
    junk = [o for o in keep if o.type == "EMPTY" and not o.children]
    _delete([arm] + junk)
    return [o for o in keep if o not in junk]


def _assign_action(arm, action):
    arm.animation_data_create()
    arm.animation_data.action = action
    if hasattr(arm.animation_data, "action_slot") and getattr(action, "slots", None):
        try:
            arm.animation_data.action_slot = action.slots[0]
        except (TypeError, RuntimeError):
            pass


def _import_animation(anim, op, main_arm):
    objs = _import_fbx(anim["fbx"], op)
    act = None
    for o in objs:
        ad = o.animation_data
        if o.type == "ARMATURE" and ad and ad.action:
            act = ad.action
            break
    if act:
        act.name = anim["name"]
        act.use_fake_user = True
    _delete(objs)
    if act and main_arm is not None:
        if not (main_arm.animation_data and main_arm.animation_data.action):
            _assign_action(main_arm, act)
        else:   # keep extra animations on NLA tracks (muted) so they are easy to find
            track = main_arm.animation_data.nla_tracks.new()
            track.name = act.name
            try:
                track.strips.new(act.name, int(act.frame_range[0]), act)
            except (RuntimeError, TypeError):
                pass
            track.mute = True
    return act


def _ue_to_blender_transform(empty, comp):
    x, y, z = comp["location"]
    roll, pitch, yaw = comp["rotation"]
    empty.location = (x / 100.0, -y / 100.0, z / 100.0)       # cm, left-handed -> m, right-handed
    empty.rotation_mode = "XYZ"
    empty.rotation_euler = (math.radians(roll), math.radians(-pitch), math.radians(-yaw))
    empty.scale = comp["scale"]


# --------------------------------------------------------------------------- import
class IMPORT_OT_uasset(bpy.types.Operator, ImportHelper):
    """Import an Unreal Engine .uasset with its materials, textures, skeleton, parts and animations"""
    bl_idname = "import_scene.uasset"
    bl_label = "Import UAsset"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".uasset"
    filter_glob: StringProperty(default="*.uasset", options={"HIDDEN"})

    import_materials: BoolProperty(
        name="Materials & Textures", default=True,
        description="Rebuild the Unreal materials as Principled BSDF with their textures")
    parts_scope: EnumProperty(
        name="Extra Parts", items=SCOPE_ITEMS, default="FOLDER",
        description="Skeletal meshes: also bring in other meshes on the same skeleton "
                    "(e.g. head/body/hands of a modular character)")
    anim_scope: EnumProperty(
        name="Animations", items=SCOPE_ITEMS, default="FOLDER",
        description="Skeletal meshes: also bring in animations made for the skeleton")
    max_animations: IntProperty(name="Max Animations", default=50, min=0, max=1000)
    automatic_bone_orientation: BoolProperty(
        name="Automatic Bone Orientation", default=False,
        description="Nicer-looking bones in Blender. Leave OFF if you will send the rig back to Unreal")
    ignore_leaf_bones: BoolProperty(name="Ignore Leaf Bones", default=True)
    export_lods: BoolProperty(name="Include LODs", default=False)
    export_collision: BoolProperty(name="Include Collision", default=False)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "import_materials")
        box = col.box()
        box.label(text="Skeletal meshes", icon="ARMATURE_DATA")
        box.prop(self, "parts_scope")
        box.prop(self, "anim_scope")
        box.prop(self, "max_animations")
        box.prop(self, "automatic_bone_orientation")
        box.prop(self, "ignore_leaf_bones")
        col.prop(self, "export_lods")
        col.prop(self, "export_collision")

    def execute(self, context):
        prefs = get_prefs(context)
        src = self.filepath
        wm = context.window_manager
        wm.progress_begin(0, 1)
        try:
            result, log_path = bridge.uasset_to_blender(
                prefs.engine_dir, prefs.uproject, src,
                out_dir=tempfile.mkdtemp(prefix="uasset_import_"),
                parts_scope=self.parts_scope, anim_scope=self.anim_scope,
                max_animations=self.max_animations,
                export_lods=self.export_lods, export_collision=self.export_collision,
            )
        except bridge.BridgeError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        finally:
            wm.progress_end()

        if result.get("texture"):
            img = bpy.data.images.load(result["texture"], check_existing=True)
            if any(r in materials.NON_COLOR_ROLES for r in result.get("texture_roles", {})):
                img.colorspace_settings.name = "Non-Color"
            img.pack()
            self.report({"INFO"}, "Imported texture '%s' (see Image Editor)" % img.name)
            return {"FINISHED"}

        mat_infos = result.get("materials", {}) if self.import_materials else {}
        built, image_cache = {}, {}
        coll = bpy.data.collections.new(result.get("name", "UAsset"))
        context.scene.collection.children.link(coll)

        def into_collection(objs):
            for o in objs:
                for c in list(o.users_collection):
                    c.objects.unlink(o)
                coll.objects.link(o)

        part_objs = {}
        main_arm = None
        components = result.get("components") or []

        if components:
            # Blueprint: one empty per component, mesh parts placed under them.
            empties = {}
            for comp in components:
                e = bpy.data.objects.new(comp["name"], None)
                e.empty_display_size = 0.2
                coll.objects.link(e)
                _ue_to_blender_transform(e, comp)
                empties[comp["name"]] = e
            for comp in components:
                if comp["parent"] in empties:
                    empties[comp["name"]].parent = empties[comp["parent"]]
            for comp in components:
                if comp["part"] is None:
                    continue
                part = result["parts"][comp["part"]]
                objs = _import_fbx(part["fbx"], self)
                into_collection(objs)
                _apply_part_materials(objs, part, mat_infos, built, image_cache, comp.get("overrides"))
                for o in objs:
                    if o.parent is None:
                        o.parent = empties[comp["name"]]
                        o.matrix_parent_inverse.identity()
        else:
            for i, part in enumerate(result.get("parts", [])):
                objs = _import_fbx(part["fbx"], self)
                if i == 0:
                    main_arm = _armature_of(objs)
                elif part["kind"] == "skeletal":
                    objs = _merge_onto_armature(objs, main_arm)
                into_collection(objs)
                _apply_part_materials(objs, part, mat_infos, built, image_cache)
                part_objs[part["name"]] = objs

        for anim in result.get("animations", []):
            if main_arm is None:      # importing an AnimSequence on its own: keep its armature
                objs = _import_fbx(anim["fbx"], self)
                into_collection(objs)
                main_arm = _armature_of(objs)
                if main_arm and main_arm.animation_data and main_arm.animation_data.action:
                    main_arm.animation_data.action.name = anim["name"]
                    main_arm.animation_data.action.use_fake_user = True
            else:
                _import_animation(anim, self, main_arm)

        n_parts = len(result.get("parts", []))
        self.report({"INFO"}, "Imported %s: %d part(s), %d material(s), %d animation(s)" % (
            result.get("name"), n_parts, len(built), len(result.get("animations", []))))
        return {"FINISHED"}


# --------------------------------------------------------------------------- export helpers
def _mesh_armature(ob):
    for mod in ob.modifiers:
        if mod.type == "ARMATURE" and mod.object:
            return mod.object
    if ob.parent and ob.parent.type == "ARMATURE":
        return ob.parent
    return None


def _fbx_export(path, objects, bake_anim):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objects:
        o.select_set(True)
    bpy.ops.export_scene.fbx(
        filepath=path,
        use_selection=True,
        apply_unit_scale=True,
        apply_scale_options="FBX_SCALE_UNITS",
        axis_forward="-Z",
        axis_up="Y",
        mesh_smooth_type="FACE",
        use_tspace=True,
        use_mesh_modifiers=True,
        add_leaf_bones=False,
        use_armature_deform_only=True,
        bake_anim=bake_anim,
        bake_anim_use_all_actions=True,
        bake_anim_use_nla_strips=False,
        path_mode="AUTO",
        embed_textures=False,
    )


def _material_names(objects):
    names = []
    for o in objects:
        if o.type == "MESH":
            for s in o.material_slots:
                if s.material and s.material.name not in names:
                    names.append(s.material.name)
    return names


# --------------------------------------------------------------------------- export
class EXPORT_OT_uasset(bpy.types.Operator, ExportHelper):
    """Export to Unreal Engine .uasset with materials, textures, skeleton, parts and animations"""
    bl_idname = "export_scene.uasset"
    bl_label = "Export UAsset"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".uasset"
    filter_glob: StringProperty(default="*.uasset", options={"HIDDEN"})

    use_selection: BoolProperty(name="Selected Objects Only", default=True)
    mesh_type: EnumProperty(
        name="Asset Type",
        items=[
            ("AUTO", "Auto Detect", "Skeletal if an armature is exported, else static"),
            ("STATIC", "Static Mesh", ""),
            ("SKELETAL", "Skeletal Mesh", ""),
            ("ANIMATION", "Animation Only", "Requires an existing Skeleton asset"),
        ],
        default="AUTO",
    )
    parts: EnumProperty(
        name="Separate Parts",
        items=[
            ("COMBINE", "Combine Into One Asset", "All objects become one mesh asset (each material keeps its own slot)"),
            ("SEPARATE", "One Asset Per Object", "Each mesh object becomes its own asset; skeletal parts share one Skeleton"),
        ],
        default="COMBINE",
    )
    skeleton_path: StringProperty(
        name="Existing Skeleton",
        description="Optional Unreal skeleton to bind to, e.g. /Game/Characters/SK_Mannequin_Skeleton",
    )
    export_materials: BoolProperty(
        name="Materials & Textures", default=True,
        description="Create Unreal materials and textures from the Principled BSDF materials")
    bake_anim: BoolProperty(name="Animations (all actions)", default=True)
    auto_collision: BoolProperty(name="Generate Collision", default=True)

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = bpy.path.clean_name(
                (context.active_object.name if context.active_object else "Asset")) + ".uasset"
        return super().invoke(context, event)

    def execute(self, context):
        prefs = get_prefs(context)
        objs = list(context.selected_objects if self.use_selection else context.scene.objects)
        # include the rig of any selected skinned mesh, and meshes of any selected rig
        for o in list(objs):
            arm = _mesh_armature(o) if o.type == "MESH" else None
            if arm and arm not in objs:
                objs.append(arm)
            if o.type == "ARMATURE":
                for c in context.scene.objects:
                    if c.type == "MESH" and _mesh_armature(c) is o and c not in objs:
                        objs.append(c)
        meshes = [o for o in objs if o.type == "MESH"]
        arms = [o for o in objs if o.type == "ARMATURE"]
        if not meshes and not (arms and self.mesh_type == "ANIMATION"):
            self.report({"ERROR"}, "Nothing to export: select mesh objects (and their armature)")
            return {"CANCELLED"}

        mesh_type = self.mesh_type
        if mesh_type == "AUTO":
            mesh_type = "SKELETAL" if arms else "STATIC"

        if context.object and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        prev_sel = list(context.selected_objects)
        prev_active = context.view_layer.objects.active

        base = bridge.sanitize_asset_name(os.path.splitext(os.path.basename(self.filepath))[0])
        tmp = tempfile.mkdtemp(prefix="uasset_export_")
        items = []
        try:
            if self.parts == "COMBINE" or mesh_type == "ANIMATION" or len(meshes) <= 1:
                path = os.path.join(tmp, base + ".fbx")
                _fbx_export(path, objs, self.bake_anim and bool(arms))
                items.append({"fbx": path, "name": base, "mesh_type": mesh_type,
                              "materials": _material_names(meshes),
                              "combine_meshes": True, "animations": self.bake_anim})
            else:
                for i, m in enumerate(meshes):
                    name = base + "_" + bridge.sanitize_asset_name(m.name)
                    path = os.path.join(tmp, name + ".fbx")
                    if mesh_type == "SKELETAL":
                        arm = _mesh_armature(m)
                        _fbx_export(path, [m] + ([arm] if arm else []), self.bake_anim and i == 0 and bool(arm))
                    else:
                        saved = m.matrix_world.copy()          # pivot at the object's origin
                        m.matrix_world.translation = (0.0, 0.0, 0.0)
                        try:
                            _fbx_export(path, [m], False)
                        finally:
                            m.matrix_world = saved
                    items.append({"fbx": path, "name": name, "mesh_type": mesh_type,
                                  "materials": _material_names([m]),
                                  "combine_meshes": True, "animations": self.bake_anim and i == 0})

            mat_infos = {}
            if self.export_materials:
                tex_dir, img_cache = os.path.join(tmp, "textures"), {}
                for o in meshes:
                    for s in o.material_slots:
                        if s.material and s.material.name not in mat_infos:
                            mat_infos[s.material.name] = materials.describe_material(s.material, tex_dir, img_cache)
        finally:
            bpy.ops.object.select_all(action="DESELECT")
            for o in prev_sel:
                o.select_set(True)
            context.view_layer.objects.active = prev_active

        wm = context.window_manager
        wm.progress_begin(0, 1)
        try:
            result, log_path = bridge.blender_to_uasset(
                prefs.engine_dir, prefs.uproject, items, materials=mat_infos,
                target_uasset=self.filepath,
                destination_path=prefs.default_destination,
                skeleton_path=self.skeleton_path,
                share_skeleton=True,
                auto_collision=self.auto_collision,
            )
        except bridge.BridgeError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        finally:
            wm.progress_end()

        self.report({"INFO"}, "Exported %d asset(s), %d material(s) -> %s" % (
            len(result.get("main_assets", [])), len(result.get("materials", [])),
            ", ".join(result.get("main_assets", [])) or "project"))
        return {"FINISHED"}


# --------------------------------------------------------------------------- menus
def menu_import(self, context):
    self.layout.operator(IMPORT_OT_uasset.bl_idname, text="Unreal Asset (.uasset)")


def menu_export(self, context):
    self.layout.operator(EXPORT_OT_uasset.bl_idname, text="Unreal Asset (.uasset)")


classes = (UASSET_AddonPreferences, IMPORT_OT_uasset, EXPORT_OT_uasset)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
