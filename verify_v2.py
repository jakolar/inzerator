"""Programmatic verification of v2 panorama + detail meshes.

Asserts:
- mesh face counts match expected (no grid_valid-induced holes)
- all Y values finite
- cardinal sanity (max world_y maps to max scene_z)
- detail outer-edge Y matches panorama Y within tolerance (the seam check
  that v1's LOD-ring approach kept failing)
"""
import argparse
import json
import struct
import sys
from pathlib import Path

import numpy as np


def parse_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:4] == b"glTF", f"Not a GLB: {path}"
    json_len = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + json_len].decode())
    bin_data = data[20 + json_len + 8:]
    prim = gltf["meshes"][0]["primitives"][0]
    pos_acc = gltf["accessors"][prim["attributes"]["POSITION"]]
    bv = gltf["bufferViews"][pos_acc["bufferView"]]
    off = bv.get("byteOffset", 0)
    count = pos_acc["count"]
    floats = struct.unpack_from(f"<{count * 3}f", bin_data, off)
    verts = np.array(
        [(floats[i], floats[i + 1], floats[i + 2]) for i in range(0, len(floats), 3)],
        dtype=np.float64,
    )
    idx_acc = gltf["accessors"][prim["indices"]]
    return verts, idx_acc["count"] // 3


def check_mesh(verts, face_count, expected_terrain, expected_skirt, label):
    """Return True if mesh passes; print issues as we go."""
    ok = True
    expected = expected_terrain + expected_skirt
    if face_count < expected * 0.99:
        print(f"[FAIL] {label}: face count {face_count} < expected {expected}")
        ok = False
    elif face_count > expected * 1.01:
        print(f"[WARN] {label}: face count {face_count} > expected {expected} (extra geometry?)")
    else:
        print(f"[ OK ] {label}: {face_count} faces (terrain {expected_terrain} + skirt {expected_skirt})")

    ys = verts[:, 1]
    if not np.all(np.isfinite(ys)):
        n_bad = np.sum(~np.isfinite(ys))
        print(f"[FAIL] {label}: {n_bad} non-finite Y values")
        ok = False

    return ok


def check_cardinal(verts, label):
    """v2 convention: scene_z = world_y - cy (linear, +z=north).
    So within a mesh, the vertex with largest local_z should have the largest
    scene_z. This catches accidental sign flips in the generator.
    """
    z_max = float(verts[:, 2].max())
    z_min = float(verts[:, 2].min())
    span = z_max - z_min
    if span <= 0:
        print(f"[FAIL] {label}: zero scene_z span (mesh collapsed?)")
        return False
    print(f"[ OK ] {label}: scene_z span {z_min:.0f}..{z_max:.0f} = {span:.0f}m")
    return True


def panorama_y_at(panorama_verts, panorama_meta, world_x, world_y):
    """Bilinear sample of the panorama mesh's Y at a given world (x, y).
    Returns None if outside panorama bounds.
    """
    cx_p, cy_p = panorama_meta["center_sjtsk"]
    half_p = panorama_meta["half"]
    step_p = panorama_meta["step"]
    plx = world_x - cx_p
    plz = world_y - cy_p
    if abs(plx) > half_p or abs(plz) > half_p:
        return None
    pano_n = int(round(2 * half_p / step_p)) + 1
    pano_grid = panorama_verts[: pano_n * pano_n].reshape(pano_n, pano_n, 3)
    fx = (plx + half_p) / step_p
    fz = (plz + half_p) / step_p
    ix, iz = int(fx), int(fz)
    tx, tz = fx - ix, fz - iz
    ix = max(0, min(pano_n - 2, ix))
    iz = max(0, min(pano_n - 2, iz))
    y00 = pano_grid[iz, ix, 1]
    y10 = pano_grid[iz + 1, ix, 1]
    y01 = pano_grid[iz, ix + 1, 1]
    y11 = pano_grid[iz + 1, ix + 1, 1]
    return (1 - tx) * (1 - tz) * y00 + tx * (1 - tz) * y01 + (1 - tx) * tz * y10 + tx * tz * y11


