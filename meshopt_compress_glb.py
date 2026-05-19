"""Compress single-mesh GLBs through gltfpack (meshoptimizer).

Pipeline: pre-snap source positions to 14-bit uniform-cube grid in Python,
then run `gltfpack -cc -vpf -vtf -kv -noq` so meshopt stores Float32
attributes without KHR_mesh_quantization or gltfpack's EXPONENTIAL attribute
filter. Client-side: no node TRS, no bake — Three.js sees Float32 world-coord
positions directly.

Why -noq: the default `-cc -vpf -vtf -kv` output was visually zubaté vs.
Draco on Kamyk inner; adding `-noq` removed the artefact while retaining
EXT_meshopt_compression. See docs/notes/2026-05-19-meshopt-zoby.md.
"""
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

GLTFPACK_VERSION = "0.24.0"
SNAP_BITS = 14   # 16383 grid points per axis; matches Draco's old quant_pos


def _find_gltfpack() -> list[str]:
    p = shutil.which("gltfpack")
    if p:
        return [p]
    pnpm = shutil.which("pnpm")
    if pnpm:
        return [pnpm, "dlx", f"gltfpack@{GLTFPACK_VERSION}"]
    raise RuntimeError(
        f"gltfpack not found; install with `pnpm i -g gltfpack@{GLTFPACK_VERSION}`."
    )


def _snap_positions_to_grid(src_path: str, dst_path: str, bits: int = SNAP_BITS) -> None:
    """Round POSITION float32 to a uniform-cube N-bit grid based on the
    largest bbox-axis span (matches Draco quant_pos behaviour). Per-axis
    snapping would give Y a much finer grid on the 1 km inner because Y
    range is only ~170 m → SM5 sensor noise (5-10 mm) would survive and
    appear as stair-stepping on regular features."""
    import json as _json
    import numpy as np

    with open(src_path, "rb") as f:
        data = f.read()
    json_len = struct.unpack_from("<I", data, 12)[0]
    gltf = _json.loads(data[20:20 + json_len].decode())
    bin_offset = 20 + json_len + 8

    prim = gltf["meshes"][0]["primitives"][0]
    if "POSITION" not in prim["attributes"]:
        raise ValueError(f"{src_path}: primitive has no POSITION attribute")
    acc = gltf["accessors"][prim["attributes"]["POSITION"]]
    bv = gltf["bufferViews"][acc["bufferView"]]
    if acc.get("componentType") != 5126:
        raise ValueError(f"{src_path}: POSITION must be Float32 (got {acc.get('componentType')})")
    off = bin_offset + bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    n = acc["count"]

    pos = np.frombuffer(data, dtype="<f4", count=n * 3, offset=off).reshape(n, 3).copy()
    bbox_min = pos.min(axis=0)
    bbox_max = pos.max(axis=0)
    span_scalar = float((bbox_max - bbox_min).max())
    if span_scalar == 0:
        span_scalar = 1.0
    levels = (1 << bits) - 1
    snapped = (np.round((pos.astype(np.float64) - bbox_min) / span_scalar * levels)
               * span_scalar / levels + bbox_min).astype("<f4")

    out = bytearray(data)
    out[off:off + snapped.nbytes] = snapped.tobytes()
    with open(dst_path, "wb") as f:
        f.write(out)


def compress(src_path, dst_path) -> None:
    src = str(Path(src_path).resolve())
    dst = str(Path(dst_path).resolve())
    with tempfile.NamedTemporaryFile(suffix=".glb", delete=False) as tmp:
        snapped_path = tmp.name
    try:
        _snap_positions_to_grid(src, snapped_path, bits=SNAP_BITS)
        cmd = _find_gltfpack() + [
            "-i", snapped_path,
            "-o", dst,
            "-cc",
            "-vpf",
            "-vtf",
            "-kv",
            "-noq",
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
    finally:
        try:
            Path(snapped_path).unlink()
        except OSError:
            pass


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: meshopt_compress_glb.py <input.glb> <output.glb>")
        sys.exit(1)
    compress(sys.argv[1], sys.argv[2])
    print(f"  → {Path(sys.argv[2]).stat().st_size / 1048576:.2f} MB")
