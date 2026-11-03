"""
Runs INSIDE Unreal Editor (headless, via the PythonScript commandlet).

Launched by the Blender add-on like:
    UnrealEditor-Cmd <Project.uproject> -run=pythonscript -script=<this file> ...

The job description is a JSON file whose path is in the BLENDER_UASSET_JOB
environment variable. Results are written as JSON to job["result_path"].

Modes
-----
"uasset_to_blender"
    Load an asset and export everything Blender needs to rebuild it:
      * StaticMesh / SkeletalMesh  -> FBX (mesh, bones, skin weights, morph targets, sockets)
      * every material on it       -> parameters + its textures exported as PNG
      * SkeletalMesh               -> other meshes on the same skeleton in the same folder
                                      ("parts" such as head/body/hands) + animations
      * Blueprint                  -> every mesh component with its transform/hierarchy
                                      and material overrides
      * AnimSequence               -> FBX animation
      * Texture2D                  -> PNG
"blender_to_uasset"
    Import one or more FBX files plus textures, build Unreal materials from the
    Blender material description, assign them to the right slots and save .uassets.
"""

import json
import os
import re
import traceback

import unreal

MEL = unreal.MaterialEditingLibrary


def log(msg):
    unreal.log("[UAssetBridge] " + str(msg))


def warn(msg):
    unreal.log_warning("[UAssetBridge] " + str(msg))


def write_result(job, ok, **data):
    data["ok"] = ok
    with open(job["result_path"], "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _set(obj, prop, value):
    """set_editor_property that tolerates properties renamed/removed between engine versions."""
    try:
        obj.set_editor_property(prop, value)
        return True
    except Exception as e:  # noqa: BLE001
        log("note: could not set %s.%s (%s)" % (type(obj).__name__, prop, e))
        return False


def _get(obj, *props, default=None):
    for p in props:
        try:
            return obj.get_editor_property(p)
        except Exception:  # noqa: BLE001
            continue
    return default


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name)) or "Asset"


def package_of(obj):
    return obj.get_path_name().split(".")[0]


def load(path):
    a = unreal.EditorAssetLibrary.load_asset(path)
    if a is None and "." not in path.rsplit("/", 1)[-1]:
        a = unreal.EditorAssetLibrary.load_asset(path + "." + path.rsplit("/", 1)[-1])
    return a


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# =========================================================================== EXPORT
def export_object(obj, filename, job):
    """Run an AssetExportTask. Returns the written filename or None."""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    task = unreal.AssetExportTask()
    task.object = obj
    task.filename = filename
    task.automated = True
    task.prompt = False
    task.replace_identical = True
    task.selected = False

    if isinstance(obj, unreal.StaticMesh) and hasattr(unreal, "StaticMeshExporterFBX"):
        task.exporter = unreal.StaticMeshExporterFBX()
    elif isinstance(obj, unreal.SkeletalMesh) and hasattr(unreal, "SkeletalMeshExporterFBX"):
        task.exporter = unreal.SkeletalMeshExporterFBX()
    elif isinstance(obj, unreal.AnimSequence) and hasattr(unreal, "AnimSequenceExporterFBX"):
        task.exporter = unreal.AnimSequenceExporterFBX()

    if filename.lower().endswith(".fbx") and hasattr(unreal, "FbxExportOption"):
        opts = unreal.FbxExportOption()
        _set(opts, "ascii", False)
        _set(opts, "collision", bool(job.get("export_collision", False)))
        _set(opts, "level_of_detail", bool(job.get("export_lods", False)))
        _set(opts, "export_morph_targets", True)
        _set(opts, "vertex_color", True)
        _set(opts, "export_preview_mesh", False)
        _set(opts, "map_skeletal_motion_to_root", False)
        task.options = opts

    ok = unreal.Exporter.run_asset_export_task(task)
    if ok and os.path.isfile(filename):
        return filename
    warn("export failed for %s -> %s %s" % (obj.get_path_name(), filename, list(_get(task, "errors", default=[]) or [])))
    return None


