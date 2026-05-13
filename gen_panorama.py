"""Generate one continuous panorama mesh for a v2 region.

DMR5G only; one GLB per region. Cardinal: scene +Z = world +Y (north).
"""
import argparse, json, urllib.request
from pathlib import Path
import numpy as np
import pygltflib
import rasterio
from pyproj import Transformer

SJTSK_TO_WGS = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)


def fetch_dmr5g(cx, cy, half, step, cache_dir="cache/dmr5g_v2"):
    cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"dmr5g_{int(cx)}_{int(cy)}_{int(half)}_{step}.tif"
    if cache_path.exists() and cache_path.stat().st_size > 1000:
        return cache_path
    n = int(round(2 * half / step)) + 1
    n = max(50, min(2048, n))
    bbox = f"{cx-half},{cy-half},{cx+half},{cy+half}"
    url = (
        f"https://ags.cuzk.gov.cz/arcgis/rest/services/3D/dmr5g/ImageServer/exportImage"
        f"?bbox={bbox}&bboxSR=5514&imageSR=5514&size={n},{n}"
        f"&format=tiff&pixelType=F32&f=image"
    )
    tmp_path = cache_path.with_suffix('.tif.tmp')
    with urllib.request.urlopen(url, timeout=120) as r:
        tmp_path.write_bytes(r.read())
    tmp_path.replace(cache_path)
    return cache_path


def build_panorama(dmr5g_path, cx, cy, half, step):
    """Return {vertices, faces, uvs, sjtsk_bbox, wgs_bbox, ground_z}.

    Convention: scene +Z = world +Y. world_y(local_z) = cy + local_z.
    """
    with rasterio.open(dmr5g_path) as ds:
        data = ds.read(1)
    valid = (data > 0) & (data != -9999.0)
    if not valid.any():
        raise RuntimeError(
            f"No valid DMR5G pixels in {dmr5g_path}; "
            f"bbox may be outside Czechia or fetch returned blank tile"
        )
    ground_z = float(np.percentile(data[valid], 5))

    n = int(round(2 * half / step)) + 1
    data_h, data_w = data.shape

    vertices, world_coords = [], []
    for r in range(n):
        for c in range(n):
            local_x = -half + c * step
            local_z = -half + r * step
            world_x = cx + local_x
            world_y = cy + local_z
            # rasterio row 0 = top of bbox = max world_y; with scene +Z = world +Y,
            # the vertex at the largest local_z reads data row 0. Clamp to last
            # valid pixel for the outer row/col when merge returns (n-1, n-1).
            dr = min((n - 1) - r, data_h - 1)
            dc = min(c, data_w - 1)
            if valid[dr, dc]:
                y = float(data[dr, dc]) - ground_z
            else:
                y = 0.0
            vertices.append([local_x, y, local_z])
            world_coords.append((world_x, world_y))

    faces = []
    for r in range(n - 1):
        for c in range(n - 1):
            i = r * n + c
            faces.append([i, i + n, i + 1])
            faces.append([i + 1, i + n, i + n + 1])

    sx = np.array([w[0] for w in world_coords])
    sy = np.array([w[1] for w in world_coords])
    all_lon, all_lat = SJTSK_TO_WGS.transform(sx, sy)
    lon_min, lon_max = float(all_lon.min()), float(all_lon.max())
    lat_min, lat_max = float(all_lat.min()), float(all_lat.max())
    uvs = []
    for i in range(len(vertices)):
        u = (float(all_lon[i]) - lon_min) / (lon_max - lon_min)
        v_uv = (float(all_lat[i]) - lat_min) / (lat_max - lat_min)
        uvs.append([u, v_uv])

    return {
        "vertices": vertices,
        "faces": faces,
        "uvs": uvs,
        "ground_z": ground_z,
        "sjtsk_bbox": [cx - half, cy - half, cx + half, cy + half],
        "wgs_bbox": [lon_min, lat_min, lon_max, lat_max],
    }


def add_skirt(tile, depth, half, step):
    """Emit four skirt strips (S/N/W/E) dropped by `depth`. UVs duplicated from parent.

    Mutates `tile` in place (appends to its vertices/faces/uvs lists).
    """
    n = int(round(2 * half / step)) + 1
    vertices = tile["vertices"]
    faces = tile["faces"]
    uvs = tile["uvs"]

    def emit_strip(edge_vert_indices, outward):
        skirt_start = len(vertices)
        for vi in edge_vert_indices:
            vx, vy, vz = vertices[vi]
            vertices.append([vx, vy - depth, vz])
            uvs.append(list(uvs[vi]))  # duplicate UV row (independent list)
        for k in range(len(edge_vert_indices) - 1):
            t1, t2 = edge_vert_indices[k], edge_vert_indices[k + 1]
            s1, s2 = skirt_start + k, skirt_start + k + 1
            # Winding for outward normal:
            #   S edge (c increasing, outward -Z) and E edge (r increasing, outward +X)
            #     → forward winding (t1,t2,s1),(s1,t2,s2)
            #   N edge (c increasing, outward +Z) and W edge (r increasing, outward -X)
            #     → reverse winding (t2,t1,s2),(s2,t1,s1)
            if outward in ('S', 'E'):
                faces.append([t1, t2, s1])
                faces.append([s1, t2, s2])
            else:  # 'N', 'W'
                faces.append([t2, t1, s2])
                faces.append([s2, t1, s1])

    # S edge: r=0, varying c. Vertex index = 0*n + c = c.
    emit_strip([c for c in range(n)], 'S')
    # N edge: r=n-1, varying c. Vertex index = (n-1)*n + c.
    emit_strip([(n - 1) * n + c for c in range(n)], 'N')
    # W edge: c=0, varying r. Vertex index = r*n.
    emit_strip([r * n for r in range(n)], 'W')
    # E edge: c=n-1, varying r. Vertex index = r*n + (n-1).
    emit_strip([r * n + (n - 1) for r in range(n)], 'E')


