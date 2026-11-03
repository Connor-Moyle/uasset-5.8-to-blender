"""
Material/texture translation between Unreal (as described by unreal_scripts/uasset_job.py)
and Blender's Principled BSDF.

Unreal -> Blender:  build_material(mat, info)
Blender -> Unreal:  describe_material(mat, tex_dir) -> info dict sent to Unreal
"""

import os
import re
import shutil

import bpy

DX_FLAG = "uasset_bridge_dx_normal"   # custom prop on images that hold DirectX (Unreal) normals
NON_COLOR_ROLES = ("normal", "roughness", "metallic", "ao", "opacity")
DISK_FORMATS = (".png", ".jpg", ".jpeg", ".tga", ".exr", ".bmp", ".tif", ".tiff", ".psd")


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name) or "Texture"


# =========================================================================== UE -> Blender
def _load_image(path, non_color, cache):
    key = (path, non_color)
    if key in cache:
        return cache[key]
    img = bpy.data.images.load(path, check_existing=False)
    img.name = os.path.splitext(os.path.basename(path))[0]
    if non_color:
        try:
            img.colorspace_settings.name = "Non-Color"
        except TypeError:
            pass
    try:
        img.pack()                     # the source lives in a temp folder
    except RuntimeError:
        pass
    cache[key] = img
    return img