# ------------------------------------------------------------------ textures
ROLE_SUFFIX = {
    "d": "base_color", "bc": "base_color", "basecolor": "base_color", "albedo": "base_color",
    "diffuse": "base_color", "diff": "base_color", "col": "base_color", "color": "base_color", "b": "base_color",
    "n": "normal", "nrm": "normal", "nor": "normal", "normal": "normal", "norm": "normal",
    "r": "roughness", "rough": "roughness", "roughness": "roughness",
    "m": "metallic", "metal": "metallic", "metallic": "metallic", "metalness": "metallic",
    "e": "emissive", "emissive": "emissive", "emission": "emissive", "glow": "emissive",
    "ao": "ao", "occlusion": "ao",
    "a": "opacity", "alpha": "opacity", "opacity": "opacity",
}
PACKED_LETTERS = {"o": "ao", "a": "ao", "r": "roughness", "m": "metallic"}


def packed_channels(token):
    """'ORM' -> {'R':'ao','G':'roughness','B':'metallic'}; returns None if not a packed token."""
    t = token.lower()
    if t == "mrao":
        t = "mra"
    if len(t) == 3 and all(c in PACKED_LETTERS for c in t) and len(set(t)) == 3:
        return {ch: PACKED_LETTERS[c] for ch, c in zip("RGB", t)}
    return None


def guess_roles(param_name, tex):
    """Return {'role': channel} for a texture, e.g. {'base_color':'RGB'} or ORM channel mapping."""
    p = (param_name or "").lower()
    tex_name = tex.get_name()
    tokens = [t for t in re.split(r"[_\s\-]+", tex_name) if t]
    suffix = tokens[-1] if len(tokens) > 1 else ""
    comp = _get(tex, "compression_settings")

    # Packed masks (ORM / ARM / RMA / MRA...) from either the parameter or texture name.
    for tok in [p.replace("map", "").replace("tex", "").replace("texture", "").strip(), suffix]:
        m = packed_channels(tok)
        if m:
            return {role: ch for ch, role in m.items()}

    if p:
        if "normal" in p:
            return {"normal": "RGB"}
        if "emiss" in p or "emit" in p or "glow" in p:
            return {"emissive": "RGB"}
        if "rough" in p:
            return {"roughness": "R"}
        if "metal" in p:
            return {"metallic": "R"}
        if "opacity" in p or "alpha" in p:
            return {"opacity": "R"}
        if "occlusion" in p or p in ("ao", "aomap", "ambientocclusion"):
            return {"ao": "R"}
        if "mask" in p or "packed" in p:
            return {"ao": "R", "roughness": "G", "metallic": "B"}
        if any(k in p for k in ("basecolor", "base color", "albedo", "diffuse", "color", "colour")):
            return {"base_color": "RGB"}

    if comp == unreal.TextureCompressionSettings.TC_NORMALMAP:
        return {"normal": "RGB"}
    role = ROLE_SUFFIX.get(suffix.lower())
    if role:
        return {role: "RGB" if role in ("base_color", "normal", "emissive") else "R"}
    if comp == unreal.TextureCompressionSettings.TC_MASKS:
        return {"ao": "R", "roughness": "G", "metallic": "B"}
    return {}


def export_texture(tex, out_dir, cache, job):
    path = tex.get_path_name()
    if path in cache:
        return cache[path]
    base = os.path.join(out_dir, "textures", safe_name(tex.get_name()))
    written = export_object(tex, base + ".png", job) or export_object(tex, base + ".tga", job)
    cache[path] = written
    return written