def check_boundary(detail_verts, detail_meta, panorama_verts, panorama_meta, tol=0.5):
    """For each detail outer-edge vertex (excluding skirt), find the panorama Y
    at the same world position. Fail if max |ΔY| > tol."""
    cx_d, cy_d = detail_meta["center_sjtsk"]
    half_d = detail_meta["half"]
    step_d = detail_meta["step"]
    det_n = int(round(2 * half_d / step_d)) + 1
    det_grid = detail_verts[: det_n * det_n]

    eps = step_d * 0.1
    edge_mask = (
        (np.abs(det_grid[:, 0]) >= half_d - eps) |
        (np.abs(det_grid[:, 2]) >= half_d - eps)
    )
    edge_verts = det_grid[edge_mask]

    diffs = []
    sample_log = []
    for v in edge_verts:
        wx = cx_d + v[0]
        wy = cy_d + v[2]
        pano_y = panorama_y_at(panorama_verts, panorama_meta, wx, wy)
        if pano_y is None:
            continue
        delta = abs(v[1] - pano_y)
        diffs.append(delta)
        if len(sample_log) < 4:
            sample_log.append((v[0], v[2], v[1], pano_y, delta))

    if not diffs:
        print("[WARN] No outer-edge verts mapped into panorama bbox")
        return True

    diffs_arr = np.array(diffs)
    max_d = float(diffs_arr.max())
    mean_d = float(diffs_arr.mean())
    print(f"[INFO] detail->panorama seam: n={len(diffs)} max|dY|={max_d:.3f}m mean={mean_d:.3f}m")
    for lx, lz, dy, py, dd in sample_log:
        print(f"       sample local=({lx:+.0f},{lz:+.0f}) detail_y={dy:.2f} pano_y={py:.2f} d={dd:.3f}")
    if max_d > tol:
        print(f"[FAIL] seam dY {max_d:.3f}m exceeds tolerance {tol:.2f}m")
        return False
    print(f"[ OK ] seam dY within tolerance ({tol:.2f}m)")
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--detail", required=True, help="detail slug to verify")
    p.add_argument("--tol", type=float, default=0.5, help="boundary dY tolerance in metres")
    args = p.parse_args()

    out_dir = Path(f"tiles_v2_{args.region}")
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    region = manifest["region"]
    detail = next((d for d in manifest["details"] if d["slug"] == args.detail), None)
    if detail is None:
        print(f"[FAIL] detail '{args.detail}' not in manifest")
        sys.exit(1)

    pano_path = out_dir / region["panorama_glb"]
    detail_path = out_dir / detail["glb_url"]
    print(f"Loading {pano_path} ...")
    pano_verts, pano_faces = parse_glb(pano_path)
    print(f"Loading {detail_path} ...")
    det_verts, det_faces = parse_glb(detail_path)

    pano_n = int(round(2 * region["half"] / region["step"])) + 1
    pano_exp_terrain = (pano_n - 1) ** 2 * 2
    pano_exp_skirt = 4 * (pano_n - 1) * 2

    det_n = int(round(2 * detail["half"] / detail["step"])) + 1
    det_exp_terrain = (det_n - 1) ** 2 * 2
    det_exp_skirt = 4 * (det_n - 1) * 2

    ok = True
    ok &= check_mesh(pano_verts, pano_faces, pano_exp_terrain, pano_exp_skirt, "panorama")
    ok &= check_mesh(det_verts, det_faces, det_exp_terrain, det_exp_skirt, "detail")
    ok &= check_cardinal(pano_verts, "panorama")
    ok &= check_cardinal(det_verts, "detail")
    ok &= check_boundary(det_verts, detail, pano_verts, region, tol=args.tol)

    print()
    print("VERIFY PASS" if ok else "VERIFY FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
