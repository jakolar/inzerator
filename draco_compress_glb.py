"""Re-pack a single-primitive GLB through Draco. Replaces what gltf-pipeline
does, but in-process via DracoPy so we don't hit the V8 heap limit on
800 MB+ GLBs."""
import json
import struct
import sys
from pathlib import Path

import numpy as np
import DracoPy

# glTF componentType numeric codes
CT_F32 = 5126
CT_U32 = 5125
CT_U16 = 5123


def _read_accessor(json_obj, bin_data, acc_idx, dtype, comp_count):
    acc = json_obj["accessors"][acc_idx]
    bv = json_obj["bufferViews"][acc["bufferView"]]
    off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    n = acc["count"] * comp_count
    arr = np.frombuffer(bin_data, dtype=dtype, count=n, offset=off)
    return arr.reshape(acc["count"], comp_count) if comp_count > 1 else arr


def compress(src_path, dst_path, quant_pos=16, comp_level=7):
    src_path = Path(src_path)
    dst_path = Path(dst_path)
    with open(src_path, "rb") as f:
        data = f.read()
    if data[:4] != b"glTF":
        raise ValueError(f"{src_path} is not a GLB")
    if struct.unpack("<I", data[4:8])[0] != 2:
        raise ValueError(f"{src_path} is not glTF v2")
    jlen = struct.unpack("<I", data[12:16])[0]
    json_obj = json.loads(data[20:20 + jlen])
    bin_data = data[20 + jlen + 8:]   # skip BIN chunk header

    prim = json_obj["meshes"][0]["primitives"][0]
    pos_acc = json_obj["accessors"][prim["attributes"]["POSITION"]]
    uv_acc = json_obj["accessors"][prim["attributes"]["TEXCOORD_0"]]
    idx_acc = json_obj["accessors"][prim["indices"]]

    pos = _read_accessor(json_obj, bin_data, prim["attributes"]["POSITION"],
                         np.float32, 3).astype(np.float64)  # DracoPy wants float64
    uvs = _read_accessor(json_obj, bin_data, prim["attributes"]["TEXCOORD_0"],
                         np.float32, 2).astype(np.float64)
    idx_dtype = {CT_U32: np.uint32, CT_U16: np.uint16}[idx_acc["componentType"]]
    idx = _read_accessor(json_obj, bin_data, prim["indices"],
                         idx_dtype, 1).astype(np.uint32)

    # Strip orphan vertices (no incident face — created by gen_*'s hole
    # carving). DracoPy.encode silently drops them, but the accessor.count
    # we write must MATCH the Draco-encoded count, otherwise three.js
    # interprets later indices into nothing and the mesh renders as
    # scattered triangles / disappears. gltf-pipeline does this dedupe
    # automatically; DracoPy doesn't.
    used = np.zeros(len(pos), dtype=bool)
    used[idx] = True
    n_orphans = int((~used).sum())
    if n_orphans:
        remap = -np.ones(len(pos), dtype=np.int64)
        remap[used] = np.arange(int(used.sum()))
        idx = remap[idx].astype(np.uint32)
        pos = pos[used]
        uvs = uvs[used]
        print(f"  {src_path.name}: dropped {n_orphans:,} orphan verts "
              f"(carved-hole interiors with no incident face)")

    print(f"  {src_path.name}: {len(pos):,} verts, {len(idx)//3:,} faces")
    draco_blob = DracoPy.encode(
        pos,
        faces=idx,
        tex_coord=uvs,
        quantization_bits=quant_pos,
        compression_level=comp_level,
    )

    # DracoPy may dedup exact-duplicate positions inside encode() — at the
    # skirt corners each terrain corner vert is shadowed by TWO skirt verts
    # at the same X,Z (one from each side strip), and identical Y. The
    # decoder hands back fewer verts than we passed in. The glTF accessor's
    # count MUST match what the decoder produces, otherwise three.js
    # interprets later indices into nothing and the mesh renders as
    # scattered triangles. Decode the blob once to learn the truth.
    decoded = DracoPy.decode(draco_blob)
    actual_n = len(np.asarray(decoded.points).reshape(-1, 3))
    if actual_n != len(pos):
        print(f"  {src_path.name}: encoder dedup {len(pos) - actual_n} duplicate-position verts")

    pos_min = pos.min(axis=0).tolist()
    pos_max = pos.max(axis=0).tolist()
    new_json = {
        "asset": {"version": "2.0", "generator": "draco_compress_glb.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "extensionsRequired": ["KHR_draco_mesh_compression"],
        "extensionsUsed": ["KHR_draco_mesh_compression"],
        "meshes": [{
            "primitives": [{
                "mode": 4,
                "attributes": {"POSITION": 1, "TEXCOORD_0": 2},
                "indices": 0,
                "extensions": {
                    "KHR_draco_mesh_compression": {
                        "bufferView": 0,
                        # DracoPy 2.0 assigns unique_ids in attribute_type
                        # order, NOT the order we pass to encode(). It
                        # iterates attribute types alphabetically: tex_coord
                        # (type 3) gets id 0, then position (type 0) gets
                        # id 1 — confirmed by encode-then-decode probe.
                        # Earlier 'POSITION: 0, TEXCOORD_0: 1' fed UV bytes
                        # into three.js's position buffer and vice-versa,
                        # producing a mesh with itemSize=2 positions, UVs
                        # in the hundreds, and a degenerate bbox.
                        "attributes": {"POSITION": 1, "TEXCOORD_0": 0},
                    },
                },
            }],
        }],
        "accessors": [
            {"componentType": CT_U32, "count": int(len(idx)), "type": "SCALAR"},
            {"componentType": CT_F32, "count": int(actual_n), "type": "VEC3",
             "min": pos_min, "max": pos_max},
            {"componentType": CT_F32, "count": int(actual_n), "type": "VEC2"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(draco_blob)},
        ],
        "buffers": [{"byteLength": len(draco_blob)}],
    }

    json_bytes = json.dumps(new_json, separators=(",", ":")).encode("utf-8")
    json_pad = (4 - len(json_bytes) % 4) % 4
    json_bytes += b" " * json_pad
    bin_pad = (4 - len(draco_blob) % 4) % 4
    bin_padded = draco_blob + b"\x00" * bin_pad
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_padded)

    tmp = dst_path.with_suffix(dst_path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(b"glTF")
        f.write(struct.pack("<II", 2, total))
        f.write(struct.pack("<II", len(json_bytes), 0x4E4F534A))
        f.write(json_bytes)
        f.write(struct.pack("<II", len(bin_padded), 0x004E4942))
        f.write(bin_padded)
    tmp.replace(dst_path)
    print(f"  → {dst_path.name}: {dst_path.stat().st_size / 1048576:.2f} MB "
          f"({100 * dst_path.stat().st_size / src_path.stat().st_size:.1f}% of original)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: draco_compress_glb.py <input.glb> <output.glb> "
              "[quant_pos=16] [comp_level=7]")
        sys.exit(1)
    args = sys.argv[1:]
    kw = {"quant_pos": 16, "comp_level": 7}
    if len(args) > 2:
        kw["quant_pos"] = int(args[2])
    if len(args) > 3:
        kw["comp_level"] = int(args[3])
    compress(args[0], args[1], **kw)