# ------------------------------------------------------------------ materials
def collect_material(mi, out_dir, materials, tex_cache, job):
    """Describe a MaterialInterface for Blender. Returns its name (key in `materials`)."""
    if mi is None:
        return None
    name = mi.get_name()
    if name in materials:
        return name

    info = {"path": mi.get_path_name(), "textures": {}, "blend": "OPAQUE", "two_sided": False}
    materials[name] = info

    base = None
    try:
        base = mi.get_base_material()
    except Exception:  # noqa: BLE001
        pass
    if base is not None:
        bm = _get(base, "blend_mode")
        if bm == unreal.BlendMode.BLEND_MASKED:
            info["blend"] = "MASKED"
        elif bm in (unreal.BlendMode.BLEND_TRANSLUCENT, getattr(unreal.BlendMode, "BLEND_ADDITIVE", None)):
            info["blend"] = "BLEND"
        info["two_sided"] = bool(_get(base, "two_sided", default=False))

    candidates = []          # (param name, texture), most specific first
    scalars, vectors = {}, {}

    m = mi
    while isinstance(m, unreal.MaterialInstance):
        for tpv in _get(m, "texture_parameter_values", default=[]) or []:
            candidates.append((str(tpv.parameter_info.name), tpv.parameter_value))
        for spv in _get(m, "scalar_parameter_values", default=[]) or []:
            scalars.setdefault(str(spv.parameter_info.name), spv.parameter_value)
        for vpv in _get(m, "vector_parameter_values", default=[]) or []:
            c = vpv.parameter_value
            vectors.setdefault(str(vpv.parameter_info.name), [c.r, c.g, c.b, c.a])
        m = _get(m, "parent")

    if base is not None:
        try:
            for pname in MEL.get_texture_parameter_names(base):
                candidates.append((str(pname), MEL.get_material_default_texture_parameter_value(base, pname)))
        except Exception:  # noqa: BLE001
            pass
        try:
            for t in MEL.get_used_textures(base):
                candidates.append(("", t))
        except Exception:  # noqa: BLE001
            pass

    for pname, tex in candidates:
        if tex is None or not isinstance(tex, unreal.Texture2D):
            continue
        if tex.get_path_name().startswith("/Engine/"):
            continue                      # engine defaults (white, flat normal...)
        roles = guess_roles(pname, tex)
        if not roles or all(r in info["textures"] for r in roles):
            continue
        f = export_texture(tex, out_dir, tex_cache, job)
        if not f:
            continue
        for role, ch in roles.items():
            info["textures"].setdefault(role, {"file": f, "channel": ch, "param": pname,
                                               "texture": tex.get_path_name()})

    for k, v in scalars.items():
        kl = k.lower()
        if "rough" in kl and "roughness" not in info:
            info["roughness"] = v
        elif "metal" in kl and "metallic" not in info:
            info["metallic"] = v
        elif "emissive" in kl and ("strength" in kl or "intensity" in kl or "power" in kl):
            info["emission_strength"] = v
    for k, v in vectors.items():
        kl = k.lower()
        if "emiss" in kl:
            info.setdefault("emission", v)
        elif any(s in kl for s in ("color", "colour", "tint", "albedo")):
            info.setdefault("base_color", v)
    return name


def mesh_slots(mesh):
    if isinstance(mesh, unreal.StaticMesh):
        mats = _get(mesh, "static_materials", default=[]) or []
    else:
        mats = _get(mesh, "materials", default=[]) or []
    out = []
    for m in mats:
        out.append({"slot": str(_get(m, "material_slot_name", default="")),
                    "material": _get(m, "material_interface")})
    return out


def export_mesh_part(mesh, out_dir, result, materials, tex_cache, job, seen):
    """Export one Static/Skeletal mesh as a part. Returns index in result['parts']."""
    key = mesh.get_path_name()
    if key in seen:
        return seen[key]
    fbx = export_object(mesh, os.path.join(out_dir, safe_name(mesh.get_name()) + ".fbx"), job)
    if not fbx:
        raise RuntimeError("Could not export %s to FBX" % key)
    slots = []
    for s in mesh_slots(mesh):
        slots.append({"slot": s["slot"],
                      "material": collect_material(s["material"], out_dir, materials, tex_cache, job)})
    part = {"name": mesh.get_name(), "path": key, "fbx": fbx,
            "kind": "skeletal" if isinstance(mesh, unreal.SkeletalMesh) else "static", "slots": slots}
    if isinstance(mesh, unreal.SkeletalMesh):
        skel = _get(mesh, "skeleton")
        part["skeleton"] = skel.get_path_name() if skel else None
    result["parts"].append(part)
    seen[key] = len(result["parts"]) - 1
    return seen[key]


# ------------------------------------------------------------------ related assets
def asset_registry():
    return unreal.AssetRegistryHelpers.get_asset_registry()


def referencers(package_name):
    try:
        opts = unreal.AssetRegistryDependencyOptions()
        return [str(p) for p in asset_registry().get_referencers(package_name, opts) or []]
    except Exception as e:  # noqa: BLE001
        warn("asset registry query failed: %s" % e)
        return []


