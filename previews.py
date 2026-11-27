"""Preview renderers. One function per format, registered in PREVIEWERS.

Every function takes (path, size) and returns (QImage | None, info_text).
Return (None, reason) when there is no honest preview - never fake one.
Runs in a worker thread: use only QImage/QPainter (no widgets).
"""
import base64
import json
import math
import os
import re
import struct
import zipfile

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPen, QColor, QPainterPath

BG = QColor(34, 34, 34)
MAX_TRIS = 800_000  # triangles drawn per mesh preview: keeps RAM < ~150 MB and time < ~1 s
STL_BLOCKS = 2000   # big binary STL: read this many evenly spaced chunks (total MAX_TRIS triangles)


def _fit(img: QImage, size):
    if img.isNull():
        return None
    return img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


# ---------------------------------------------------------------- meshes
# Models are drawn from a 3/4 view (turned 35°, looking down 30°), lit by each triangle's real
# angle - flat tops, walls and relief all read clearly. Only real geometry is drawn.
# The size shown always comes from the real (unturned) vertices.
VIEW_YAW, VIEW_PITCH = math.radians(35), math.radians(30)
_L = np.array([-0.45, 0.55, 0.70], dtype=np.float32)
LIGHT = _L / np.linalg.norm(_L)
FILL_LIGHT = np.array([0.70, 0.10, 0.70], dtype=np.float32) / np.linalg.norm([0.70, 0.10, 0.70])  # soft, from the right
FILL_BUDGET = 1_500_000  # extra points spread inside big triangles (~18 MB)


def _size_note(mn, mx):
    d = mx - mn
    return f"Size: {d[0]:.1f} × {d[1]:.1f} × {d[2]:.1f} (model units, usually mm)"


def _view(p):
    """(N, 3) Z-up model coordinates (or directions) -> (N, 3) screen right, screen up, toward viewer."""
    cy, sy, cp, sp = math.cos(VIEW_YAW), math.sin(VIEW_YAW), math.cos(VIEW_PITCH), math.sin(VIEW_PITCH)
    x1 = p[:, 0] * cy - p[:, 1] * sy
    y1 = p[:, 0] * sy + p[:, 1] * cy
    z = p[:, 2]
    return np.stack([x1, z * cp + y1 * sp, z * sp - y1 * cp], axis=1).astype(np.float32)


def y_up_to_z_up(p):
    """Turn (not mirror) a Y-up model upright: (x, y, z) -> (x, -z, y)."""
    return np.stack([p[:, 0], -p[:, 2], p[:, 1]], axis=1)


class _Frame:
    """One picture being drawn: screen placement + nearest-point buffer. Pieces of a model can be
    added one after another (big files are drawn in chunks, so RAM stays flat)."""

    def __init__(self, smn, smx, W):
        d = smx - smn
        self.mn, self.mx, self.W = smn, smx, W
        self.sc = (W - 10) / max(float(d[0]), float(d[1]), 1e-6)
        self.ox, self.oy = (W - d[0] * self.sc) / 2, (W - d[1] * self.sc) / 2  # centred
        self.dd = float(d[2]) + 1e-6
        self.img = np.full((W, W), -1.0, dtype=np.float32)
        self.dimg = np.full((W, W), -1.0, dtype=np.float32)

    def add(self, pts, light, dot=1):
        """Screen-space points; nearer points win. light in 0..1 per point, or None = depth only."""
        W = self.W
        x = ((pts[:, 0] - self.mn[0]) * self.sc + self.ox).astype(np.int32)
        y = ((self.mx[1] - pts[:, 1]) * self.sc + self.oy).astype(np.int32)
        depth = ((pts[:, 2] - self.mn[2]) / self.dd).astype(np.float32)
        shade = (0.18 + 0.12 * depth + 0.70 * light) if light is not None else (0.30 + 0.60 * depth)
        order = np.argsort(depth, kind="stable")  # far first: near points overwrite
        x, y, shade, depth = x[order], y[order], shade[order], depth[order]
        ci = np.full((W, W), -1.0, dtype=np.float32)
        cd = np.full((W, W), -1.0, dtype=np.float32)
        for dx in range(dot):
            for dy in range(dot):
                xx, yy = np.clip(x + dx, 0, W - 1), np.clip(y + dy, 0, W - 1)
                ci[yy, xx] = shade
                cd[yy, xx] = depth
        m = cd > self.dimg
        self.img[m] = ci[m]
        self.dimg[m] = cd[m]

    def image(self):
        img = _close_pinholes(_close_pinholes(self.img, self.dimg), self.dimg)
        out = np.where(img < 0, 0.13, np.clip(img, 0, 1))
        g = (out * 255).astype(np.uint8)
        rgb = np.ascontiguousarray(np.dstack([g, (g * 0.93).astype(np.uint8), (g * 0.80).astype(np.uint8)]))
        return QImage(rgb.data, self.W, self.W, 3 * self.W, QImage.Format_RGB888).copy()


def _draw(pts, light, size, dot=1):
    f = _Frame(pts.min(0), pts.max(0), size)
    f.add(pts, light, dot)
    return f.image()


def _close_pinholes(img, dimg, tol=0.02):
    """A pixel the sampling missed shows the back of the model (or background) through the front
    surface as a dark speck. Where at least 6 of the 8 neighbours are clearly nearer, take their
    average. Uses only already-drawn points of the real surface."""
    H, W = img.shape
    pad_i = np.pad(img, 1, constant_values=-1.0)
    pad_d = np.pad(dimg, 1, constant_values=-1.0)
    cnt = np.zeros((H, W), dtype=np.int8)
    acc_i = np.zeros((H, W), dtype=np.float32)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            if dy == 1 and dx == 1:
                continue
            ni = pad_i[dy:dy + H, dx:dx + W]
            nd = pad_d[dy:dy + H, dx:dx + W]
            near = (ni >= 0) & (nd > dimg + tol)
            cnt += near
            acc_i += np.where(near, ni, 0)
    fix = cnt >= 6
    out = img.copy()
    out[fix] = acc_i[fix] / cnt[fix]
    return out


def _splat(pts, nz, size, extent=None):
    """Point cloud (no triangles), Z-up: 3/4 view, shaded by depth, 2 px dots."""
    mn, mx = extent if extent is not None else (pts.min(0), pts.max(0))
    return _draw(_view(pts), None, size, dot=2), _size_note(mn, mx)


def _thin(v):
    """At most MAX_TRIS triangles, evenly picked. Returns (triangles, share kept)."""
    if len(v) <= MAX_TRIS:
        return v, 1.0
    step = len(v) // MAX_TRIS + 1
    return v[::step], 1.0 / step


