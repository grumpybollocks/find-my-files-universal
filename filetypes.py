"""File-type registry: the ONE place that says which extensions count as work.

Each extension below was checked against real files on the drives (2026-11-10):
the folder it lives in and/or its first bytes. Add new types here only.
"""

CATEGORIES = {
    # 3D geometry / CAD
    "3D models": {
        "stl": "STL mesh", "obj": "OBJ mesh", "3mf": "3MF (mesh / slicer project)",
        "3dm": "Rhino", "f3d": "Fusion 360", "f3z": "Fusion 360 archive",
        "step": "STEP CAD", "stp": "STEP CAD", "iges": "IGES CAD", "igs": "IGES CAD",
        "blend": "Blender", "fbx": "FBX", "glb": "glTF binary", "gltf": "glTF",
        "ply": "PLY mesh / scan", "3ds": "3ds Max (old)", "max": "3ds Max",
        "ztl": "ZBrush tool", "zpr": "ZBrush project", "dae": "Collada", "skp": "SketchUp",
        "amf": "AMF mesh", "usdz": "USD", "sldprt": "SolidWorks part", "ipt": "Inventor part",
    },
    # slicers / printers
    "Print files": {
        "lys": "Lychee scene", "lyt": "Lychee autosave", "gcode": "G-code", "bgcode": "binary G-code",
        "fabbproject": "Netfabb project", "cxdlp": "Chitubox/Creality resin slice",
        "chitubox": "Chitubox project", "ctb": "Chitubox slice", "cbddlp": "Chitubox slice",
        "pwmx": "Anycubic Photon slice", "pwma": "Anycubic Photon slice", "pm3m": "Anycubic Photon M3 slice",
        "pm3": "Anycubic Photon slice", "pm4n": "Anycubic Photon slice", "pwmo": "Anycubic Photon slice",
        "pwscene": "Photon Workshop scene", "goo": "Elegoo slice", "sl1": "Prusa resin slice",
    },
    # 2D: vector, drawings
    "2D / vector": {
        "cdr": "CorelDRAW", "cmx": "Corel exchange", "dxf": "DXF drawing", "dwg": "AutoCAD drawing",
        "ai": "Illustrator", "eps": "EPS vector", "svg": "SVG vector", "plt": "plotter file",
    },
    # laser engraving
    "Laser": {
        "ezd": "EZCAD / LenMark", "stm": "laser file (stm)", "rld": "RDWorks", "lbrn": "LightBurn",
        "lbrn2": "LightBurn",
    },
    # 3D scanner sessions
    "3D scans": {"rscan": "scanner session", "scanbuf": "scanner buffer"},
}

# Checked and deliberately NOT work: lyx (Lychee settings backup), aip/m3d/nsb/scn (game files),
# mtl (OBJ material sidecar - noise), pdf (mostly manuals; search with "Everything").

EXTRA = {
    "Images": "jpg jpeg png heic webp bmp tif tiff gif".split(),
    "Archives": "zip rar 7z tar gz zst".split(),
    "Videos": "mp4 mkv mov avi webm m4v wmv flv mpg mpeg m2ts 3gp".split(),
    "Audio": "mp3 wav flac ogg oga opus m4a aac wma".split(),
    "Text & documents": ("txt md markdown rst log nfo csv tsv json xml yaml yml toml ini cfg conf srt "
                         "pdf doc docx odt rtf xls xlsx ods").split(),
}

WORK_EXTS = sorted({e for c in CATEGORIES.values() for e in c})


def filter_options():
    """Name -> list of extensions (None = no filter). First entry is the default."""
    opts = {"All my work": WORK_EXTS}
    opts.update({name: sorted(exts) for name, exts in CATEGORIES.items()})
    opts.update(EXTRA)
    opts["Everything"] = None
    return opts


def describe(ext: str) -> str:
    for c in CATEGORIES.values():
        if ext in c:
            return c[ext]
    return (ext.upper() + " file") if ext else "file"