def skeleton_family(mesh, job):
    """Other SkeletalMeshes and AnimSequences that use this mesh's skeleton."""
    skel = _get(mesh, "skeleton")
    if skel is None:
        return [], []
    mesh_folder = package_of(mesh).rsplit("/", 1)[0]
    parts_scope = job.get("parts_scope", "FOLDER")      # NONE | FOLDER | ALL
    anim_scope = job.get("anim_scope", "FOLDER")        # NONE | FOLDER | ALL
    max_anims = int(job.get("max_animations", 50))
    meshes, anims = [], []
    for pkg in sorted(referencers(package_of(skel))):
        in_folder = pkg.startswith(mesh_folder + "/")
        a = None
        if (parts_scope == "ALL" or (parts_scope == "FOLDER" and in_folder)) or \
           (anim_scope == "ALL" or (anim_scope == "FOLDER" and in_folder)):
            a = load(pkg)
        if a is None:
            continue
        if isinstance(a, unreal.SkeletalMesh) and a.get_path_name() != mesh.get_path_name():
            if parts_scope == "ALL" or (parts_scope == "FOLDER" and in_folder):
                meshes.append(a)
        elif isinstance(a, unreal.AnimSequence):
            if (anim_scope == "ALL" or (anim_scope == "FOLDER" and in_folder)) and len(anims) < max_anims:
                anims.append(a)
    return meshes, anims


def blueprint_components(bp):
    """Yield dicts describing a Blueprint's scene components (incl. inherited ones)."""
    sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    lib = unreal.SubobjectDataBlueprintFunctionLibrary
    handles = list(sds.k2_gather_subobject_data_for_blueprint(bp))
    datas = [lib.get_data(h) for h in handles]

    def obj_of(d):
        if hasattr(lib, "get_object_for_blueprint"):
            try:
                o = lib.get_object_for_blueprint(d, bp)
                if o is not None:
                    return o
            except Exception:  # noqa: BLE001
                pass
        return lib.get_object(d)

    comps = []
    for h, d in zip(handles, datas):
        o = obj_of(d)
        if o is None or not isinstance(o, unreal.SceneComponent):
            continue
        name = str(lib.get_variable_name(d)) or o.get_name()
        comps.append({"handle": h, "data": d, "obj": o, "name": name})

    # Parent = the attached-to ancestor that itself has the most ancestors.
    for c in comps:
        anc = []
        for other in comps:
            if other is c:
                continue
            try:
                if lib.is_attached_to(c["data"], other["handle"]):
                    anc.append(other)
            except Exception:  # noqa: BLE001
                pass
        c["ancestors"] = anc
    for c in comps:
        c["parent"] = max(c["ancestors"], key=lambda a: len(a["ancestors"]))["name"] if c["ancestors"] else None
    return comps


# ------------------------------------------------------------------ main export
def uasset_to_blender(job):
    package_path = job["package_path"]
    out_dir = job["output_dir"]
    os.makedirs(out_dir, exist_ok=True)

    asset = load(package_path)
    if asset is None:
        raise RuntimeError(
            "Could not load asset %s. Is the .uasset an editor (uncooked) asset from a compatible "
            "engine version, and are its dependencies in this project?" % package_path)
    asset_class = asset.get_class().get_name()
    log("Loaded %s (%s)" % (package_path, asset_class))

    result = {"asset_class": asset_class, "name": asset.get_name(), "parts": [], "components": [],
              "animations": [], "materials": {}}
    materials, tex_cache, seen = result["materials"], {}, {}

    if isinstance(asset, unreal.Texture2D):
        f = export_texture(asset, out_dir, tex_cache, job)
        if not f:
            raise RuntimeError("Could not export texture")
        result["texture"] = f
        result["texture_roles"] = guess_roles("", asset)

    elif isinstance(asset, (unreal.StaticMesh, unreal.SkeletalMesh)):
        export_mesh_part(asset, out_dir, result, materials, tex_cache, job, seen)
        if isinstance(asset, unreal.SkeletalMesh):
            extra_meshes, anims = skeleton_family(asset, job)
            for m in extra_meshes:
                export_mesh_part(m, out_dir, result, materials, tex_cache, job, seen)
            for a in anims:
                f = export_object(a, os.path.join(out_dir, "anims", safe_name(a.get_name()) + ".fbx"), job)
                if f:
                    result["animations"].append({"name": a.get_name(), "fbx": f})

    elif isinstance(asset, unreal.AnimSequence):
        f = export_object(asset, os.path.join(out_dir, "anims", safe_name(asset.get_name()) + ".fbx"), job)
        if not f:
            raise RuntimeError("Could not export animation")
        result["animations"].append({"name": asset.get_name(), "fbx": f})

    elif isinstance(asset, unreal.Blueprint):
        for c in blueprint_components(asset):
            o = c["obj"]
            loc = _get(o, "relative_location")
            rot = _get(o, "relative_rotation")
            scl = _get(o, "relative_scale3d")
            entry = {
                "name": c["name"], "parent": c["parent"],
                "location": [loc.x, loc.y, loc.z] if loc else [0, 0, 0],
                "rotation": [rot.roll, rot.pitch, rot.yaw] if rot else [0, 0, 0],
                "scale": [scl.x, scl.y, scl.z] if scl else [1, 1, 1],
                "part": None, "overrides": {},
            }
            mesh = None
            if isinstance(o, unreal.StaticMeshComponent):
                mesh = _get(o, "static_mesh")
            elif isinstance(o, unreal.SkeletalMeshComponent):
                mesh = _get(o, "skeletal_mesh_asset", "skeletal_mesh")
            if mesh is not None:
                entry["part"] = export_mesh_part(mesh, out_dir, result, materials, tex_cache, job, seen)
                for i, om in enumerate(_get(o, "override_materials", default=[]) or []):
                    if om is not None:
                        entry["overrides"][str(i)] = collect_material(om, out_dir, materials, tex_cache, job)
            result["components"].append(entry)
        if not result["parts"]:
            raise RuntimeError("Blueprint %s has no static or skeletal mesh components" % package_path)
    else:
        raise RuntimeError("Unsupported asset type %s. Supported: StaticMesh, SkeletalMesh, "
                           "AnimSequence, Texture2D, Blueprint" % asset_class)

    write_result(job, True, **result)


