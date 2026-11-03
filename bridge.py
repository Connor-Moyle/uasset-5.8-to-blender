"""
Blender-independent core: locating Unreal Editor, mapping files <-> Unreal package
paths, and running a headless Unreal job. No bpy imports here so it can be tested
(or used from a plain Python CLI) without Blender.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNREAL_SCRIPT = os.path.join(HERE, "unreal_scripts", "uasset_job.py")
TEMP_CONTENT_SUBDIR = "__BlenderBridge"
SIDECAR_EXTS = (".uexp", ".ubulk", ".uptnl")


class BridgeError(RuntimeError):
    pass


# ----------------------------------------------------------------- Unreal executable
def find_editor_cmd(engine_dir):
    """Return the headless UnrealEditor-Cmd executable for an engine root (e.g. .../UE_5.8)."""
    if not engine_dir:
        raise BridgeError("Set the Unreal Engine folder in the add-on preferences.")
    engine_dir = os.path.abspath(os.path.expanduser(engine_dir))
    if os.path.isfile(engine_dir):          # user pointed straight at the exe
        return engine_dir
    root = engine_dir
    if os.path.basename(root.rstrip("/\\")).lower() != "engine" and os.path.isdir(os.path.join(root, "Engine")):
        root = os.path.join(root, "Engine")
    candidates = [
        os.path.join(root, "Binaries", "Win64", "UnrealEditor-Cmd.exe"),
        os.path.join(root, "Binaries", "Linux", "UnrealEditor-Cmd"),
        os.path.join(root, "Binaries", "Mac", "UnrealEditor-Cmd"),
        os.path.join(root, "Binaries", "Mac", "UnrealEditor.app", "Contents", "MacOS", "UnrealEditor"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise BridgeError("Could not find UnrealEditor-Cmd under %s" % engine_dir)


def project_content_dir(uproject):
    return os.path.join(os.path.dirname(os.path.abspath(uproject)), "Content")


# ----------------------------------------------------------------- path mapping
def file_to_package_path(uasset_file, uproject):
    """
    Map a .uasset on disk to an Unreal package path.
      <Project>/Content/Props/SM_Chair.uasset          -> /Game/Props/SM_Chair
      <Project>/Plugins/MyPlug/Content/X/T_Wood.uasset -> /MyPlug/X/T_Wood
    Returns None if the file is not inside the project.
    """
    f = os.path.normcase(os.path.abspath(uasset_file))
    proj_dir = os.path.dirname(os.path.abspath(uproject))
    content = os.path.normcase(project_content_dir(uproject)) + os.sep
    stem = os.path.splitext(os.path.abspath(uasset_file))[0]

    if f.startswith(content):
        rel = stem[len(content):]
        return "/Game/" + rel.replace("\\", "/")

    plugins = os.path.normcase(os.path.join(proj_dir, "Plugins")) + os.sep
    if f.startswith(plugins):
        parts = stem[len(plugins):].replace("\\", "/").split("/")
        if "Content" in parts:
            i = parts.index("Content")
            plugin_name = parts[i - 1]
            return "/" + plugin_name + "/" + "/".join(parts[i + 1:])
    return None


def package_path_to_file(package_path, uproject):
    if not package_path.startswith("/Game/"):
        raise BridgeError("Only /Game/ destinations are supported, got %s" % package_path)
    rel = package_path[len("/Game/"):].split("/")
    return os.path.join(project_content_dir(uproject), *rel) + ".uasset"


def stage_external_uasset(uasset_file, uproject):
    """
    Copy a .uasset that lives outside the project into Content/__BlenderBridge/ so the
    editor can load it. Returns (package_path, list_of_copied_files).
    """
    name = os.path.splitext(os.path.basename(uasset_file))[0]
    dest_dir = os.path.join(project_content_dir(uproject), TEMP_CONTENT_SUBDIR, "Staged")
    os.makedirs(dest_dir, exist_ok=True)
    copied = []
    base = os.path.splitext(uasset_file)[0]
    for ext in (".uasset",) + SIDECAR_EXTS:
        src = base + ext
        if os.path.isfile(src):
            dst = os.path.join(dest_dir, name + ext)
            shutil.copy2(src, dst)
            copied.append(dst)
    return "/Game/%s/Staged/%s" % (TEMP_CONTENT_SUBDIR, name), copied


def sanitize_asset_name(name):
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    return out or "Asset"


# ----------------------------------------------------------------- run a job
def run_unreal_job(engine_dir, uproject, job, timeout=900, extra_args=()):
    """Run uasset_job.py inside a headless editor. Returns (result_dict, log_path)."""
    if not uproject or not os.path.isfile(uproject):
        raise BridgeError("Set a valid .uproject file in the add-on preferences.")
    exe = find_editor_cmd(engine_dir)

    work = tempfile.mkdtemp(prefix="uasset_bridge_")
    job = dict(job)
    job["result_path"] = os.path.join(work, "result.json")
    job_path = os.path.join(work, "job.json")
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
    log_path = os.path.join(work, "unreal.log")

    cmd = [exe,
        os.path.abspath(uproject),
        "-run=pythonscript",
        "-script=" + UNREAL_SCRIPT,
        "-unattended", "-nop4", "-nosplash", "-NullRHI", "-nosound",
        "-stdout", "-FullStdOutLogOutput",
        "-abslog=" + log_path,
    ]
    cmd += list(extra_args)

    env = dict(os.environ)
    env["BLENDER_UASSET_JOB"] = job_path

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise BridgeError("Unreal timed out after %ds. Log: %s" % (timeout, log_path))
    except OSError as e:
        raise BridgeError("Could not start Unreal (%s): %s" % (exe, e))

    stdout_path = os.path.join(work, "stdout.txt")
    with open(stdout_path, "wb") as f:
        f.write(proc.stdout or b"")

    if not os.path.isfile(job["result_path"]):
        raise BridgeError(
            "Unreal exited (code %s after %.0fs) without a result. Is the Python Editor Script "
            "Plugin enabled in the project? Log: %s" % (proc.returncode, time.time() - t0,
                                                        log_path if os.path.isfile(log_path) else stdout_path))
    with open(job["result_path"], "r", encoding="utf-8") as f:
        result = json.load(f)
    if not result.get("ok"):
        raise BridgeError("Unreal reported an error: %s\nLog: %s" % (result.get("error"), log_path))
    return result, log_path


# ----------------------------------------------------------------- high level
def uasset_to_blender(engine_dir, uproject, uasset_file, out_dir=None, **opts):
    """
    Have Unreal export a .uasset (mesh, its materials/textures, other parts on the same
    skeleton, animations, or a Blueprint's mesh components) into out_dir.
    Returns (result, log_path); see unreal_scripts/uasset_job.py for the result layout.
    """
    staged = []
    pkg = file_to_package_path(uasset_file, uproject)
    if pkg is None:
        pkg, staged = stage_external_uasset(uasset_file, uproject)
    out_dir = out_dir or tempfile.mkdtemp(prefix="uasset_import_")
    job = {"mode": "uasset_to_blender", "package_path": pkg, "output_dir": os.path.abspath(out_dir)}
    job.update(opts)
    try:
        return run_unreal_job(engine_dir, uproject, job)
    finally:
        for p in staged:
            try:
                os.remove(p)
            except OSError:
                pass


def blender_to_uasset(engine_dir, uproject, items, materials=None, target_uasset=None,
                      destination_path="/Game/BlenderExports", **opts):
    """
    Import FBX files (items = [{"fbx", "name", "mesh_type", "materials": [slot names]}])
    plus a material description into the project as .uassets.

    If target_uasset is inside the project's Content folder, assets go into that folder and
    the first item is named after the file. Otherwise they are created under
    destination_path and the resulting .uasset files are copied next to target_uasset.
    Returns (result, log_path).
    """
    items = [dict(i) for i in items]
    copy_dir = None
    if target_uasset:
        pkg = file_to_package_path(target_uasset, uproject)
        if pkg and pkg.startswith("/Game/"):
            destination_path, first = pkg.rsplit("/", 1)
        else:
            copy_dir = os.path.dirname(os.path.abspath(target_uasset))
            first = os.path.splitext(os.path.basename(target_uasset))[0]
        if len(items) == 1:
            items[0]["name"] = first
    for it in items:
        it["name"] = sanitize_asset_name(it.get("name") or os.path.splitext(os.path.basename(it["fbx"]))[0])
        it["fbx"] = os.path.abspath(it["fbx"])

    job = {"mode": "blender_to_uasset", "destination_path": destination_path.rstrip("/"),
           "items": items, "materials": materials or {}}
    job.update(opts)
    result, log_path = run_unreal_job(engine_dir, uproject, job)

    if copy_dir:
        os.makedirs(copy_dir, exist_ok=True)
        copied = []
        for f in result.get("uasset_files", []):
            if os.path.isfile(f):
                dst = os.path.join(copy_dir, os.path.basename(f))
                shutil.copy2(f, dst)
                copied.append(dst)
        result["copied_to"] = copied
    return result, log_path