def _tri_points(vr, sc, budget=FILL_BUDGET):
    """Screen-space triangles vr -> (points, light): triangle centres + extra points spread inside
    each triangle in proportion to its size on screen, so big flat triangles draw solid, not as
    scattered dots (seen on real files 2026-11-11). Points only ever lie ON the real triangles."""
    with np.errstate(all="ignore"):
        n = np.cross(vr[:, 1] - vr[:, 0], vr[:, 2] - vr[:, 0])
        n /= np.linalg.norm(n, axis=1, keepdims=True)
        lit = 0.75 * np.abs(n @ LIGHT) + 0.25 * np.abs(n @ FILL_LIGHT)  # abs: STL winding isn't reliable
        light = np.nan_to_num(lit, nan=0.5).astype(np.float32)
    a, b, c = vr[:, 0], vr[:, 1], vr[:, 2]
    with np.errstate(all="ignore"):
        area = 0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                            - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) * sc * sc
        edge = np.max([np.hypot(*(p[:, :2] - q[:, :2]).T) for p, q in ((a, b), (b, c), (c, a))], axis=0) * sc
    k = np.nan_to_num(np.maximum(area * 4, edge * 2), nan=0).astype(np.float64)  # ~4 points per pixel
    k[edge <= 2] = 0  # already dense enough: the triangle centres cover it
    total = k.sum()
    if total > budget:
        k *= budget / total
    k = k.astype(np.int64)
    pts, lights = [vr.mean(axis=1)], [light]
    if k.sum():
        idx = np.repeat(np.arange(len(vr)), k)
        rng = np.random.default_rng(0)  # same picture every time
        r = rng.random((len(idx), 2), dtype=np.float32)
        out = r.sum(1) > 1
        r[out] = 1 - r[out]
        ai = a[idx]
        pts.append(ai + r[:, :1] * (b[idx] - ai) + r[:, 1:] * (c[idx] - ai))
        lights.append(light[idx])
    return np.concatenate(pts), np.concatenate(lights)


def _mesh_splat(v, size, part=1.0):
    """Draw triangles v (N, 3, 3), Z-up, in 3/4 view.
    part = share of the file's triangles that were read (huge files are sampled): the less was read,
    the bigger each dot, so the surface still reads as a surface, not a sieve."""
    flat = v.reshape(-1, 3)
    note = _size_note(flat.min(0), flat.max(0))
    vr = _view(flat).reshape(-1, 3, 3)
    f = _Frame(vr.reshape(-1, 3).min(0), vr.reshape(-1, 3).max(0), size)
    pts, light = _tri_points(vr, f.sc)
    f.add(pts, light, 1 if part >= 0.5 else min(5, round(1 / math.sqrt(max(part, 1e-6)))))
    return f.image(), note


STREAM_MAX = 1_200_000_000  # binary STLs up to this size are drawn in full, in chunks (RAM stays ~150 MB)
STREAM_CHUNK = 500_000    # triangles per chunk


def _stl_chunks(path, ntri, dt):
    """Valid triangles of a binary STL, STREAM_CHUNK at a time."""
    with open(path, "rb") as fh:
        fh.seek(84)
        left = ntri
        while left > 0:
            n = min(STREAM_CHUNK, left)
            buf = fh.read(n * 50)
            if len(buf) < n * 50:
                return
            v = np.frombuffer(buf, dtype=dt)["v"].astype(np.float32)
            yield _finite(v)
            left -= n


def _stl_stream(path, ntri, dt, size):
    """Every triangle of a big binary STL, drawn chunk by chunk: pass 1 = exact size and framing,
    pass 2 = drawing (the file is usually still in the disk cache by then)."""
    mn = mx = smn = smx = None
    for v in _stl_chunks(path, ntri, dt):
        if not len(v):
            continue
        flat = v.reshape(-1, 3)
        vf = _view(flat)
        lo, hi, slo, shi = flat.min(0), flat.max(0), vf.min(0), vf.max(0)
        mn = lo if mn is None else np.minimum(mn, lo)
        mx = hi if mx is None else np.maximum(mx, hi)
        smn = slo if smn is None else np.minimum(smn, slo)
        smx = shi if smx is None else np.maximum(smx, shi)
    if mn is None:
        return None, "STL contains only invalid numbers (damaged file?)"
    f = _Frame(smn, smx, size)
    for v in _stl_chunks(path, ntri, dt):
        if len(v):
            vr = _view(v.reshape(-1, 3)).reshape(-1, 3, 3)
            pts, light = _tri_points(vr, f.sc, max(FILL_BUDGET * len(v) / ntri, 50_000))
            f.add(pts, light)
    return f.image(), _size_note(mn, mx)