def save_glb(tile, output_path):
    """Write the mesh as a GLB. Vertices already in scene-local coords
    (no offset baking — the viewer places the mesh at scene origin)."""
    verts = np.array(tile["vertices"], dtype=np.float32)
    faces = np.array(tile["faces"], dtype=np.uint32)
    uvs = np.array(tile["uvs"], dtype=np.float32)

    # 601² > 65 536, must use uint32 indices
    if len(verts) < 65536:
        faces_packed = faces.astype(np.uint16)
        idx_component = pygltflib.UNSIGNED_SHORT
    else:
        faces_packed = faces
        idx_component = pygltflib.UNSIGNED_INT

    verts_bytes = verts.tobytes()
    faces_bytes = faces_packed.tobytes()
    uvs_bytes = uvs.tobytes()

    # GLB spec: bufferViews containing FLOAT data must start at 4-byte aligned
    # offsets. Pad each block to a multiple of 4; bufferView byteOffset uses the
    # padded cumulative size, byteLength stays at the unpadded data size.
    def _pad4(b):
        pad = (-len(b)) % 4
        return b + b'\x00' * pad

    verts_bytes_p = _pad4(verts_bytes)
    faces_bytes_p = _pad4(faces_bytes)
    uvs_bytes_p = _pad4(uvs_bytes)
    all_bytes = verts_bytes_p + faces_bytes_p + uvs_bytes_p

    gltf = pygltflib.GLTF2(
        scene=0,
        scenes=[pygltflib.Scene(nodes=[0])],
        nodes=[pygltflib.Node(mesh=0)],
        meshes=[pygltflib.Mesh(primitives=[
            pygltflib.Primitive(
                attributes=pygltflib.Attributes(POSITION=0, TEXCOORD_0=2),
                indices=1,
            )
        ])],
        accessors=[
            pygltflib.Accessor(bufferView=0, componentType=pygltflib.FLOAT,
                               count=len(verts), type="VEC3",
                               max=verts.max(axis=0).tolist(),
                               min=verts.min(axis=0).tolist()),
            pygltflib.Accessor(bufferView=1, componentType=idx_component,
                               count=faces_packed.size, type="SCALAR"),
            pygltflib.Accessor(bufferView=2, componentType=pygltflib.FLOAT,
                               count=len(uvs), type="VEC2"),
        ],
        bufferViews=[
            pygltflib.BufferView(buffer=0, byteOffset=0,
                                 byteLength=len(verts_bytes),
                                 target=pygltflib.ARRAY_BUFFER),
            pygltflib.BufferView(buffer=0, byteOffset=len(verts_bytes_p),
                                 byteLength=len(faces_bytes),
                                 target=pygltflib.ELEMENT_ARRAY_BUFFER),
            pygltflib.BufferView(buffer=0,
                                 byteOffset=len(verts_bytes_p) + len(faces_bytes_p),
                                 byteLength=len(uvs_bytes),
                                 target=pygltflib.ARRAY_BUFFER),
        ],
        buffers=[pygltflib.Buffer(byteLength=len(all_bytes))],
    )
    gltf.set_binary_blob(all_bytes)
    gltf.save(output_path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--center-sjtsk", required=True,
                   help="cx,cy (use --center-sjtsk=cx,cy for negative ČÚZK coords)")
    p.add_argument("--half", type=int, default=15000)
    p.add_argument("--step", type=int, default=50)
    p.add_argument("--skirt", type=int, default=200)
    args = p.parse_args()

    cx, cy = (float(s) for s in args.center_sjtsk.split(","))
    out_dir = Path(f"tiles_v2_{args.region}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching DMR5G for {args.region} ({cx}, {cy}) — {args.half*2/1000:.0f}km square …")
    dmr = fetch_dmr5g(cx, cy, args.half, args.step)
    tile = build_panorama(dmr, cx, cy, args.half, args.step)
    add_skirt(tile, args.skirt, args.half, args.step)
    glb_path = out_dir / "panorama.glb"
    save_glb(tile, str(glb_path))

    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"details": []}
    manifest["region"] = {
        "slug": args.region,
        "center_sjtsk": [cx, cy],
        "half": args.half, "step": args.step,
        "ground_z": tile["ground_z"],
        "sjtsk_bbox": tile["sjtsk_bbox"],
        "wgs_bbox": tile["wgs_bbox"],
        "panorama_glb": "panorama.glb",
    }
    manifest_tmp = manifest_path.with_suffix('.json.tmp')
    manifest_tmp.write_text(json.dumps(manifest, indent=2))
    manifest_tmp.replace(manifest_path)
    print(f"Wrote {glb_path} ({glb_path.stat().st_size // 1024} KB) and {manifest_path}")


if __name__ == "__main__":
    main()
