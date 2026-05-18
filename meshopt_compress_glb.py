"""Compress single-mesh GLBs through gltfpack (meshoptimizer) with -cc.

Why meshopt instead of Draco:
- Wire size after brotli q=11: ~10× smaller than Draco (entropy-coded Draco
  resists further compression; meshopt's bit-packed delta encoding shrinks
  hugely under brotli).
- Decode speed: 5-10× faster on the browser side.
- Quality: 16-bit position quantization by default = 1.5 cm error on a
  1 km bbox, better than current Draco quant_pos=14 (6 cm).

Trade-offs:
- Output requires `EXT_meshopt_compression` + `KHR_mesh_quantization`
  extensions; viewer must use MeshoptDecoder. Modern Three.js (≥ r130)
  supports both.
- gltfpack is a Node-based CLI; we shell out. Pinned to a known version
  via `pnpm dlx`.
"""
import shutil
import subprocess
import sys
from pathlib import Path

# Pinned to avoid surprise upgrades (per workspace supply-chain rules).
GLTFPACK_VERSION = "0.24.0"


def _find_gltfpack() -> list[str]:
    """Return the argv prefix that invokes gltfpack. Prefer a PATH install
    (fastest), fall back to `pnpm dlx` (uses pnpm's store, downloads on
    first call then cached)."""
    p = shutil.which("gltfpack")
    if p:
        return [p]
    pnpm = shutil.which("pnpm")
    if pnpm:
        return [pnpm, "dlx", f"gltfpack@{GLTFPACK_VERSION}"]
    raise RuntimeError(
        f"gltfpack not found on PATH and pnpm unavailable. "
        f"Install with `npm i -g gltfpack@{GLTFPACK_VERSION}` or `pnpm i -g "
        f"gltfpack@{GLTFPACK_VERSION}`."
    )


def compress(src_path, dst_path) -> None:
    """Run gltfpack on `src_path`, writing meshopt-compressed GLB to
    `dst_path`. Raises subprocess.CalledProcessError on encode failure.

    Flags:
      -c: standard meshopt encoding (we don't use -cc; user reported
        visible aliasing on flat features like football-pitch lines and
        moving to the slightly less aggressive encoder removes any
        suspicion of encoder-side rounding/reordering artefacts. Both
        are documented as lossless for float32 attributes but -c is
        the conservative choice; wire cost ~3.4 vs 2.6 MB per inner glb).
      -vpf: float32 positions instead of 14-bit integers — avoids the
        KHR_mesh_quantization node TRS dequantize bake on the viewer
        side (a 144 MB Float32 copy across 3 detail meshes locked low-
        spec Macs during initial load).
      -vtf: float32 tex coords (same reason as -vpf).
      -kv: keep vertex attributes even if not referenced by any material.
        gen_detail emits the source GLB without a material binding (the
        viewer assigns its own MeshBasicMaterial post-load); without -kv
        gltfpack treats TEXCOORD_0 as dead and strips it → black mesh
        because Three.js can't sample the ortofoto texture without UVs.

    Wire size (per inner.glb, brotli q=11): ~3.4 MB; ČR full inner-only
    coverage ≈ 270 GB.
    """
    src = str(Path(src_path).resolve())
    dst = str(Path(dst_path).resolve())
    cmd = _find_gltfpack() + ["-i", src, "-o", dst, "-c", "-vpf", "-vtf", "-kv"]
    # Capture stderr so failures don't pollute the job log with raw gltfpack
    # noise; subprocess.CalledProcessError carries it for the caller.
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,  # 10 min ceiling per detail; inner takes ~5s on M-class
    )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: meshopt_compress_glb.py <input.glb> <output.glb>")
        sys.exit(1)
    compress(sys.argv[1], sys.argv[2])
    print(f"  → {Path(sys.argv[2]).stat().st_size / 1048576:.2f} MB")