def build_material(mat, info, image_cache=None):
    """Rebuild `mat` as a Principled BSDF network from an Unreal material description."""
    image_cache = {} if image_cache is None else image_cache
    mat.use_nodes = True
    nt = mat.node_tree
    nodes, links = nt.nodes, nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (500, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (150, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    textures = info.get("textures", {})
    tex_nodes, seps = {}, {}
    y = [300]

    def tex_node(role):
        t = textures.get(role)
        if not t or not os.path.isfile(t["file"]):
            return None
        non_color = role in NON_COLOR_ROLES
        key = (t["file"], non_color)
        if key not in tex_nodes:
            n = nodes.new("ShaderNodeTexImage")
            n.image = _load_image(t["file"], non_color, image_cache)
            n.location = (-700, y[0])
            y[0] -= 300
            tex_nodes[key] = n
        return tex_nodes[key]

    def channel_socket(node, ch):
        if ch in ("RGB", None):
            return node.outputs["Color"]
        if ch == "A":
            return node.outputs["Alpha"]
        if node not in seps:
            s = nodes.new("ShaderNodeSeparateColor")
            s.location = (node.location.x + 300, node.location.y)
            links.new(node.outputs["Color"], s.inputs["Color"])
            seps[node] = s
        return seps[node].outputs[{"R": "Red", "G": "Green", "B": "Blue"}[ch]]

    # Base colour (+ alpha from its A channel if the material is masked/translucent)
    n = tex_node("base_color")
    if n:
        links.new(n.outputs["Color"], bsdf.inputs["Base Color"])
    elif info.get("base_color"):
        c = info["base_color"]
        bsdf.inputs["Base Color"].default_value = (c[0], c[1], c[2], 1.0)

    for role, sock in (("roughness", "Roughness"), ("metallic", "Metallic")):
        n = tex_node(role)
        if n:
            links.new(channel_socket(n, textures[role].get("channel", "R")), bsdf.inputs[sock])
        elif role in info:
            bsdf.inputs[sock].default_value = float(info[role])

    # Normal map: Unreal uses DirectX (green down); Blender wants OpenGL, so flip G.
    n = tex_node("normal")
    if n:
        n.image[DX_FLAG] = True
        sep = nodes.new("ShaderNodeSeparateColor")
        inv = nodes.new("ShaderNodeMath")
        comb = nodes.new("ShaderNodeCombineColor")
        nm = nodes.new("ShaderNodeNormalMap")
        inv.operation = "SUBTRACT"
        inv.inputs[0].default_value = 1.0
        for i, node in enumerate((sep, inv, comb, nm)):
            node.location = (-400 + i * 150, -600)
        links.new(n.outputs["Color"], sep.inputs["Color"])
        links.new(sep.outputs["Red"], comb.inputs["Red"])
        links.new(sep.outputs["Green"], inv.inputs[1])
        links.new(inv.outputs["Value"], comb.inputs["Green"])
        links.new(sep.outputs["Blue"], comb.inputs["Blue"])
        links.new(comb.outputs["Color"], nm.inputs["Color"])
        links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])

    n = tex_node("emissive")
    if n:
        links.new(n.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = float(info.get("emission_strength", 1.0))
    elif info.get("emission") and any(v > 0 for v in info["emission"][:3]):
        e = info["emission"]
        bsdf.inputs["Emission Color"].default_value = (e[0], e[1], e[2], 1.0)
        bsdf.inputs["Emission Strength"].default_value = float(info.get("emission_strength", 1.0))

    blend = info.get("blend", "OPAQUE")
    if blend != "OPAQUE":
        n = tex_node("opacity")
        if n:
            links.new(channel_socket(n, textures["opacity"].get("channel", "R")), bsdf.inputs["Alpha"])
        elif tex_node("base_color"):
            links.new(tex_node("base_color").outputs["Alpha"], bsdf.inputs["Alpha"])
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = "BLENDED" if blend == "BLEND" else "DITHERED"
        elif hasattr(mat, "blend_method"):
            mat.blend_method = "BLEND" if blend == "BLEND" else "CLIP"

    if hasattr(mat, "use_backface_culling"):
        mat.use_backface_culling = not info.get("two_sided", False)
    mat["uasset_bridge_source"] = info.get("path", "")
    return mat


# =========================================================================== Blender -> UE
def _trace(socket, depth=0):
    """Follow links back from an input socket to an image. Returns (image, channel) or None."""
    if depth > 12 or not socket.is_linked:
        return None
    link = socket.links[0]
    node, out = link.from_node, link.from_socket
    if node.type == "TEX_IMAGE":
        if node.image is None:
            return None
        return node.image, ("A" if out.name == "Alpha" else "RGB")
    if node.type in ("SEPARATE_COLOR", "SEPRGB", "SEPARATE_RGB"):
        r = _trace(node.inputs[0], depth + 1)
        if r:
            ch = {"Red": "R", "Green": "G", "Blue": "B", "R": "R", "G": "G", "B": "B"}.get(out.name, "R")
            return r[0], ch
        return None
    if node.type == "NORMAL_MAP":
        return _trace(node.inputs["Color"], depth + 1)
    for inp in node.inputs:                    # reroutes, mix, gamma, our DX flip chain, ...
        if inp.is_linked:
            r = _trace(inp, depth + 1)
            if r:
                return r
    return None


def _write_image(img, tex_dir, cache):
    if img.name in cache:
        return cache[img.name]
    os.makedirs(tex_dir, exist_ok=True)
    src = bpy.path.abspath(img.filepath) if img.filepath else ""
    ext = os.path.splitext(src)[1].lower()
    if src and os.path.isfile(src) and not img.packed_file and ext in DISK_FORMATS and not img.is_dirty:
        dst = os.path.join(tex_dir, _safe(os.path.splitext(img.name)[0]) + ext)
        shutil.copy2(src, dst)
    else:
        dst = os.path.join(tex_dir, _safe(os.path.splitext(img.name)[0]) + ".png")
        prev = img.file_format
        try:
            img.file_format = "PNG"
            img.save(filepath=dst, save_copy=True)
        finally:
            img.file_format = prev
    cache[img.name] = dst
    return dst


def _principled(mat):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    out = next((n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
    if out and out.inputs["Surface"].is_linked:
        n = out.inputs["Surface"].links[0].from_node
        if n.type == "BSDF_PRINCIPLED":
            return n
    return next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)


def describe_material(mat, tex_dir, image_cache):
    """Describe a Blender material for the Unreal side (textures written into tex_dir)."""
    info = {"textures": {}, "blend": "OPAQUE", "two_sided": not getattr(mat, "use_backface_culling", False)}
    bsdf = _principled(mat)
    if bsdf is None:
        c = mat.diffuse_color
        info["base_color"] = [c[0], c[1], c[2], c[3]]
        info["roughness"] = float(mat.roughness)
        info["metallic"] = float(mat.metallic)
        return info

    def add(role, sock_name, srgb):
        sock = bsdf.inputs.get(sock_name)
        if sock is None:
            return False
        r = _trace(sock)
        if not r:
            return False
        img, ch = r
        entry = {"file": _write_image(img, tex_dir, image_cache), "channel": ch, "srgb": srgb}
        if role == "normal":
            entry["channel"] = "RGB"
            entry["flip_green"] = not bool(img.get(DX_FLAG, False))   # OpenGL -> DirectX
        info["textures"][role] = entry
        return True

    if not add("base_color", "Base Color", True):
        c = bsdf.inputs["Base Color"].default_value
        info["base_color"] = [c[0], c[1], c[2], c[3]]
    if not add("roughness", "Roughness", False):
        info["roughness"] = float(bsdf.inputs["Roughness"].default_value)
    if not add("metallic", "Metallic", False):
        info["metallic"] = float(bsdf.inputs["Metallic"].default_value)
    add("normal", "Normal", False)
    strength = float(bsdf.inputs["Emission Strength"].default_value)
    if not add("emissive", "Emission Color", True):
        e = bsdf.inputs["Emission Color"].default_value
        if strength > 0 and any(v > 0 for v in e[:3]):
            info["emission"] = [e[0], e[1], e[2], 1.0]
    info["emission_strength"] = strength

    if add("opacity", "Alpha", False):
        method = getattr(mat, "surface_render_method", None) or getattr(mat, "blend_method", "")
        info["blend"] = "BLEND" if method in ("BLENDED", "BLEND") else "MASKED"
    return info