def preview_stl(path, size):
    fsize = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(84)
    ntri = int.from_bytes(head[80:84], "little")
    if ntri and 0 <= fsize - (84 + ntri * 50) <= 512:  # binary STL (some writers pad a few bytes)
        dt = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
        part = 1.0
        if ntri <= MAX_TRIS:
            v = np.fromfile(path, dtype=dt, offset=84, count=ntri)["v"]
        elif fsize <= STREAM_MAX:  # big: every triangle, in chunks
            return _stl_stream(path, ntri, dt, size)
        else:  # huge: read STL_BLOCKS evenly spaced chunks (~MAX_TRIS triangles, ~40 MB read)
            per = MAX_TRIS // STL_BLOCKS
            blocks = []
            with open(path, "rb") as f:
                for k in range(STL_BLOCKS):
                    f.seek(84 + (ntri - per) * k // (STL_BLOCKS - 1) * 50)
                    blocks.append(np.frombuffer(f.read(per * 50), dtype=dt)["v"])
            v = np.concatenate(blocks)
            part = len(v) / ntri
        v = v.astype(np.float32, copy=False)
    else:  # ASCII STL: sample ~80 slices spread over the file
        parts = []
        with open(path, "rb") as f:
            part = 1.0
            if fsize <= 60_000_000:
                parts.append(f.read())
            else:
                part = 80 * 700_000 / fsize
                for k in range(80):
                    f.seek(fsize * k // 80)
                    parts.append(f.read(700_000))
        vv = []
        for chunk in parts:
            txt = chunk.decode("ascii", "ignore")
            i, j = txt.find("facet"), txt.rfind("endfacet")
            if i >= 0 and j >= 0:
                found = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", txt[i:j])
                vv += found[: len(found) // 3 * 3]
        if not vv:
            return None, "Not a readable STL"
        v = np.array(vv, dtype=np.float32).reshape(-1, 3, 3)
    # drop triangles with NaN/inf or absurd coordinates (seen in real damaged STLs)
    good = _finite(v)
    if len(good) != len(v):
        v = good
        if len(v) == 0:
            return None, "STL contains only invalid numbers (damaged file?)"
    if len(v) == 0:
        return None, "Empty STL"
    img, note = _mesh_splat(v, size, part)
    if part < 1:
        note += f"\nHuge file: drawn from {part:.0%} of its triangles - size is approximate"
    return img, note


OBJ_FULL_MAX = 80_000_000  # bigger OBJs: points sampled from the whole file (fast, light on RAM)


_NUM = re.compile(rb"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")


def _obj_vertices(data):
    """All 'v x y z' lines as a float32 array. Joins '\\' line continuations (valid OBJ, seen in a
    real file 2026-11-11) and skips the odd broken line instead of giving up on the whole model."""
    data = data.replace(b"\\\r\n", b" ").replace(b"\\\n", b" ")
    vs = re.findall(rb"^v[ \t]+(\S+)[ \t]+(\S+)[ \t]+(\S+)", data, re.M)
    try:
        with np.errstate(over="ignore"):  # 1e40 -> inf, dropped later as an invalid number
            return np.array(vs, dtype=np.float64).astype(np.float32).reshape(-1, 3), data
    except ValueError:  # keep the row (as NaN) so later face numbers still point at the right vertex
        nan = float("nan")
        vs = [tuple(float(x) if _NUM.fullmatch(x) else nan for x in v) for v in vs]
        with np.errstate(over="ignore"):
            return np.array(vs, dtype=np.float64).astype(np.float32).reshape(-1, 3), data


def _face_ids(line):
    try:  # "f 1/2/3 4//6 -1 ..." -> vertex numbers
        return [int(t.split(b"/")[0]) for t in line.split()]
    except ValueError:
        return None


def _obj_faces(data):
    """Triangles (0-based vertex numbers), polygons fanned. Negative numbers count back from the
    vertices read SO FAR (OBJ rule), so files using them are walked in order."""
    faces = re.findall(rb"^f[ \t]+([^\r\n]+)", data, re.M)
    tris = []
    if not any(b"-" in f for f in faces):
        for line in faces:
            ids = _face_ids(line)
            if ids:
                ids = [i - 1 for i in ids]
                tris += [(ids[0], ids[j], ids[j + 1]) for j in range(1, len(ids) - 1)]
        return tris
    seen = 0
    for m in re.finditer(rb"^(?:(v)[ \t]+\S+[ \t]+\S+[ \t]+\S+|f[ \t]+([^\r\n]+))", data, re.M):
        if m.group(1):
            seen += 1
            continue
        ids = _face_ids(m.group(2))
        if ids:
            ids = [i - 1 if i > 0 else seen + i for i in ids]
            tris += [(ids[0], ids[j], ids[j + 1]) for j in range(1, len(ids) - 1)]
    return tris


def _finite(a):
    """Rows (points (N,3) or triangles (N,3,3)) with only sane numbers (NaN / 1e30 junk dropped)."""
    ax = tuple(range(1, a.ndim))
    return a[np.isfinite(a).all(axis=ax) & (np.abs(a) < 1e7).all(axis=ax)]


def _obj_z_up(pts):
    """OBJ has no 'up' rule: most exports are Y-up (taller in Y than in Z)."""
    return y_up_to_z_up(pts) if np.ptp(pts[:, 1]) > np.ptp(pts[:, 2]) else pts


def _obj_sampled(path, size, fsize):
    vs = []
    with open(path, "rb") as f:
        for k in range(100):  # 100 slices of 1 MB spread over the file
            f.seek(fsize * k // 100)
            chunk = f.read(1_000_000)
            vs.append(_obj_vertices(chunk[chunk.find(b"\n") + 1:])[0])
    pts = np.concatenate(vs)
    if not len(pts):
        return None, "No vertices found in this big OBJ"
    pts = _finite(pts)
    if len(pts) == 0:
        return None, "OBJ contains only invalid numbers (damaged file?)"
    img, note = _splat(_obj_z_up(pts), None, size)
    return img, note + f"\nBig file ({fsize / 1e9:.1f} GB): points sampled from the whole file - size is approximate"


def preview_obj(path, size):
    fsize = os.path.getsize(path)
    if fsize > OBJ_FULL_MAX:
        return _obj_sampled(path, size, fsize)
    with open(path, "rb") as f:
        data = f.read()
    pts, data = _obj_vertices(data)
    if not len(pts):
        return None, "No vertices in OBJ"
    tris = _obj_faces(data)
    del data
    pts = _obj_z_up(pts)
    t = np.array(tris, dtype=np.int64).reshape(-1, 3)
    t = t[((t >= 0) & (t < len(pts))).all(axis=1)]
    v = pts[t]
    v = _finite(v)
    if len(v) == 0:  # only a point cloud
        good = _finite(pts)
        return _splat(good, None, size) if len(good) else (None, "OBJ contains only invalid numbers")
    v, part = _thin(v)
    return _mesh_splat(v, size, part)


def _glb_array(data, js, bin_off, acc_id, comps):
    """Read accessor acc_id as an (count, comps) array, or None if it's packed in a way we don't read."""
    acc = js["accessors"][acc_id]
    types = {5126: "<f4", 5125: "<u4", 5123: "<u2", 5121: "u1"}
    if acc.get("componentType") not in types or "sparse" in acc or "bufferView" not in acc:
        return None
    dt = np.dtype(types[acc["componentType"]])
    bv = js["bufferViews"][acc["bufferView"]]
    off = bin_off + bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    item = dt.itemsize * comps
    stride = bv.get("byteStride", item)
    n = acc["count"]
    if n == 0 or off + (n - 1) * stride + item > len(data):
        return None
    raw = np.frombuffer(data, dtype=np.uint8, count=(n - 1) * stride + item, offset=off)
    rows = np.lib.stride_tricks.as_strided(raw, shape=(n, item), strides=(stride, 1))
    return rows.copy().view(dt).reshape(n, comps)


MESH_XML_MAX = 150_000_000  # 3MF model XML read for a mesh preview
GLB_MAX = 300_000_000       # GLB is read whole


def preview_glb(path, size):
    if os.path.getsize(path) > GLB_MAX:
        return None, f"GLB too big to preview (over {GLB_MAX // 1_000_000} MB)"
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"glTF":
        return None, "Not a GLB file"
    jlen = struct.unpack_from("<I", data, 12)[0]
    js = json.loads(data[20:20 + jlen])
    bin_off = 20 + jlen + 8  # skip BIN chunk header
    if "KHR_draco_mesh_compression" in js.get("extensionsUsed", []):
        return None, "GLB is Draco-compressed (no preview)"
    pts, tris = [], []
    for mesh in js.get("meshes", []):
        for prim in mesh.get("primitives", []):
            p = _glb_array(data, js, bin_off, prim["attributes"]["POSITION"], 3)
            if p is None or p.dtype != np.float32:
                return None, "GLB uses compressed/quantized positions (no preview)"
            pts.append(p)
            if prim.get("mode", 4) == 4 and "indices" in prim:
                ix = _glb_array(data, js, bin_off, prim["indices"], 1)
                if ix is not None:
                    ix = ix.reshape(-1).astype(np.int64)
                    ix = ix[: len(ix) // 3 * 3].reshape(-1, 3)
                    ix = ix[(ix < len(p)).all(axis=1)]
                    tris.append(p[ix])
            elif prim.get("mode", 4) == 4 and len(p) % 3 == 0:
                tris.append(p.reshape(-1, 3, 3))
    if not pts:
        return None, "No meshes in GLB"
    extra = "\n(part positions not applied - pieces may overlap)"
    if tris:
        v = y_up_to_z_up(np.concatenate(tris).reshape(-1, 3)).reshape(-1, 3, 3)  # glTF is Y-up
        v = v[np.isfinite(v).all(axis=(1, 2))]
        v, part = _thin(v)
        if len(v):
            img, info = _mesh_splat(v, size, part)
            return img, info + extra
    cloud = _finite(y_up_to_z_up(np.concatenate(pts)))
    if not len(cloud):
        return None, "GLB holds only invalid numbers (damaged file?)"
    img, info = _splat(cloud, None, size)
    return img, info + extra


# ---------------------------------------------------------------- embedded thumbnails
def _is_blank(img: QImage) -> bool:
    """True for a single flat colour (e.g. Photon's empty image_0.png)."""
    small = img.scaled(48, 48).convertToFormat(QImage.Format_Grayscale8)
    arr = np.frombuffer(small.constBits(), dtype=np.uint8, count=small.bytesPerLine() * 48)
    return int(arr.max()) - int(arr.min()) < 3


def _salvage_png(path, size):
    """A zip-based file cut off at the end (interrupted download/copy: no central directory) can still
    hold its thumbnail intact near the start. Accept only a COMPLETE PNG (signature .. IEND) that
    decodes and isn't blank - never a partial picture. Seen on a real .3mf 2026-11-12."""
    with open(path, "rb") as f:
        data = f.read(8_000_000)
    i = data.find(b"\x89PNG\r\n\x1a\n")
    j = data.find(b"IEND", i) if i >= 0 else -1
    if i < 0 or j < 0:
        return None, "Damaged file (incomplete zip) - no thumbnail to recover"
    img = QImage.fromData(data[i:j + 8])
    if img.isNull() or _is_blank(img):
        return None, "Damaged file (incomplete zip) - no thumbnail to recover"
    return _fit(img, size), "Thumbnail recovered from a damaged / incomplete file"


def _zip_image(path, size, candidates):
    if not zipfile.is_zipfile(path):
        return _salvage_png(path, size)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for pat in candidates:
            for n in names:
                if re.search(pat, n, re.I) and z.getinfo(n).file_size <= 30_000_000:
                    img = QImage.fromData(z.read(n))
                    if not img.isNull() and not _is_blank(img):
                        return _fit(img, size), "Built-in thumbnail saved by the program"
    return None, "No thumbnail saved inside this file"


def _dib_to_qimage(dib: bytes):
    """Windows DIB (BITMAPINFOHEADER + palette + pixels) -> QImage via an in-memory BMP."""
    bpp, clr = struct.unpack_from("<H", dib, 14)[0], struct.unpack_from("<I", dib, 32)[0]
    pal = (clr or (1 << bpp)) * 4 if bpp <= 8 else 0
    bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, 14 + 40 + pal) + dib
    img = QImage.fromData(bmp, "BMP")
    return None if img.isNull() else img


def preview_cdr(path, size):
    if zipfile.is_zipfile(path):  # CorelDRAW X4 and newer
        return _zip_image(path, size, [r"previews/page1\.png$", r"previews/thumbnail\.png$"])
    # older CorelDRAW (RIFF): 'DISP' chunk = 4-byte prefix + DIB. Used only when
    # header + palette + pixels add up to exactly the chunk size (checked on real files 2026-11-10).
    with open(path, "rb") as f:
        data = f.read(200_000)
    i = data.find(b"DISP")
    if data[:4] != b"RIFF" or i < 0:
        return None, "Old CorelDRAW file without a preview"
    n = struct.unpack_from("<I", data, i + 4)[0]
    chunk = data[i + 8:i + 8 + n]
    if len(chunk) < n and n <= 16_000_000:  # preview bigger than the first 200 KB: read all of it
        with open(path, "rb") as f:
            f.seek(i + 8)
            chunk = f.read(n)
    if len(chunk) != n:
        return None, "Old CorelDRAW file with a cut-off preview"
    if len(chunk) < 44 or struct.unpack_from("<I", chunk, 4)[0] != 40:
        return None, "Old CorelDRAW file without a readable preview"
    w, h, planes, bpp, comp, _, _, _, clr = struct.unpack_from("<iiHHIIiiI", chunk, 8)
    pal = (clr or (1 << bpp)) * 4 if bpp <= 8 else 0
    need = 4 + 40 + pal + ((w * bpp + 31) // 32 * 4) * abs(h)
    if comp != 0 or planes != 1 or need != n:
        return None, "Old CorelDRAW file without a readable preview"
    img = _dib_to_qimage(chunk[4:])
    return (_fit(img, size), "CorelDRAW's saved preview (old format)") if img else (None, "Preview unreadable")


def preview_3mf(path, size):
    img, note = _zip_image(path, size, [r"Metadata/plate_1\.png$", r"Metadata/top_1\.png$",
                                        r"thumbnail.*\.png$", r"\.png$"])
    if img is not None or not zipfile.is_zipfile(path):  # damaged zip: no mesh to read either
        return img, note
    # no thumbnail: draw the meshes stored in the file (triangles; vertices if there are none)
    pts, tris, total = [], [], 0
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if not info.filename.lower().endswith(".model"):
                continue
            total += info.file_size
            if total > MESH_XML_MAX:  # XML is decoded + searched in memory: ~5x its size in RAM
                return None, "3MF too big to preview (no thumbnail inside)"
            xml = z.read(info).decode("utf-8", "ignore")
            for mesh in re.findall(r"<(?:\w+:)?mesh\b.*?</(?:\w+:)?mesh>", xml, re.S):
                v = re.findall(r'<(?:\w+:)?vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"', mesh)
                if not v:
                    continue
                t = re.findall(r'<(?:\w+:)?triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"', mesh)
                with np.errstate(over="ignore"):  # 1e40 -> inf, dropped by _finite below
                    va = np.array(v, dtype=np.float64).astype(np.float32)
                ta = np.array(t, dtype=np.int64).reshape(-1, 3)
                ta = ta[(ta < len(va)).all(axis=1)]
                pts.append(_finite(va))
                if len(ta):
                    tris.append(_finite(va[ta]))
    tris = [t for t in tris if len(t)]
    if not pts or not sum(len(p) for p in pts):
        return None, "No thumbnail and no mesh inside this 3MF"
    if tris:
        v = np.concatenate(tris)
        v, part = _thin(v)
        img, info = _mesh_splat(v, size, part)
    else:
        img, info = _splat(np.concatenate(pts), None, size)
    return img, info + "\n(drawn from the mesh - parts shown at their own positions)"


def preview_f3d(path, size):
    return _zip_image(path, size, [r"Previews/big\.png$", r"Previews/small\.png$"])


def preview_pwscene(path, size):
    return _zip_image(path, size, [r"preview/image_0\.png$", r"\.png$"])


def preview_gcode(path, size):
    """Orca/Prusa/Cura write '; thumbnail begin WxH len' + base64 lines. Take the largest."""
    with open(path, "rb") as f:
        head = f.read(4_000_000).decode("ascii", "ignore").replace("\r\n", "\n")
    blocks = re.findall(r"; thumbnail(?:_\w+)? begin (\d+)x(\d+) \d+\n(.*?); thumbnail(?:_\w+)? end", head, re.S)
    if not blocks:
        return _gcode_toolpath(path, size)
    for w, h, b64 in sorted(blocks, key=lambda b: -int(b[0]) * int(b[1])):  # largest readable one
        try:
            raw = base64.b64decode("".join(l.lstrip("; ").strip() for l in b64.splitlines()))
        except ValueError:
            continue
        img = QImage.fromData(raw)
        if not img.isNull() and not _is_blank(img):
            return _fit(img, size), f"Slicer thumbnail {w}×{h}"
    return _gcode_toolpath(path, size)  # thumbnails unreadable (e.g. QOI): draw the moves


_GX = re.compile(r"X(-?\d+\.?\d*)")
_GY = re.compile(r"Y(-?\d+\.?\d*)")
_GS = re.compile(r"S(-?\d+\.?\d*)")
_LASER_ON = re.compile(r"M0*[34](?!\d)")
_LASER_OFF = re.compile(r"M0*5(?!\d)")  # not M500-M503
_GCMD = re.compile(r"G0*(\d+)(?![\d.])")  # G1 = G01; G91.1 is not G91


def _gcode_toolpath(path, size):
    """No thumbnail: draw the extruding (E) / laser-power (S) G1 moves, top view.
    Absolute XY only: moves after G91 (relative) are not drawn."""
    fsize = os.path.getsize(path)
    step = max(1, fsize // 20_000_000)  # big files: read every n-th 1 MB block (~20 MB in total)
    segs = []
    x = y = None
    rel = False
    power, laser_on = 0.0, False  # laser: S is modal; M3/M4 = on, M5 = off (S kept)
    with open(path, "rb") as f:
        k = 0
        while True:
            block = f.read(1_000_000)
            if not block:
                break
            k += 1
            if step > 1 and k % step:
                x = y = None  # don't join across skipped blocks
                continue
            for line in block.decode("ascii", "ignore").splitlines():
                line = line.split(";", 1)[0].strip().upper()
                if not line:
                    continue
                m_on, m_off = _LASER_ON.match(line), _LASER_OFF.match(line)
                if m_on:
                    laser_on = True
                elif m_off:
                    laser_on = False
                g = _GCMD.match(line)
                ms = _GS.search(line) if (m_on or g) else None  # never M104/M140/M106 S (temperatures, fan)
                line_s = float(ms.group(1)) if ms else None
                if line_s is not None:
                    power = line_s
                code = int(g.group(1)) if g else None
                if code == 91:
                    rel = True
                elif code == 90:
                    rel = False
                if rel or code not in (0, 1):
                    continue
                mx, my = _GX.search(line), _GY.search(line)
                nx = float(mx.group(1)) if mx else x
                ny = float(my.group(1)) if my else y
                draw = code == 1 and ("E" in line or (line_s or 0) > 0 or (laser_on and power > 0))
                if draw and x is not None and y is not None and nx is not None and ny is not None:
                    segs.append((x, y, nx, ny))
                x, y = nx, ny
    if len(segs) < 2:
        return None, "No thumbnail and no drawable moves in this G-code"
    a = np.array(segs, dtype=np.float64)
    if len(a) > 400_000:
        a = a[:: len(a) // 400_000 + 1]
    mn = np.minimum(a[:, :2].min(0), a[:, 2:].min(0))
    mxv = np.maximum(a[:, :2].max(0), a[:, 2:].max(0))
    dims = mxv - mn
    sc = (size - 20) / max(dims[0], dims[1], 1e-9)
    ox, oy = (size - dims[0] * sc) / 2, (size - dims[1] * sc) / 2
    from PySide6.QtCore import QLineF
    img = QImage(size, size, QImage.Format_RGB32)
    img.fill(QColor(245, 245, 245))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(200, 80, 20, 90), 0.8))
    p.drawLines([QLineF(ox + (r[0] - mn[0]) * sc, size - (oy + (r[1] - mn[1]) * sc),
                        ox + (r[2] - mn[0]) * sc, size - (oy + (r[3] - mn[1]) * sc)) for r in a])
    p.end()
    note = f"Toolpath drawn from the G-code (top view), {dims[0]:.1f} × {dims[1]:.1f} mm"
    if step > 1:
        note += f"\n(big file: every {step}th MB read)"
    return img, note


def preview_3dm(path, size):
    """Rhino keeps a preview bitmap (BITMAPINFOHEADER + zlib-compressed pixels) near the start.
    Accepted ONLY if the zlib data unpacks to exactly width*height*bytes - otherwise no preview
    (an uncompressed-looking header alone produced garbage on real files)."""
    import zlib
    with open(path, "rb") as f:
        data = f.read(4_000_000)
    for m in re.finditer(re.escape(b"\x28\x00\x00\x00"), data):
        i = m.start()
        if i + 40 > len(data):
            break
        w, h, planes, bpp, comp = struct.unpack_from("<iiHHI", data, i + 4)
        if not (16 <= w <= 4096 and 16 <= abs(h) <= 4096 and planes == 1 and bpp in (24, 32) and comp == 0):
            continue
        need = ((w * bpp // 8 + 3) & ~3) * abs(h)
        for k in range(i + 40, min(i + 136, len(data) - 2)):
            if data[k] == 0x78 and data[k + 1] in (0x01, 0x5E, 0x9C, 0xDA):
                try:
                    pix = zlib.decompressobj().decompress(data[k:k + need + 2_000_000], need + 1)
                except zlib.error:
                    continue
                if len(pix) == need:
                    dib = data[i:i + 40] + pix
                    bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, 54) + dib
                    img = QImage.fromData(bmp, "BMP")
                    if not img.isNull():
                        return _fit(img, size), "Rhino's saved preview"
    return None, "No readable preview saved in this Rhino file"


def preview_lychee(path, size):
    """Lychee stores a PNG preview: .lyt near the start, .lys a few MB before the end
    (checked on real files 2026-11-10). mmap = no full read of 800 MB scenes."""
    import mmap
    sig = b"\x89PNG\r\n\x1a\n"
    if os.path.getsize(path) == 0:
        return None, "Empty file"
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        n = m.size()
        i = m.find(sig, 0, min(n, 4_000_000))
        if i < 0:
            i = m.rfind(sig, max(0, n - 64_000_000), n)
        if i < 0:
            return None, "No preview saved in this Lychee file"
        j = m.find(b"IEND", i)
        img = QImage.fromData(m[i:(j + 8) if j > 0 else min(n, i + 30_000_000)])
    if img.isNull():
        return None, "Lychee preview unreadable"
    if _is_blank(img):  # seen on a real .lys 2026-11-11: a plain white picture
        return None, "Lychee's saved preview is blank"
    return _fit(img, size), "Lychee's saved preview"


def _rgb565(buf, w, h, big_endian):
    v = np.frombuffer(buf, dtype=">u2" if big_endian else "<u2", count=w * h).astype(np.uint32).reshape(h, w)
    r = ((v >> 11) & 31) * 255 // 31
    g = ((v >> 5) & 63) * 255 // 63
    b = (v & 31) * 255 // 31
    rgb = np.ascontiguousarray(np.dstack([r, g, b]).astype(np.uint8))
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


def preview_anycubic(path, size):
    """Anycubic Photon slices (pm3m, pwmx, ...): 'PREVIEW' section = length, width, mark, height,
    then width*height RGB565 pixels. Checked on real files 2026-11-10 (224x168)."""
    with open(path, "rb") as f:
        data = f.read(600_000)
    if not data.startswith(b"ANYCUBIC"):
        return None, "Not an Anycubic slice file"
    i = data.find(b"PREVIEW\0")
    if i < 0:
        return None, "No preview in this Anycubic file"
    length, w, _mark, h = struct.unpack_from("<IIII", data, i + 12)
    if not (16 <= w <= 2000 and 16 <= h <= 2000) or length < w * h * 2 or i + 28 + w * h * 2 > len(data):
        return None, "Anycubic preview unreadable"
    return _fit(_rgb565(data[i + 28:i + 28 + w * h * 2], w, h, False), size), f"Slicer preview {w}×{h}"


def preview_cxdlp(path, size):
    """Creality .cxdlp: after the header come 116x116, 290x290, 290x290 RGB565 (big-endian)
    previews, each followed by CR LF. Used only if both CR LF markers sit exactly where expected."""
    with open(path, "rb") as f:
        data = f.read(600_000)
    if data[4:12] != b"CXSW3DV2":
        return None, "Not a Creality CXDLP file"
    small, big = 116 * 116 * 2, 290 * 290 * 2
    for o in range(16, 400):
        if data[o + small:o + small + 2] == b"\r\n" and data[o + small + 2 + big:o + small + 4 + big] == b"\r\n":
            return _fit(_rgb565(data[o + small + 2:o + small + 2 + big], 290, 290, True), size), "Slicer preview 290×290"
    return None, "CXDLP preview not found"


_STM_POLY = re.compile(r"\nPolyline\s*\n(.*?)\nPolylineEnd", re.S)
_STM_PT = re.compile(r"201:(-?[\d.eE+-]+)\s*\n\s*202:(-?[\d.eE+-]+)")


def preview_stm(path, size):
    """Laser .stm (text, 'File Ver 3.0'): draws every Polyline's CSPoint (201=x, 202=y) = the laser paths,
    including fill/hatch lines. Bezier control data is not drawn (meaning not decoded)."""
    if os.path.getsize(path) > 200_000_000:
        return None, "STM too big to preview"
    with open(path, "r", errors="ignore") as f:
        txt = f.read()
    if not txt.startswith("File Ver"):
        return None, "Not a readable STM file"
    polys = []
    for m in _STM_POLY.finditer(txt):
        pts = [(float(x), float(y)) for x, y in _STM_PT.findall(m.group(1))]
        if len(pts) >= 2:
            polys.append(pts)
    if not polys:
        return None, "This STM only holds curve (Bezier) data - not drawn (format not decoded yet)"
    return _draw_2d(polys, size, "Laser paths from the file (incl. fill lines)")


def _draw_2d(polys, size, label, extra=""):
    """Draw 2D polylines (lists of (x, y)) fitted into a size x size image, Y up."""
    polys = [p for p in polys if len(p) and np.isfinite(np.asarray(p, dtype=np.float64)).all()]
    if not polys:
        return None, f"{label}: nothing drawable (only invalid numbers)"
    allp = np.array([pt for p in polys for pt in p], dtype=np.float64)
    mn, mx = allp.min(0), allp.max(0)
    dims = mx - mn
    sc = (size - 20) / max(dims[0], dims[1], 1e-9)
    ox, oy = (size - dims[0] * sc) / 2, (size - dims[1] * sc) / 2
    img = QImage(size, size, QImage.Format_RGB32)
    img.fill(QColor(245, 245, 245))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(20, 20, 20), 1.0))
    for poly in polys:
        path_ = QPainterPath()
        for k, (x, y) in enumerate(poly):
            q = QPointF(ox + (x - mn[0]) * sc, size - (oy + (y - mn[1]) * sc))
            path_.moveTo(q) if k == 0 else path_.lineTo(q)
        p.drawPath(path_)
    p.end()
    return img, f"{label}: {dims[0]:.1f} × {dims[1]:.1f}" + extra


# ---------------------------------------------------------------- 2D
def preview_svg(path, size):
    from PySide6.QtSvg import QSvgRenderer
    r = QSvgRenderer(path)
    if not r.isValid():
        return None, "SVG not readable"
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(QColor(245, 245, 245))
    p = QPainter(img)
    vb = r.viewBoxF()
    sc = min(size / max(vb.width(), 1e-6), size / max(vb.height(), 1e-6))
    w, h = vb.width() * sc, vb.height() * sc
    r.render(p, QRectF((size - w) / 2, (size - h) / 2, w, h))
    p.end()
    return img, ""


def _pdf_page(path, size):
    from PySide6.QtPdf import QPdfDocument
    doc = QPdfDocument()
    if doc.load(path) != QPdfDocument.Error.None_ or doc.pageCount() == 0:
        return None, "Not readable as PDF"
    pg = doc.pagePointSize(0)
    sc = size / max(pg.width(), pg.height(), 1)
    img = doc.render(0, (pg * sc).toSize())
    if img.isNull():
        return None, "PDF render failed"
    out = QImage(img.size(), QImage.Format_RGB32)
    out.fill(Qt.white)
    p = QPainter(out); p.drawImage(0, 0, img); p.end()
    return out, f"page 1 of {doc.pageCount()}"


def preview_ai(path, size):
    with open(path, "rb") as f:
        if f.read(5) != b"%PDF-":
            return None, "Old Illustrator format (not PDF-based) - no preview"
    return _pdf_page(path, size)


def preview_pdf(path, size):
    return _pdf_page(path, size)


def _dxf_entities(path):
    """Yield (type, [(code, value), ...]) for every entity in the ENTITIES section, in file order."""
    with open(path, "r", errors="ignore") as f:
        lines = f.read().lstrip("\ufeff").splitlines()
    in_ent, prev, ent, cur = False, None, None, []
    for i in range(0, len(lines) - 1, 2):
        code, val = lines[i].strip(), lines[i + 1].strip()
        if code == "2" and prev == ("0", "SECTION"):
            in_ent = val == "ENTITIES"
        prev = (code, val)
        if not in_ent:
            continue
        if code == "0":
            if ent is not None:
                yield ent, cur
            if val == "ENDSEC":
                return
            ent, cur = val, []
        elif ent is not None:
            cur.append((code, val))
    if ent is not None and ent != "ENDSEC":
        yield ent, cur


def _f(pairs, code, default=None):
    for c, v in pairs:
        if c == code:
            return float(v)
    if default is None:
        raise KeyError(code)
    return default


def _all(pairs, code):
    return [float(v) for c, v in pairs if c == code]


def _bulge_arc(p0, p1, bulge, n=16):
    """Points of the arc between p0 and p1 for a DXF bulge (tan of a quarter of the included angle)."""
    (x0, y0), (x1, y1) = p0, p1
    theta = 4 * math.atan(bulge)
    chord = math.hypot(x1 - x0, y1 - y0)
    if chord == 0 or abs(bulge) < 1e-9:
        return [p1]
    r = chord / (2 * math.sin(theta / 2))
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    d = r * math.cos(theta / 2)  # signed distance from chord midpoint to centre
    ux, uy = -(y1 - y0) / chord, (x1 - x0) / chord
    cx, cy = mx + ux * d, my + uy * d
    a0 = math.atan2(y0 - cy, x0 - cx)
    return [(cx + abs(r) * math.cos(a0 + theta * k / n), cy + abs(r) * math.sin(a0 + theta * k / n))
            for k in range(1, n + 1)]


def _bspline(ctrl, knots, degree, weights=None, n=None):
    """Evaluate a (rational) B-spline with de Boor's algorithm, vectorised over all sample points.
    Returns points, or None if the data doesn't form a valid spline (caller then falls back)."""
    m = len(ctrl)
    if degree < 1 or m <= degree or len(knots) != m + degree + 1:
        return None
    U = np.asarray(knots, dtype=np.float64)
    if np.any(np.diff(U) < 0):
        return None
    w = np.asarray(weights if weights and len(weights) == m else [1.0] * m, dtype=np.float64)
    C = np.asarray(ctrl, dtype=np.float64)
    P = np.column_stack([C * w[:, None], w])  # homogeneous (x*w, y*w, w)
    t0, t1 = U[degree], U[m]
    if t1 <= t0:
        return None
    n = n or min(200, max(24, m * 4))
    t = np.linspace(t0, t1, n)
    k = np.clip(np.searchsorted(U, t, side="right") - 1, degree, m - 1)  # knot span of each t
    d = np.stack([P[k - degree + j] for j in range(degree + 1)], axis=1)  # (n, degree+1, 3)
    for r in range(1, degree + 1):
        for j in range(degree, r - 1, -1):
            lo = U[j + k - degree]
            hi = U[j + 1 + k - r]
            den = hi - lo
            alpha = np.where(den == 0, 0.0, (t - lo) / np.where(den == 0, 1, den))
            d[:, j] = (1 - alpha)[:, None] * d[:, j - 1] + alpha[:, None] * d[:, j]
    out = d[:, degree]
    if np.any(out[:, 2] == 0):
        return None
    return list(zip(out[:, 0] / out[:, 2], out[:, 1] / out[:, 2]))


DXF_MAX = 40_000_000  # whole file parsed in memory (~25x its size in RAM, ~0.4 s per MB)


def preview_dxf(path, size):
    """Draws LINE, LWPOLYLINE (with arcs/bulges), POLYLINE, CIRCLE, ARC, ELLIPSE and SPLINE
    (real B-spline curve) from the ENTITIES section. Blocks (INSERT), text and hatches are not drawn."""
    if os.path.getsize(path) > DXF_MAX:
        return None, "DXF too big to preview"
    with open(path, "rb") as f:
        if f.read(22).startswith(b"AutoCAD Binary DXF"):
            return None, "Binary DXF - no preview"
    polys, skipped, approx = [], 0, 0
    poly_open = False
    for ent, g in _dxf_entities(path):
        try:
            if ent == "LINE":
                polys.append([(_f(g, "10"), _f(g, "20")), (_f(g, "11"), _f(g, "21"))])
            elif ent == "LWPOLYLINE":
                verts = []  # [x, y, bulge]
                for c, v in g:
                    if c == "10":
                        verts.append([float(v), None, 0.0])
                    elif c == "20" and verts:
                        verts[-1][1] = float(v)
                    elif c == "42" and verts:
                        verts[-1][2] = float(v)
                verts = [v for v in verts if v[1] is not None]
                if len(verts) < 2:
                    skipped += 1
                    continue
                closed = int(_f(g, "70", 0)) & 1
                pts = [(verts[0][0], verts[0][1])]
                seq = verts + ([verts[0]] if closed else [])
                for v0, v1 in zip(seq, seq[1:]):
                    pts += _bulge_arc((v0[0], v0[1]), (v1[0], v1[1]), v0[2])
                polys.append(pts)
            elif ent == "CIRCLE":
                cx, cy, r = _f(g, "10"), _f(g, "20"), _f(g, "40")
                polys.append([(cx + r * math.cos(t), cy + r * math.sin(t)) for t in np.linspace(0, 2 * math.pi, 72)])
            elif ent == "ARC":
                cx, cy, r = _f(g, "10"), _f(g, "20"), _f(g, "40")
                a0, a1 = math.radians(_f(g, "50")), math.radians(_f(g, "51"))
                if a1 < a0:
                    a1 += 2 * math.pi
                polys.append([(cx + r * math.cos(t), cy + r * math.sin(t)) for t in np.linspace(a0, a1, 48)])
            elif ent == "ELLIPSE":
                cx, cy, mx_, my_, ratio = _f(g, "10"), _f(g, "20"), _f(g, "11"), _f(g, "21"), _f(g, "40")
                t0, t1 = _f(g, "41", 0.0), _f(g, "42", 2 * math.pi)
                if t1 < t0:
                    t1 += 2 * math.pi
                polys.append([(cx + mx_ * math.cos(t) - my_ * ratio * math.sin(t),
                               cy + my_ * math.cos(t) + mx_ * ratio * math.sin(t)) for t in np.linspace(t0, t1, 72)])
            elif ent == "SPLINE":
                ctrl = list(zip(_all(g, "10"), _all(g, "20")))
                fit = list(zip(_all(g, "11"), _all(g, "21")))
                pts = _bspline(ctrl, _all(g, "40"), int(_f(g, "71", 3)), _all(g, "41"))
                if pts is None:
                    pts = fit if len(fit) >= 2 else ctrl
                    approx += 1
                if len(pts) >= 2:
                    polys.append(pts)
            elif ent == "POLYLINE":
                polys.append([])
                poly_open = True
            elif ent == "VERTEX":
                if poly_open and polys:
                    polys[-1].append((_f(g, "10"), _f(g, "20")))
            elif ent == "SEQEND":
                poly_open = False
            else:
                skipped += 1
        except (KeyError, IndexError, ValueError, ZeroDivisionError, OverflowError):
            skipped += 1
    polys = [p for p in polys if len(p) >= 2]
    if not polys:
        return None, "No drawable lines in this DXF (maybe only blocks/text)"
    extra = ""
    if skipped:
        extra += f"\n{skipped} items not drawn (text, blocks, hatches)"
    if approx:
        extra += f"\n{approx} splines drawn approximately (incomplete spline data)"
    return _draw_2d(polys, size, "Drawing (drawing units)", extra)


IMAGE_EXTS = ("jpg", "jpeg", "png", "webp", "bmp", "gif", "tif", "tiff")
FULL_PIXELS = 24_000_000  # a photo opened for zooming is decoded to at most this many pixels (~96 MB)


def read_image(path, size=None, max_pixels=None):
    """(QImage, (width, height) of the file). Turned the way the camera held it (EXIF), and decoded
    straight to the wanted size, so a 50-megapixel photo never sits in memory at full size."""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImageReader
    rd = QImageReader(path)
    rd.setAutoTransform(True)
    full = rd.size()
    if full.isValid() and full.width() > 0 and full.height() > 0:
        k = 1.0
        if size:
            k = min(1.0, size / max(full.width(), full.height()))
        elif max_pixels and full.width() * full.height() > max_pixels:
            k = (max_pixels / (full.width() * full.height())) ** 0.5
        if k < 1.0:
            rd.setScaledSize(QSize(max(1, round(full.width() * k)), max(1, round(full.height() * k))))
    img = rd.read()
    if img.isNull():
        return img, (0, 0)
    w, h = (full.width(), full.height()) if full.isValid() else (img.width(), img.height())
    if (img.width() > img.height()) != (w > h):  # the camera's turn swapped the sides
        w, h = h, w
    return img, (w, h)


def preview_image(path, size):
    img, (w, h) = read_image(path, size)
    if img.isNull():
        return None, "Image not readable"
    return (img if max(img.width(), img.height()) <= size else _fit(img, size)), f"{w}×{h} px"


def full_image(path):
    """The photo itself for zooming (None if unreadable). At most FULL_PIXELS pixels."""
    try:
        img, _ = read_image(path, max_pixels=FULL_PIXELS)
    except Exception:  # noqa: BLE001  a broken file must never stop the preview thread
        return None
    return None if img.isNull() else img


# ---------------------------------------------------------------- text
TEXT_EXTS = ("txt", "md", "markdown", "rst", "log", "nfo", "csv", "tsv", "json", "xml", "yaml", "yml", "toml",
             "ini", "cfg", "conf", "desktop", "srt", "sub", "py", "js", "ts", "html", "htm", "css", "sh", "bat",
             "ps1", "c", "h", "cpp", "hpp", "java", "rs", "go", "lua", "sql", "tex", "scad", "mtl", "cue", "m3u")
TEXT_LIMIT = 200_000  # bytes of a text file that are read and shown
AUDIO_EXTS = ("mp3", "wav", "flac", "ogg", "oga", "opus", "m4a", "aac", "wma")


def looks_like_text(path):
    """True if the first 4 KB hold no binary bytes (for files whose ending says nothing)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if not head or b"\0" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    odd = sum(1 for b in head if b < 9 or 13 < b < 32 and b != 27)
    return odd <= len(head) // 100


def is_text(path):
    """Should this file be shown as text? Its ending says so, or (for an ending with no picture
    preview) its first bytes do."""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in TEXT_EXTS:
        return True
    if ext in PREVIEWERS or ext in AUDIO_EXTS:
        return False
    import archives
    if archives.split(path):
        return False  # unknown ending inside an archive: not unpacked just to look
    try:
        return os.path.isfile(path) and looks_like_text(path)
    except OSError:
        return False


def _decode(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        if e.start >= len(raw) - 4:  # only the last letter is cut off (the file is longer than what was read)
            return raw[:e.start].decode("utf-8-sig", "replace")
    for old in ("cp1250", "latin-1"):  # older Windows text: Central European first, then Western
        try:
            return raw.decode(old)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def text_preview(path, limit=TEXT_LIMIT):
    """(text | None, note). The first `limit` bytes; never raises."""
    import archives
    tmpdir = None
    try:
        real = path
        if archives.split(path):
            real, tmpdir = archives.extract_to_temp(path)
            if real is None:
                return None, tmpdir
        if not os.path.isfile(real):
            return None, "File not found (moved, deleted, or its drive is unplugged)"
        size = os.path.getsize(real)
        with open(real, "rb") as f:
            raw = f.read(limit)
        text = _decode(raw)
        lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        note = f"Text, {size:,} bytes" + (f" - showing the first {limit // 1000} KB" if size > limit
                                          else f", {lines:,} lines")
        return text, ("Inside archive. " + note) if tmpdir else note
    except Exception as e:  # noqa: BLE001
        return None, f"Preview failed: {type(e).__name__}: {e}"
    finally:
        if tmpdir and isinstance(tmpdir, str) and os.path.isdir(tmpdir):
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


VIDEO_EXTS = ("mp4", "mkv", "mov", "avi", "webm", "m4v", "wmv", "flv", "mpg", "mpeg", "m2ts", "3gp")  # not .ts (TypeScript too)


def _clock(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def video_info(path):
    """{'duration', 'width', 'height', 'codec'} from ffprobe (read-only), or {} if it can't tell."""
    import json
    import shutil
    import subprocess
    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "format=duration:stream=codec_name,width,height", "-of", "json", path],
                             capture_output=True, timeout=8, stdin=subprocess.DEVNULL)
        data = json.loads(out.stdout or b"{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    st = (data.get("streams") or [{}])[0]
    try:
        dur = float(data.get("format", {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    return {"duration": dur, "width": st.get("width"), "height": st.get("height"), "codec": st.get("codec_name")}


def preview_video(path, size):
    """One real frame (about 10 % in, at most 5 min in) via ffmpeg at low priority; never guessed."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return None, "Video (install ffmpeg to see a picture)"
    info = video_info(path)
    if not info.get("width"):
        return None, "Video: no picture stream found (damaged, or audio only)"
    t = min(info["duration"] * 0.1, 300) if info.get("duration") else 1.0
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale='min({size * 2},iw)':-2", "-f", "image2pipe", "-vcodec", "png", "-"]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10"] + cmd
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None, "Video: the picture took too long (slow drive?)"
    img = QImage.fromData(out.stdout, "PNG")
    if img.isNull():
        return None, "Video: no frame could be read"
    bits = [f"{info['width']}×{info['height']}"]
    if info.get("duration"):
        bits.append(_clock(info["duration"]))
    if info.get("codec"):
        bits.append(info["codec"])
    return _fit(img, size), "Video · " + " · ".join(bits) + f"  (frame at {_clock(t)})"


PREVIEWERS = {
    "stl": preview_stl, "obj": preview_obj, "glb": preview_glb,
    "cdr": preview_cdr, "3mf": preview_3mf, "f3d": preview_f3d, "pwscene": preview_pwscene,
    "gcode": preview_gcode, "3dm": preview_3dm, "lys": preview_lychee, "lyt": preview_lychee,
    "cxdlp": preview_cxdlp, "stm": preview_stm,
    **{e: preview_anycubic for e in ("pm3m", "pwmx", "pwma", "pm3", "pm4n", "pwmo", "pwms", "pm3n", "pwmb")},
    "svg": preview_svg, "ai": preview_ai, "pdf": preview_pdf, "dxf": preview_dxf,
    **{e: preview_image for e in IMAGE_EXTS},
    **{e: preview_video for e in VIDEO_EXTS},
}


def render(path, size=420):
    """(QImage | None, note). Never raises: one bad file must not stop the preview thread."""
    try:
        return _render(path, size)
    except Exception as e:  # noqa: BLE001
        return None, f"Preview failed: {type(e).__name__}: {e}"


def _render(path, size):
    import archives
    if archives.split(path):  # a file inside an archive: preview a temp copy of just that member
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext not in PREVIEWERS:
            return None, f"No preview for .{ext} files" if ext else "No preview (file has no extension)"
        if ext in VIDEO_EXTS:  # could be gigabytes: never unpacked just for a picture
            return None, "Video inside an archive: open the archive to play it"
        tmp, tmpdir = archives.extract_to_temp(path)
        if tmp is None:
            return None, tmpdir
        try:
            img, note = render(tmp, size)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        return img, ("Inside archive. " + note) if note else "Inside archive"
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    fn = PREVIEWERS.get(ext)
    if fn is None:
        return None, f"No preview for .{ext} files" if ext else "No preview (file has no extension)"
    if not os.path.isfile(path):
        return None, "File not found (moved, deleted, or its drive is unplugged)"
    if os.path.getsize(path) < 4096:
        with open(path, "rb") as f:
            head = f.read(8)
        if head in (b"\x01\0\0\0\0\0\0\0", b"\x02\0\0\0\0\0\0\0"):
            return None, "Windows recycle-bin record (only the deleted file's name and size) - not a real model"
    try:
        img, note = fn(path, size)
    except Exception as e:  # a broken file must never crash the app
        img, note = None, f"Preview failed: {type(e).__name__}: {e}"
    if img is None and ext != "lys" and _looks_like_lychee(path):  # e.g. a .lys saved as ".stl"
        img2, note2 = preview_lychee(path, size)
        if img2 is not None:
            return img2, note2 + f"\n(this is really a Lychee file, named .{ext})"
    return img, note


def _looks_like_lychee(path):
    try:
        with open(path, "rb") as f:
            return b'"mangoFiles"' in f.read(400)
    except OSError:
        return False