# =========================================================================== IMPORT
def import_file(filename, dest_dir, dest_name, options=None):
    task = unreal.AssetImportTask()
    task.filename = filename
    task.destination_path = dest_dir
    task.destination_name = dest_name
    task.automated = True
    task.replace_existing = True
    task.save = True
    if options is not None:
        task.options = options
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return [str(p) for p in (task.get_editor_property("imported_object_paths") or [])]


def import_textures(job, tex_dir):
    """Import every texture referenced by the materials. Returns {(file, kind): Texture2D}."""
    out = {}
    for minfo in job.get("materials", {}).values():
        for role, t in minfo.get("textures", {}).items():
            kind = "normal" if role == "normal" else ("color" if t.get("srgb", True) else "data")
            key = (t["file"], kind)
            if key in out:
                continue
            stem = safe_name(os.path.splitext(os.path.basename(t["file"]))[0])
            name = "T_" + stem + {"normal": "_N", "data": "_Data", "color": ""}[kind]
            paths = import_file(t["file"], tex_dir, name)
            tex = load(paths[0]) if paths else load(tex_dir + "/" + name)
            if tex is None:
                warn("texture import failed: %s" % t["file"])
                continue
            if kind == "normal":
                _set(tex, "compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
                _set(tex, "srgb", False)
                _set(tex, "flip_green_channel", bool(t.get("flip_green", True)))
            elif kind == "data":
                _set(tex, "compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
                _set(tex, "srgb", False)
            unreal.EditorAssetLibrary.save_loaded_asset(tex, only_if_is_dirty=False)
            out[key] = tex
    return out


def build_material(name, info, mat_dir, textures):
    mat_name = "M_" + safe_name(name)
    mat_path = mat_dir + "/" + mat_name
    mat = load(mat_path) if unreal.EditorAssetLibrary.does_asset_exist(mat_path) else None
    if isinstance(mat, unreal.Material):
        MEL.delete_all_material_expressions(mat)
    else:
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            mat_name, mat_dir, unreal.Material, unreal.MaterialFactoryNew())

    MP = unreal.MaterialProperty
    tex_nodes = {}
    y = [0]

    def tex_node(role):
        t = info.get("textures", {}).get(role)
        if not t:
            return None, None
        kind = "normal" if role == "normal" else ("color" if t.get("srgb", True) else "data")
        tex = textures.get((t["file"], kind))
        if tex is None:
            return None, None
        key = (t["file"], kind)
        if key not in tex_nodes:
            n = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSample, -600, y[0])
            y[0] += 280
            n.set_editor_property("texture", tex)
            n.set_editor_property("sampler_type", {
                "normal": unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL,
                "data": unreal.MaterialSamplerType.SAMPLERTYPE_MASKS,
                "color": unreal.MaterialSamplerType.SAMPLERTYPE_COLOR}[kind])
            tex_nodes[key] = n
        ch = t.get("channel", "RGB")
        return tex_nodes[key], ("RGB" if ch == "RGB" else ch)

    def const3(rgb):
        n = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant3Vector, -400, y[0])
        y[0] += 120
        n.set_editor_property("constant", unreal.LinearColor(rgb[0], rgb[1], rgb[2], 1.0))
        return n

    def const1(v):
        n = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -400, y[0])
        y[0] += 80
        n.set_editor_property("r", float(v))
        return n

    # Base color
    n, ch = tex_node("base_color")
    if n:
        MEL.connect_material_property(n, "RGB", MP.MP_BASE_COLOR)
    elif info.get("base_color"):
        MEL.connect_material_property(const3(info["base_color"]), "", MP.MP_BASE_COLOR)

    # Scalars
    for role, prop in (("roughness", MP.MP_ROUGHNESS), ("metallic", MP.MP_METALLIC),
                       ("ao", MP.MP_AMBIENT_OCCLUSION)):
        n, ch = tex_node(role)
        if n:
            MEL.connect_material_property(n, "R" if ch == "RGB" else ch, prop)
        elif role in info:
            MEL.connect_material_property(const1(info[role]), "", prop)

    # Normal
    n, ch = tex_node("normal")
    if n:
        MEL.connect_material_property(n, "RGB", MP.MP_NORMAL)

    # Emission
    strength = float(info.get("emission_strength", 1.0))
    n, ch = tex_node("emissive")
    em_src = None
    if n:
        em_src = (n, "RGB")
    elif info.get("emission") and strength > 0 and any(c > 0 for c in info["emission"][:3]):
        em_src = (const3(info["emission"]), "")
    if em_src:
        if abs(strength - 1.0) > 1e-4:
            mul = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -200, y[0])
            y[0] += 120
            MEL.connect_material_expressions(em_src[0], em_src[1], mul, "A")
            MEL.connect_material_expressions(const1(strength), "", mul, "B")
            em_src = (mul, "")
        MEL.connect_material_property(em_src[0], em_src[1], MP.MP_EMISSIVE_COLOR)

    # Opacity
    blend = info.get("blend", "OPAQUE")
    n, ch = tex_node("opacity")
    if blend != "OPAQUE" and n:
        if blend == "MASKED":
            _set(mat, "blend_mode", unreal.BlendMode.BLEND_MASKED)
            MEL.connect_material_property(n, ch if ch != "RGB" else "R", MP.MP_OPACITY_MASK)
        else:
            _set(mat, "blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
            MEL.connect_material_property(n, ch if ch != "RGB" else "R", MP.MP_OPACITY)
    _set(mat, "two_sided", bool(info.get("two_sided", False)))

    MEL.layout_material_expressions(mat)
    MEL.recompile_material(mat)
    unreal.EditorAssetLibrary.save_loaded_asset(mat, only_if_is_dirty=False)
    return mat


def assign_materials(mesh, wanted, built):
    """wanted = list of Blender material names in slot order; match UE slots by name, then index."""
    if not wanted:
        return
    by_norm = {norm(n): n for n in wanted}
    if isinstance(mesh, unreal.StaticMesh):
        slots = list(_get(mesh, "static_materials", default=[]) or [])
        for i, s in enumerate(slots):
            src = by_norm.get(norm(_get(s, "material_slot_name", default=""))) or \
                (wanted[i] if i < len(wanted) else None)
            if src in built:
                mesh.set_material(i, built[src])
    elif isinstance(mesh, unreal.SkeletalMesh):
        slots = list(_get(mesh, "materials", default=[]) or [])
        for i, s in enumerate(slots):
            src = by_norm.get(norm(_get(s, "material_slot_name", default=""))) or \
                (wanted[i] if i < len(wanted) else None)
            if src in built:
                s.set_editor_property("material_interface", built[src])
        mesh.set_editor_property("materials", slots)
    unreal.EditorAssetLibrary.save_loaded_asset(mesh, only_if_is_dirty=False)


def fbx_options(item, job, skeleton):
    mesh_type = item.get("mesh_type", "STATIC")
    ui = unreal.FbxImportUI()
    _set(ui, "import_mesh", mesh_type != "ANIMATION")
    _set(ui, "import_textures", False)          # textures/materials are built explicitly
    _set(ui, "import_materials", not job.get("materials"))
    _set(ui, "import_as_skeletal", mesh_type == "SKELETAL")
    _set(ui, "import_animations", mesh_type in ("SKELETAL", "ANIMATION") and item.get("animations", True))
    _set(ui, "create_physics_asset", mesh_type == "SKELETAL" and skeleton is None)
    _set(ui, "automated_import_should_detect_type", False)
    _set(ui, "mesh_type_to_import", {
        "ANIMATION": unreal.FBXImportType.FBXIT_ANIMATION,
        "SKELETAL": unreal.FBXImportType.FBXIT_SKELETAL_MESH,
    }.get(mesh_type, unreal.FBXImportType.FBXIT_STATIC_MESH))
    if skeleton is not None:
        _set(ui, "skeleton", skeleton)
    sm = _get(ui, "static_mesh_import_data")
    if sm is not None:
        _set(sm, "combine_meshes", bool(item.get("combine_meshes", True)))
        _set(sm, "generate_lightmap_u_vs", True)
        _set(sm, "auto_generate_collision", bool(job.get("auto_collision", True)))
    sk = _get(ui, "skeletal_mesh_import_data")
    if sk is not None:
        _set(sk, "import_morph_targets", True)
        _set(sk, "use_t0_as_ref_pose", False)
    return ui


def blender_to_uasset(job):
    dest = job["destination_path"].rstrip("/")
    # Use the legacy FBX pipeline so the FbxImportUI options above are honoured.
    try:
        unreal.SystemLibrary.execute_console_command(None, "Interchange.FeatureFlags.Import.FBX False")
    except Exception:  # noqa: BLE001
        pass

    textures = import_textures(job, dest + "/Textures") if job.get("materials") else {}
    built = {}
    for mname, minfo in job.get("materials", {}).items():
        try:
            built[mname] = build_material(mname, minfo, dest + "/Materials", textures)
        except Exception as e:  # noqa: BLE001
            warn("material %s failed: %s" % (mname, e))

    created, files = [], []
    shared_skeleton = None
    if job.get("skeleton_path"):
        shared_skeleton = load(job["skeleton_path"])
        if shared_skeleton is None:
            raise RuntimeError("Skeleton not found: %s" % job["skeleton_path"])

    for item in job["items"]:
        skeleton = shared_skeleton if item.get("mesh_type") in ("SKELETAL", "ANIMATION") else None
        paths = import_file(item["fbx"], dest, safe_name(item["name"]), fbx_options(item, job, skeleton))
        if not paths:
            raise RuntimeError("Unreal imported nothing from %s (check the log)" % item["fbx"])
        for p in paths:
            a = load(p)
            if isinstance(a, (unreal.StaticMesh, unreal.SkeletalMesh)):
                assign_materials(a, item.get("materials", []), built)
                if isinstance(a, unreal.SkeletalMesh) and shared_skeleton is None and job.get("share_skeleton", True):
                    shared_skeleton = _get(a, "skeleton")
            if a is not None:
                unreal.EditorAssetLibrary.save_loaded_asset(a, only_if_is_dirty=False)
        created += paths

    content_dir = os.path.abspath(unreal.Paths.project_content_dir())
    for p in created + [m.get_path_name() for m in built.values()] + [t.get_path_name() for t in textures.values()]:
        pkg = p.split(".")[0]
        if pkg.startswith("/Game/"):
            files.append(os.path.join(content_dir, *pkg[len("/Game/"):].split("/")) + ".uasset")
    main = []
    for p in created:
        a = load(p)
        if isinstance(a, (unreal.StaticMesh, unreal.SkeletalMesh)):
            main.append(p)
    write_result(job, True, imported_objects=created, main_assets=main,
                 materials=[m.get_path_name() for m in built.values()],
                 uasset_files=sorted(set(files)))


# =========================================================================== entry
def main():
    job_path = os.environ.get("BLENDER_UASSET_JOB")
    if not job_path:
        unreal.log_error("[UAssetBridge] BLENDER_UASSET_JOB not set")
        return
    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)
    try:
        if job["mode"] == "uasset_to_blender":
            uasset_to_blender(job)
        elif job["mode"] == "blender_to_uasset":
            blender_to_uasset(job)
        else:
            raise RuntimeError("Unknown mode %r" % job["mode"])
    except Exception as e:  # noqa: BLE001
        unreal.log_error("[UAssetBridge] " + traceback.format_exc())
        write_result(job, False, error=str(e), traceback=traceback.format_exc())


main()
