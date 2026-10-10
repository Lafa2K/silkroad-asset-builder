from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image

try:
    from Crypto.Cipher import Blowfish
except ImportError:  # Show a friendly message only when the PK2 tab is used.
    Blowfish = None


APP_NAME = "Silkroad Asset Builder"
BG = "#07111d"
PANEL = "#0d1b2a"
PANEL_2 = "#112438"
TEXT = "#eef6ff"
MUTED = "#8fa8bf"
GOLD = "#f3b33d"
CYAN = "#43c8e8"
SUCCESS = "#65d79d"
ERROR = "#ff6b78"
SUPPORTED_TEXTURES = {".png", ".jpg", ".jpeg", ".bmp", ".dds"}
PK2_HEADER_SIZE = 256
PK2_BLOCK_SIZE = 2560
PK2_ENTRY_SIZE = 128
PK2_ENTRIES_PER_BLOCK = 20
PK2_BLOWFISH_KEY = bytes((0x32, 0xCE, 0xDD, 0x7C, 0xBC, 0xA8))
COPY_CHUNK_SIZE = 1024 * 1024


class AssetError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class Pk2FileEntry:
    relative_path: Path
    position: int
    size: int


class Pk2Archive:
    """Read-only reader for standard Silkroad PK2 archives."""

    def __init__(self, path: Path) -> None:
        if Blowfish is None:
            raise AssetError(
                "PK2 extraction requires pycryptodome. Run: pip install -r requirements.txt"
            )
        self.path = path
        self._cipher = Blowfish.new(PK2_BLOWFISH_KEY, Blowfish.MODE_ECB)
        self._visited_blocks: set[int] = set()

    @staticmethod
    def _decode_name(raw: bytes) -> str:
        value = raw.split(b"\0", 1)[0]
        for encoding in ("cp1252", "utf-8", "latin-1"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("latin-1", errors="replace")

    @staticmethod
    def _safe_part(name: str) -> str:
        name = name.strip().replace("/", "_").replace("\\", "_")
        if not name or name in {".", ".."}:
            raise AssetError(f"Invalid name inside PK2: {name!r}")
        return name

    def _decrypt_block(self, encrypted: bytes) -> bytes:
        # The original Silkroad reader treats each 32-bit half as little-endian,
        # while modern Blowfish libraries use big-endian words.
        swapped = b"".join(
            encrypted[index : index + 4][::-1]
            + encrypted[index + 4 : index + 8][::-1]
            for index in range(0, len(encrypted), 8)
        )
        decrypted = self._cipher.decrypt(swapped)
        return b"".join(
            decrypted[index : index + 4][::-1]
            + decrypted[index + 4 : index + 8][::-1]
            for index in range(0, len(decrypted), 8)
        )

    def entries(self) -> list[Pk2FileEntry]:
        archive_size = self.path.stat().st_size
        if archive_size < PK2_HEADER_SIZE + PK2_BLOCK_SIZE:
            raise AssetError(f"Invalid or incomplete PK2: {self.path.name}")

        results: list[Pk2FileEntry] = []
        self._visited_blocks.clear()
        with self.path.open("rb") as stream:
            header = stream.read(PK2_HEADER_SIZE)
            if not header.startswith(b"JoyMax File Manager"):
                raise AssetError(f"Unrecognized PK2 header: {self.path.name}")
            self._read_directory(stream, PK2_HEADER_SIZE, Path(), archive_size, results)
        return results

    @staticmethod
    def server_file_markers(entries: list[Pk2FileEntry]) -> list[str]:
        """Detect server-side files that should never be inside a client PK2."""
        exact_names = {
            "agentserver.exe",
            "downloadserver.exe",
            "farmmanager.exe",
            "gatewayserver.exe",
            "globalmanager.exe",
            "machinemanager.exe",
            "sr_gameserver.exe",
            "sr_shardmanager.exe",
            "server.cfg",
        }
        server_folders = {
            "certmodule",
            "serverfiles",
            "silkroadconfigeditor_v28",
            "smc_independent",
        }
        matches = []
        for entry in entries:
            parts = tuple(part.casefold() for part in entry.relative_path.parts)
            if entry.relative_path.name.casefold() in exact_names or any(
                part in server_folders for part in parts
            ):
                matches.append(str(entry.relative_path))
        return matches

    def _read_directory(
        self,
        stream,
        first_block: int,
        parent: Path,
        archive_size: int,
        results: list[Pk2FileEntry],
    ) -> None:
        folders: list[tuple[Path, int]] = []
        block_position = first_block

        while block_position:
            if block_position in self._visited_blocks:
                raise AssetError(f"Circular PK2 chain detected at {block_position}")
            if block_position < PK2_HEADER_SIZE or block_position + PK2_BLOCK_SIZE > archive_size:
                raise AssetError(f"PK2 block lies outside the archive: {block_position}")
            self._visited_blocks.add(block_position)
            stream.seek(block_position)
            encrypted = stream.read(PK2_BLOCK_SIZE)
            if len(encrypted) != PK2_BLOCK_SIZE:
                raise AssetError(f"Truncated PK2 block at {block_position}")
            block = self._decrypt_block(encrypted)

            next_chain = 0
            for index in range(PK2_ENTRIES_PER_BLOCK):
                offset = index * PK2_ENTRY_SIZE
                entry = block[offset : offset + PK2_ENTRY_SIZE]
                entry_type = entry[0]
                name = self._decode_name(entry[1:82])
                position = struct.unpack_from("<Q", entry, 106)[0]
                size = struct.unpack_from("<I", entry, 114)[0]
                chain = struct.unpack_from("<Q", entry, 118)[0]
                if index == PK2_ENTRIES_PER_BLOCK - 1:
                    next_chain = chain

                if entry_type == 1 and name not in {".", ".."}:
                    part = self._safe_part(name)
                    folders.append((parent / part, position))
                elif entry_type == 2:
                    part = self._safe_part(name)
                    if position + size > archive_size:
                        raise AssetError(f"File lies outside the PK2: {parent / part}")
                    results.append(Pk2FileEntry(parent / part, position, size))

            block_position = next_chain

        for folder_path, folder_position in folders:
            self._read_directory(
                stream, folder_position, folder_path, archive_size, results
            )

    def extract(
        self,
        destination: Path,
        entries: list[Pk2FileEntry],
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[int, int]:
        destination.mkdir(parents=True, exist_ok=True)
        destination_root = destination.resolve()
        total_bytes = sum(entry.size for entry in entries)
        written_bytes = 0

        with self.path.open("rb") as source:
            for index, entry in enumerate(entries, 1):
                target = (destination / entry.relative_path).resolve()
                try:
                    target.relative_to(destination_root)
                except ValueError as exc:
                    raise AssetError(f"Unsafe path inside PK2: {entry.relative_path}") from exc

                target.parent.mkdir(parents=True, exist_ok=True)
                source.seek(entry.position)
                remaining = entry.size
                with target.open("wb") as output:
                    while remaining:
                        chunk = source.read(min(COPY_CHUNK_SIZE, remaining))
                        if not chunk:
                            raise AssetError(f"Interrupted read: {entry.relative_path}")
                        output.write(chunk)
                        remaining -= len(chunk)
                        written_bytes += len(chunk)
                if progress:
                    progress(index, len(entries), str(entry.relative_path))

        return len(entries), total_bytes


class Writer:
    def __init__(self) -> None:
        self.data = bytearray()

    @property
    def position(self) -> int:
        return len(self.data)

    def raw(self, value: bytes | bytearray) -> None:
        self.data.extend(value)

    def u8(self, value: int) -> None:
        self.raw(struct.pack("<B", value))

    def u16(self, value: int) -> None:
        self.raw(struct.pack("<H", value))

    def u32(self, value: int) -> None:
        self.raw(struct.pack("<I", value))

    def i32(self, value: int) -> None:
        self.raw(struct.pack("<i", value))

    def f32(self, value: float) -> None:
        self.raw(struct.pack("<f", float(value)))

    def string(self, value: str) -> None:
        encoded = value.encode("utf-8")
        self.u32(len(encoded))
        self.raw(encoded)

    def vec3(self, value: tuple[float, float, float]) -> None:
        for component in value:
            self.f32(component)

    def patch_u32(self, offset: int, value: int) -> None:
        struct.pack_into("<I", self.data, offset, value)


@dataclass(slots=True)
class Mesh:
    positions: list[tuple[float, float, float]]
    normals: list[tuple[float, float, float]]
    uvs: list[tuple[float, float]]
    indices: list[int]

    @property
    def vertex_count(self) -> int:
        return len(self.positions)

    @property
    def face_count(self) -> int:
        return len(self.indices) // 3


def safe_name(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "custom_asset"


def normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = vector
    length = math.sqrt(x * x + y * y + z * z)
    if length <= 1e-12:
        return (0.0, 1.0, 0.0)
    return (x / length, y / length, z / length)


def face_normal(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float]:
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    return normalize(
        (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
    )


def obj_index(raw: str, count: int) -> int:
    value = int(raw)
    index = value - 1 if value > 0 else count + value
    if not 0 <= index < count:
        raise AssetError(f"OBJ index out of range: {value}")
    return index


def transform_position(
    value: tuple[float, float, float], scale: float, axis_mode: str
) -> tuple[float, float, float]:
    x, y, z = value
    if axis_mode == "blender":
        # Conventional Blender Z-up -> Y-up rotation. The default "etheral"
        # mode performs no conversion and matches Etheral's OBJ importer.
        x, y, z = x, z, -y
    return (x * scale, y * scale, z * scale)


def transform_normal(
    value: tuple[float, float, float], axis_mode: str
) -> tuple[float, float, float]:
    x, y, z = value
    if axis_mode == "blender":
        x, y, z = x, z, -y
    return normalize((x, y, z))


def parse_obj(path: Path, scale: float, axis_mode: str) -> Mesh:
    source_positions: list[tuple[float, float, float]] = []
    source_uvs: list[tuple[float, float]] = []
    source_normals: list[tuple[float, float, float]] = []
    triangles: list[list[tuple[int, int | None, int | None]]] = []

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        tag = parts[0]
        try:
            if tag == "v" and len(parts) >= 4:
                source_positions.append(tuple(map(float, parts[1:4])))
            elif tag == "vt" and len(parts) >= 3:
                source_uvs.append((float(parts[1]), float(parts[2])))
            elif tag == "vn" and len(parts) >= 4:
                source_normals.append(tuple(map(float, parts[1:4])))
            elif tag == "f" and len(parts) >= 4:
                polygon: list[tuple[int, int | None, int | None]] = []
                for token in parts[1:]:
                    fields = token.split("/")
                    vi = obj_index(fields[0], len(source_positions))
                    ti = (
                        obj_index(fields[1], len(source_uvs))
                        if len(fields) > 1 and fields[1]
                        else None
                    )
                    ni = (
                        obj_index(fields[2], len(source_normals))
                        if len(fields) > 2 and fields[2]
                        else None
                    )
                    polygon.append((vi, ti, ni))
                for index in range(1, len(polygon) - 1):
                    triangles.append([polygon[0], polygon[index], polygon[index + 1]])
        except (ValueError, IndexError) as exc:
            raise AssetError(f"invalid OBJ on line {line_number}: {exc}") from exc

    if not source_positions or not triangles:
        raise AssetError("the OBJ does not contain valid vertices and faces")

    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    indices: list[int] = []

    # Keep one vertex per face corner. This preserves hard edges and every OBJ
    # position/UV/normal combination without relying on Blender or an OBJ SDK.
    for triangle in triangles:
        converted = [
            transform_position(source_positions[item[0]], scale, axis_mode)
            for item in triangle
        ]
        fallback_normal = face_normal(*converted)
        for corner, converted_position in zip(triangle, converted):
            _, uv_index, normal_index = corner
            positions.append(converted_position)
            uvs.append(source_uvs[uv_index] if uv_index is not None else (0.0, 0.0))
            if normal_index is None:
                normals.append(fallback_normal)
            else:
                normals.append(transform_normal(source_normals[normal_index], axis_mode))
            indices.append(len(indices))

    if len(positions) > 65535:
        raise AssetError(
            f"the mesh generated {len(positions)} vertices; BMS uses 16-bit indices (maximum 65535)"
        )
    return Mesh(positions, normals, uvs, indices)


def bounds(mesh: Mesh) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    xs = [value[0] for value in mesh.positions]
    ys = [value[1] for value in mesh.positions]
    zs = [value[2] for value in mesh.positions]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def build_bms(mesh: Mesh, name: str, material_name: str) -> bytes:
    encoded_name = name.encode("utf-8")
    encoded_material = material_name.encode("utf-8")
    bone_name = "Bone01"

    vertex_offset = 72 + 4 + len(encoded_name) + 4 + len(encoded_material) + 4
    skin_offset = vertex_offset + 4 + 44 * mesh.vertex_count
    face_offset = skin_offset + 4 + 4 + len(bone_name) + 6 * mesh.vertex_count
    cloth_vertex_offset = face_offset + 4 + 6 * mesh.face_count
    cloth_edge_offset = cloth_vertex_offset + 4
    bbox_offset = cloth_edge_offset + 4
    occlusion_offset = bbox_offset + 24
    unknown9_offset = occlusion_offset + 4

    writer = Writer()
    writer.raw(b"JMXVBMS 0110")
    for value in (
        vertex_offset,
        skin_offset,
        face_offset,
        cloth_vertex_offset,
        cloth_edge_offset,
        bbox_offset,
        occlusion_offset,
        0,
        0,
        unknown9_offset,
        0,
        0,
    ):
        writer.u32(value)
    writer.u32(1)  # sub primitive count
    writer.u32(0)  # vertex flags
    writer.u32(0)
    writer.string(name)
    writer.string(material_name)
    writer.u32(15)

    writer.u32(mesh.vertex_count)
    for position, normal, uv in zip(mesh.positions, mesh.normals, mesh.uvs):
        writer.vec3(position)
        writer.vec3(normal)
        writer.f32(uv[0])
        writer.f32(1.0 - uv[1])
        writer.f32(0.0)
        writer.u32(0xFFFFFFFF)
        writer.u32(0)

    writer.u32(1)
    writer.string(bone_name)
    for _ in mesh.positions:
        writer.raw(bytes((0, 255, 255, 255, 0, 0)))

    writer.u32(mesh.face_count)
    for index in mesh.indices:
        writer.u16(index)
    writer.u32(0)  # cloth vertices
    writer.u32(0)  # cloth edges

    minimum, maximum = bounds(mesh)
    writer.vec3(minimum)
    writer.vec3(maximum)
    writer.u32(0)  # occlusion data
    writer.u32(0)  # unknown section

    if writer.position != unknown9_offset + 4:
        raise AssetError(
            f"internal BMS build failure: expected {unknown9_offset + 4}, got {writer.position}"
        )
    return bytes(writer.data)


def color(writer: Writer, values: tuple[float, float, float, float]) -> None:
    for value in values:
        writer.f32(value)


def build_bmt(material_name: str, texture_filename: str) -> bytes:
    writer = Writer()
    writer.raw(b"JMXVBMT 0102")
    writer.u32(1)
    writer.string(material_name)
    color(writer, (0.8, 0.8, 0.8, 1.0))
    color(writer, (0.2, 0.2, 0.2, 1.0))
    color(writer, (0.0, 0.0, 0.0, 1.0))
    color(writer, (0.0, 0.0, 0.0, 1.0))
    writer.f32(0.0)
    writer.u32(320)
    writer.string(texture_filename)
    writer.f32(1.0)
    writer.u8(24)
    writer.u8(0)
    writer.u8(0)
    return bytes(writer.data)


def build_bsr(
    name: str,
    internal_bms: str,
    internal_bmt: str,
    mesh: Mesh,
) -> bytes:
    minimum, maximum = bounds(mesh)
    writer = Writer()
    writer.raw(b"JMXVRES 0109")
    offset_table = writer.position
    for _ in range(8):
        writer.u32(0)

    for _ in range(5):
        writer.u32(0)
    writer.u16(2)
    writer.u16(2)
    writer.string(name)
    writer.i32(0)
    writer.i32(2)
    writer.raw(bytes(40))

    collision_offset = writer.position
    writer.string(internal_bms)
    writer.vec3(minimum)
    writer.vec3(maximum)
    writer.vec3(minimum)
    writer.vec3(maximum)
    writer.u32(0)

    material_offset = writer.position
    writer.u32(1)
    writer.u32(0)
    writer.string(internal_bmt)

    mesh_offset = writer.position
    writer.u32(1)
    writer.string(internal_bms)

    animation_offset = writer.position
    writer.u32(4096)
    writer.u32(0)
    writer.i32(0)

    skeleton_offset = writer.position
    writer.u32(0)

    mesh_groups_offset = writer.position
    writer.i32(1)
    writer.string("default")
    writer.i32(1)
    writer.i32(0)

    animation_groups_offset = writer.position
    writer.i32(0)

    mod_sets_offset = writer.position
    writer.i32(0)
    writer.i32(0)

    for index, value in enumerate(
        (
            material_offset,
            mesh_offset,
            skeleton_offset,
            animation_offset,
            mesh_groups_offset,
            animation_groups_offset,
            mod_sets_offset,
            collision_offset,
        )
    ):
        writer.patch_u32(offset_table + index * 4, value)
    return bytes(writer.data)


def build_ddj(texture_path: Path) -> tuple[bytes, tuple[int, int] | None]:
    if texture_path.suffix.lower() == ".dds":
        dds = texture_path.read_bytes()
        if not dds.startswith(b"DDS "):
            raise AssetError("the selected file does not have a DDS header")
        size = None
    else:
        try:
            image = Image.open(texture_path).convert("RGBA")
        except Exception as exc:
            raise AssetError(f"could not open the texture: {exc}") from exc
        size = image.size
        from io import BytesIO

        stream = BytesIO()
        image.save(stream, format="DDS", pixel_format="DXT1")
        dds = stream.getvalue()

    writer = Writer()
    writer.raw(b"JMXVDDJ 1000")
    writer.u32(len(dds) + 8)
    writer.u32(3)
    writer.raw(dds)
    return bytes(writer.data), size


def validate_mesh_asset(bms: bytes, ddj: bytes) -> None:
    signatures = ((bms, b"JMXVBMS 0110"), (ddj, b"JMXVDDJ 1000"))
    for content, signature in signatures:
        if not content.startswith(signature):
            raise AssetError(f"generated file is missing the {signature.decode('ascii')} signature")

    vertex_offset, skin_offset, face_offset = struct.unpack_from("<III", bms, 12)
    for offset in (vertex_offset, skin_offset, face_offset):
        if not 0 < offset < len(bms):
            raise AssetError("the generated BMS contains an invalid offset")
    if b"DDS " not in ddj[20:24]:
        raise AssetError("the generated DDJ does not contain DDS data")


def validate_package(bms: bytes, bmt: bytes, bsr: bytes, ddj: bytes) -> None:
    validate_mesh_asset(bms, ddj)
    for content, signature in (
        (bmt, b"JMXVBMT 0102"),
        (bsr, b"JMXVRES 0109"),
    ):
        if not content.startswith(signature):
            raise AssetError(f"generated file is missing the {signature.decode('ascii')} signature")


def create_package(
    obj_path: Path,
    texture_path: Path,
    output_root: Path,
    asset_name: str,
    scale: float,
    axis_mode: str,
    asset_mode: str = "PROP SKILL",
    internal_base: str = "res\\custom",
) -> dict:
    if not obj_path.is_file():
        raise AssetError("select a valid OBJ file")
    if not texture_path.is_file():
        raise AssetError("select a valid PNG, DDS, JPG, or BMP texture")
    if scale <= 0:
        raise AssetError("scale must be greater than zero")

    name = safe_name(asset_name or obj_path.stem)
    material_name = f"{name}_material"
    texture_name = f"{safe_name(texture_path.stem)}.ddj"
    mesh = parse_obj(obj_path, scale, axis_mode)
    bms = build_bms(mesh, name, material_name)
    ddj, texture_size = build_ddj(texture_path)

    if asset_mode == "PROP SKILL":
        # Keep the mesh and texture pair under the same normalized asset name.
        # This avoids case/name mismatches when the source image has a different stem.
        texture_name = f"{name}.ddj"
        particles_root = (
            output_root if output_root.name.casefold() == "particles" else output_root / "Particles"
        )
        mesh_folder = particles_root / "meshes"
        texture_folder = particles_root / "textures"
        mesh_folder.mkdir(parents=True, exist_ok=True)
        texture_folder.mkdir(parents=True, exist_ok=True)
        output_folder = mesh_folder
        internal_bms = f"meshes\\{name}.bms"
        internal_ddj = f"textures\\{texture_name}"
        internal_bmt = None
        internal_bsr = None
        validate_mesh_asset(bms, ddj)
        files = {
            "bms": mesh_folder / f"{name}.bms",
            "ddj": texture_folder / texture_name,
        }
        files["bms"].write_bytes(bms)
        files["ddj"].write_bytes(ddj)
    elif asset_mode == "CUSTOM PROP":
        data_root = output_root if output_root.name.casefold() == "data" else output_root / "Data"
        relative_folder = Path(*internal_base.replace("/", "\\").split("\\")) / name
        output_folder = data_root / relative_folder
        output_folder.mkdir(parents=True, exist_ok=True)
        internal_folder = "\\".join((*internal_base.replace("/", "\\").split("\\"), name))
        internal_bms = f"{internal_folder}\\{name}.bms"
        internal_bmt = f"{internal_folder}\\{name}.bmt"
        internal_bsr = f"{internal_folder}\\{name}.bsr"
        internal_ddj = f"{internal_folder}\\{texture_name}"
        bmt = build_bmt(material_name, texture_name)
        bsr = build_bsr(name, internal_bms, internal_bmt, mesh)
        validate_package(bms, bmt, bsr, ddj)
        files = {
            "bms": output_folder / f"{name}.bms",
            "bmt": output_folder / f"{name}.bmt",
            "bsr": output_folder / f"{name}.bsr",
            "ddj": output_folder / texture_name,
        }
        files["bms"].write_bytes(bms)
        files["bmt"].write_bytes(bmt)
        files["bsr"].write_bytes(bsr)
        files["ddj"].write_bytes(ddj)
    else:
        raise AssetError(f"unknown asset type: {asset_mode}")

    minimum, maximum = bounds(mesh)
    manifest = {
        "format": "silkroad-static-skill-asset",
        "name": name,
        "assetMode": asset_mode,
        "source": {"obj": str(obj_path), "texture": str(texture_path)},
        "geometry": {
            "vertices": mesh.vertex_count,
            "faces": mesh.face_count,
            "scale": scale,
            "axisMode": axis_mode,
            "bounds": {"min": minimum, "max": maximum},
        },
        "textureSize": texture_size,
        "internalPaths": {
            "resource": internal_bsr,
            "mesh": internal_bms,
            "material": internal_bmt,
            "texture": internal_ddj,
        },
        "skillUsage": {
            "preferred": "Use BSR for a custom prop; use BMS and DDJ for a mesh rendered directly by an EFP.",
            "efpMesh": "A PROP SKILL is referenced directly as meshes\\name.bms and textures\\name.ddj.",
            "requiresObjectIfo": False,
        },
    }
    manifest_path = output_folder / f"{name}.import.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    files["manifest"] = manifest_path

    return {
        "name": name,
        "folder": output_folder,
        "mesh": mesh,
        "paths": manifest["internalPaths"],
        "files": files,
    }


class PathPicker(tk.Frame):
    def __init__(self, master, label: str, button_text: str, command) -> None:
        super().__init__(master, bg=PANEL)
        self.columnconfigure(0, weight=1)
        tk.Label(
            self,
            text=label.upper(),
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.value = tk.StringVar()
        self.entry = tk.Entry(
            self,
            textvariable=self.value,
            bg="#091522",
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 10),
        )
        self.entry.grid(row=1, column=0, sticky="ew", ipady=9)
        tk.Button(
            self,
            text=button_text,
            command=command,
            bg=PANEL_2,
            fg=CYAN,
            activebackground="#18344e",
            activeforeground=TEXT,
            relief="flat",
            cursor="hand2",
            font=("Segoe UI Semibold", 9),
            padx=15,
        ).grid(row=1, column=1, padx=(8, 0), ipady=7)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("900x840")
        self.minsize(820, 760)
        self.configure(bg=BG)

        self.name_value = tk.StringVar()
        self.scale_value = tk.StringVar(value="1.0")
        self.axis_value = tk.StringVar(value="etheral")
        self.mode_value = tk.StringVar(value="PROP SKILL")
        self.last_output: Path | None = None
        self.ddj_files: list[Path] = []
        self.ddj_folder: Path | None = None
        self.ddj_output = tk.StringVar()
        self.ddj_subfolders = tk.BooleanVar(value=True)
        self.last_ddj_output: Path | None = None
        self.pk2_files: list[Path] = []
        self.pk2_output = tk.StringVar()
        self.pk2_separate_folders = tk.BooleanVar(value=True)
        self.last_pk2_output: Path | None = None

        self._configure_style()
        self._build()

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Silk.Horizontal.TProgressbar",
            troughcolor="#07111d",
            background=GOLD,
            bordercolor="#07111d",
            lightcolor=GOLD,
            darkcolor=GOLD,
        )
        style.configure(
            "Silk.TCombobox",
            fieldbackground="#091522",
            background=PANEL_2,
            foreground=TEXT,
            arrowcolor=CYAN,
            bordercolor=PANEL_2,
        )
        style.map("Silk.TCombobox", fieldbackground=[("readonly", "#091522")])
        style.configure("Silk.TNotebook", background=BG, borderwidth=0)
        style.configure(
            "Silk.TNotebook.Tab",
            background=PANEL,
            foreground=MUTED,
            padding=(20, 10),
            borderwidth=0,
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "Silk.TNotebook.Tab",
            background=[("selected", PANEL_2)],
            foreground=[("selected", TEXT)],
        )
        style.configure(
            "Silk.TCheckbutton",
            background=PANEL,
            foreground=TEXT,
            font=("Segoe UI", 10),
        )
        style.map("Silk.TCheckbutton", background=[("active", PANEL)])

    def _build(self) -> None:
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=34, pady=(26, 16))
        tk.Label(
            header,
            text="SILKROAD",
            bg=BG,
            fg=GOLD,
            font=("Segoe UI Black", 11),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Asset Builder",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 27),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Effects, props, textures, and PK2 tools for Silkroad",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 11),
        ).pack(anchor="w", pady=(3, 0))

        notebook = ttk.Notebook(self, style="Silk.TNotebook")
        notebook.pack(fill="both", expand=True, padx=34, pady=(0, 28))
        prop_page = tk.Frame(notebook, bg=BG)
        ddj_page = tk.Frame(notebook, bg=BG)
        pk2_page = tk.Frame(notebook, bg=BG)
        notebook.add(prop_page, text="  EFFECT / PROP  ")
        notebook.add(ddj_page, text="  DDJ TEXTURES  ")
        notebook.add(pk2_page, text="  EXTRACT PK2  ")

        card = tk.Frame(prop_page, bg=PANEL, padx=22, pady=19)
        card.pack(fill="x", pady=(16, 0))
        card.columnconfigure(0, weight=1)

        self.obj_picker = PathPicker(card, "3D Model", "Select OBJ", self.pick_obj)
        self.obj_picker.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.texture_picker = PathPicker(
            card, "Texture", "Select texture", self.pick_texture
        )
        self.texture_picker.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self.output_picker = PathPicker(
            card, "Extracted root (Data/Media/Particles)", "Select folder", self.pick_output
        )
        self.output_picker.grid(row=2, column=0, sticky="ew")

        settings = tk.Frame(prop_page, bg=BG)
        settings.pack(fill="x", pady=16)
        settings.columnconfigure((0, 1, 2, 3), weight=1, uniform="settings")

        self._field(settings, "ASSET NAME", self.name_value).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        self._field(settings, "SCALE", self.scale_value).grid(
            row=0, column=1, sticky="ew", padx=8
        )

        axis_box = tk.Frame(settings, bg=PANEL, padx=14, pady=12)
        axis_box.grid(row=0, column=2, sticky="nsew", padx=8)
        tk.Label(
            axis_box,
            text="AXIS MODE",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", pady=(0, 5))
        axis = ttk.Combobox(
            axis_box,
            textvariable=self.axis_value,
            state="readonly",
            style="Silk.TCombobox",
            values=("etheral", "blender"),
            font=("Segoe UI", 10),
        )
        axis.pack(fill="x", ipady=5)

        mode_box = tk.Frame(settings, bg=PANEL, padx=14, pady=12)
        mode_box.grid(row=0, column=3, sticky="nsew", padx=(8, 0))
        tk.Label(
            mode_box,
            text="TYPE / DESTINATION",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", pady=(0, 5))
        mode = ttk.Combobox(
            mode_box,
            textvariable=self.mode_value,
            state="readonly",
            style="Silk.TCombobox",
            values=("PROP SKILL", "CUSTOM PROP"),
            font=("Segoe UI", 10),
        )
        mode.pack(fill="x", ipady=5)

        action = tk.Frame(prop_page, bg=BG)
        action.pack(fill="x")
        self.generate_button = tk.Button(
            action,
            text="BUILD SKILL PACKAGE",
            command=self.generate,
            bg=GOLD,
            fg="#1c1304",
            activebackground="#ffd16f",
            activeforeground="#1c1304",
            relief="flat",
            cursor="hand2",
            font=("Segoe UI Black", 11),
            padx=24,
            pady=13,
        )
        self.generate_button.pack(side="left")
        self.open_button = tk.Button(
            action,
            text="Open output",
            command=self.open_result,
            state="disabled",
            bg=PANEL_2,
            fg=CYAN,
            disabledforeground="#52677a",
            relief="flat",
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
            padx=18,
            pady=13,
        )
        self.open_button.pack(side="left", padx=10)

        self.progress = ttk.Progressbar(
            prop_page, style="Silk.Horizontal.TProgressbar", mode="indeterminate"
        )
        self.progress.pack(fill="x", pady=(18, 8))

        result = tk.Frame(prop_page, bg=PANEL, padx=18, pady=15)
        result.pack(fill="both", expand=True)
        tk.Label(
            result,
            text="RESULT / SKILL PATHS",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", pady=(0, 8))
        self.log = tk.Text(
            result,
            bg="#091522",
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            wrap="word",
            height=9,
            font=("Cascadia Mono", 9),
            padx=12,
            pady=10,
        )
        self.log.pack(fill="both", expand=True)
        self._set_log(
            "PROP SKILL writes BMS/DDJ to Particles\\meshes and Particles\\textures.\n"
            "CUSTOM PROP writes a BSR/BMT/BMS/DDJ package to Data\\res\\custom."
        )
        self._build_ddj_page(ddj_page)
        self._build_pk2_page(pk2_page)

    def _tool_button(
        self,
        master,
        text: str,
        command,
        color: str = PANEL_2,
        foreground: str = TEXT,
    ) -> tk.Button:
        return tk.Button(
            master,
            text=text,
            command=command,
            bg=color,
            fg=foreground,
            activebackground=color,
            activeforeground=foreground,
            relief="flat",
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
            padx=17,
            pady=10,
        )

    def _build_ddj_page(self, parent) -> None:
        controls = tk.Frame(parent, bg=PANEL, padx=22, pady=20)
        controls.pack(fill="x", pady=(16, 12))
        controls.columnconfigure(1, weight=1)

        tk.Label(
            controls,
            text="PNG / JPG / DDS  →  DDJ",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 19),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        tk.Label(
            controls,
            text="Convert individual files or an entire texture folder tree.",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 15))

        self._tool_button(controls, "Select files", self.pick_ddj_files).grid(
            row=2, column=0, sticky="w"
        )
        self._tool_button(controls, "Select folder", self.pick_ddj_folder).grid(
            row=2, column=1, sticky="w", padx=10
        )
        ttk.Checkbutton(
            controls,
            text="Include subfolders",
            variable=self.ddj_subfolders,
            style="Silk.TCheckbutton",
        ).grid(row=2, column=2, sticky="w")

        tk.Label(
            controls,
            text="DESTINATION (optional — leave empty to save beside the source)",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(18, 5))
        tk.Entry(
            controls,
            textvariable=self.ddj_output,
            bg="#091522",
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 10),
        ).grid(row=4, column=0, columnspan=2, sticky="ew", ipady=9)
        self._tool_button(controls, "Choose destination", self.pick_ddj_output).grid(
            row=4, column=2, sticky="w", padx=(10, 0)
        )

        result = tk.Frame(parent, bg=PANEL, padx=18, pady=14)
        result.pack(fill="both", expand=True)
        tk.Label(
            result,
            text="SELECTION / RESULT",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        self.ddj_log = tk.Text(
            result,
            bg="#091522",
            fg=TEXT,
            relief="flat",
            height=10,
            font=("Cascadia Mono", 9),
            padx=11,
            pady=9,
            state="disabled",
        )
        self.ddj_log.pack(fill="both", expand=True, pady=(7, 12))

        actions = tk.Frame(result, bg=PANEL)
        actions.pack(fill="x")
        self.ddj_files_button = self._tool_button(
            actions, "CONVERT FILES", self.convert_ddj_files, GOLD, "#211604"
        )
        self.ddj_files_button.pack(side="left")
        self.ddj_folder_button = self._tool_button(
            actions, "CONVERT FOLDER", self.convert_ddj_folder, CYAN, "#05212b"
        )
        self.ddj_folder_button.pack(side="left", padx=9)
        self._tool_button(actions, "Open output", self.open_ddj_output).pack(
            side="left"
        )
        self.ddj_status = tk.Label(
            actions,
            text="No input selected",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.ddj_status.pack(side="right")
        self.ddj_progress = ttk.Progressbar(
            result, style="Silk.Horizontal.TProgressbar", mode="determinate"
        )
        self.ddj_progress.pack(fill="x", pady=(13, 0))

    def _build_pk2_page(self, parent) -> None:
        controls = tk.Frame(parent, bg=PANEL, padx=22, pady=20)
        controls.pack(fill="x", pady=(16, 12))
        controls.columnconfigure(0, weight=1)

        tk.Label(
            controls,
            text="EXTRACT PK2 ARCHIVES",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 19),
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(
            controls,
            text="Select Data.pk2, Media.pk2, Particles.pk2, or multiple archives at once.",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 15))

        self._tool_button(controls, "Select PK2", self.pick_pk2_files).grid(
            row=2, column=0, sticky="w"
        )
        ttk.Checkbutton(
            controls,
            text="Create one folder per PK2",
            variable=self.pk2_separate_folders,
            style="Silk.TCheckbutton",
        ).grid(row=2, column=1, sticky="e")

        tk.Label(
            controls,
            text="DESTINATION FOLDER",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(18, 5))
        tk.Entry(
            controls,
            textvariable=self.pk2_output,
            bg="#091522",
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 10),
        ).grid(row=4, column=0, sticky="ew", ipady=9)
        self._tool_button(controls, "Choose destination", self.pick_pk2_output).grid(
            row=4, column=1, sticky="e", padx=(10, 0)
        )

        result = tk.Frame(parent, bg=PANEL, padx=18, pady=14)
        result.pack(fill="both", expand=True)
        tk.Label(
            result,
            text="ARCHIVES / PROGRESS",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        self.pk2_log = tk.Text(
            result,
            bg="#091522",
            fg=TEXT,
            relief="flat",
            height=10,
            font=("Cascadia Mono", 9),
            padx=11,
            pady=9,
            state="disabled",
        )
        self.pk2_log.pack(fill="both", expand=True, pady=(7, 12))

        actions = tk.Frame(result, bg=PANEL)
        actions.pack(fill="x")
        self.pk2_extract_button = self._tool_button(
            actions, "EXTRACT ARCHIVES", self.extract_pk2_files, GOLD, "#211604"
        )
        self.pk2_extract_button.pack(side="left")
        self._tool_button(actions, "Open output", self.open_pk2_output).pack(
            side="left", padx=9
        )
        self.pk2_status = tk.Label(
            actions,
            text="No PK2 selected",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.pk2_status.pack(side="right")
        self.pk2_progress = ttk.Progressbar(
            result, style="Silk.Horizontal.TProgressbar", mode="determinate"
        )
        self.pk2_progress.pack(fill="x", pady=(13, 0))

    def _set_pk2_log(self, value: str) -> None:
        self.pk2_log.configure(state="normal")
        self.pk2_log.delete("1.0", "end")
        self.pk2_log.insert("1.0", value)
        self.pk2_log.configure(state="disabled")

    def pick_pk2_files(self) -> None:
        values = filedialog.askopenfilenames(
            title="Select PK2 archives",
            filetypes=(("Silkroad PK2", "*.pk2"), ("All files", "*.*")),
        )
        if values:
            self.pk2_files = [Path(value) for value in values]
            self._set_pk2_log("\n".join(str(path) for path in self.pk2_files))
            self.pk2_status.configure(
                text=f"{len(self.pk2_files)} archive(s) selected", fg=CYAN
            )

    def pick_pk2_output(self) -> None:
        value = filedialog.askdirectory(title="Select destination folder")
        if value:
            self.pk2_output.set(value)

    def extract_pk2_files(self) -> None:
        if not self.pk2_files:
            messagebox.showinfo(APP_NAME, "Select one or more PK2 archives.")
            return
        output_text = self.pk2_output.get().strip()
        if not output_text:
            messagebox.showinfo(APP_NAME, "Choose a destination folder.")
            return

        output = Path(output_text)
        self.pk2_extract_button.configure(state="disabled")
        self.pk2_progress.configure(value=0, maximum=100)
        self.pk2_status.configure(text="Reading PK2 indexes...", fg=GOLD)
        self._set_pk2_log(
            "Preparing extraction. The interface will remain responsive; "
            "large archives may take a few minutes."
        )
        threading.Thread(
            target=self._pk2_worker,
            args=(list(self.pk2_files), output, self.pk2_separate_folders.get()),
            daemon=True,
        ).start()

    def _pk2_worker(
        self, archives: list[Path], output: Path, separate_folders: bool
    ) -> None:
        summaries: list[str] = []
        failures: list[str] = []
        total_archives = len(archives)

        for archive_index, archive_path in enumerate(archives):
            try:
                archive = Pk2Archive(archive_path)
                entries = archive.entries()
                server_markers = archive.server_file_markers(entries)
                if server_markers:
                    sample = ", ".join(server_markers[:4])
                    raise AssetError(
                        "extraction stopped: this client PK2 contains Silkroad server files "
                        f"({sample}). Replace or repair the source archive first"
                    )
                destination = output / archive_path.stem if separate_folders else output

                def progress(done: int, total: int, current: str) -> None:
                    archive_fraction = done / max(total, 1)
                    overall = ((archive_index + archive_fraction) / total_archives) * 100
                    self.after(
                        0,
                        self._pk2_progress_update,
                        overall,
                        archive_path.name,
                        done,
                        total,
                        current,
                    )

                count, total_bytes = archive.extract(destination, entries, progress)
                summaries.append(
                    f"{archive_path.name}: {count:,} files · "
                    f"{total_bytes / (1024 ** 2):,.1f} MB"
                )
            except Exception as exc:
                failures.append(f"{archive_path.name}: {exc}")

        self.after(0, self._pk2_complete, output, summaries, failures)

    def _pk2_progress_update(
        self,
        value: float,
        archive_name: str,
        done: int,
        total: int,
        current: str,
    ) -> None:
        self.pk2_progress.configure(value=value)
        self.pk2_status.configure(
            text=f"{archive_name}: {done:,}/{total:,}", fg=GOLD
        )
        self._set_pk2_log(
            f"Extracting {archive_name}\n\n"
            f"File {done:,} of {total:,}\n{current}"
        )

    def _pk2_complete(
        self, output: Path, summaries: list[str], failures: list[str]
    ) -> None:
        self.pk2_extract_button.configure(state="normal")
        self.last_pk2_output = output if output.exists() else None
        if failures:
            self.pk2_status.configure(
                text=f"{len(summaries)} complete · {len(failures)} error(s)", fg=ERROR
            )
        else:
            self.pk2_progress.configure(value=100)
            self.pk2_status.configure(
                text=f"{len(summaries)} archive(s) extracted", fg=SUCCESS
            )

        sections = []
        if summaries:
            sections.append("COMPLETE\n" + "\n".join(summaries))
        if failures:
            sections.append("ERRORS\n" + "\n".join(failures))
        self._set_pk2_log("\n\n".join(sections))

    def open_pk2_output(self) -> None:
        if self.last_pk2_output and self.last_pk2_output.exists():
            os.startfile(self.last_pk2_output)
        else:
            messagebox.showinfo(APP_NAME, "Extract an archive first.")

    def _set_ddj_log(self, value: str) -> None:
        self.ddj_log.configure(state="normal")
        self.ddj_log.delete("1.0", "end")
        self.ddj_log.insert("1.0", value)
        self.ddj_log.configure(state="disabled")

    def pick_ddj_files(self) -> None:
        values = filedialog.askopenfilenames(
            title="Select textures",
            filetypes=(
                ("Textures", "*.png *.jpg *.jpeg *.bmp *.dds"),
                ("All files", "*.*"),
            ),
        )
        if values:
            self.ddj_files = [Path(value) for value in values]
            self._set_ddj_log("\n".join(str(path) for path in self.ddj_files))
            self.ddj_status.configure(
                text=f"{len(self.ddj_files)} file(s) selected", fg=CYAN
            )

    def pick_ddj_folder(self) -> None:
        value = filedialog.askdirectory(title="Select texture folder")
        if value:
            self.ddj_folder = Path(value)
            self._set_ddj_log(str(self.ddj_folder))
            self.ddj_status.configure(text="Folder selected", fg=CYAN)

    def pick_ddj_output(self) -> None:
        value = filedialog.askdirectory(title="Select destination folder")
        if value:
            self.ddj_output.set(value)

    def convert_ddj_files(self) -> None:
        if not self.ddj_files:
            messagebox.showinfo(APP_NAME, "Select one or more files first.")
            return
        destination = Path(self.ddj_output.get()) if self.ddj_output.get().strip() else None
        jobs = [
            (source, (destination or source.parent) / f"{source.stem}.ddj")
            for source in self.ddj_files
        ]
        self._start_ddj_jobs(jobs)

    def convert_ddj_folder(self) -> None:
        if not self.ddj_folder:
            messagebox.showinfo(APP_NAME, "Select a folder first.")
            return
        iterator = (
            self.ddj_folder.rglob("*")
            if self.ddj_subfolders.get()
            else self.ddj_folder.iterdir()
        )
        sources = sorted(
            path
            for path in iterator
            if path.is_file() and path.suffix.lower() in SUPPORTED_TEXTURES
        )
        if not sources:
            messagebox.showinfo(APP_NAME, "No supported textures were found.")
            return

        destination = Path(self.ddj_output.get()) if self.ddj_output.get().strip() else None
        jobs: list[tuple[Path, Path]] = []
        for source in sources:
            if destination:
                target = destination / source.relative_to(self.ddj_folder).with_suffix(".ddj")
            else:
                target = source.with_suffix(".ddj")
            jobs.append((source, target))
        self._start_ddj_jobs(jobs)

    def _start_ddj_jobs(self, jobs: list[tuple[Path, Path]]) -> None:
        self.ddj_files_button.configure(state="disabled")
        self.ddj_folder_button.configure(state="disabled")
        self.ddj_progress.configure(maximum=len(jobs), value=0)
        self.ddj_status.configure(text=f"Converting 0/{len(jobs)}...", fg=GOLD)

        def worker() -> None:
            outputs: list[Path] = []
            failures: list[str] = []
            for index, (source, target) in enumerate(jobs, 1):
                try:
                    if source.suffix.lower() not in SUPPORTED_TEXTURES:
                        raise AssetError(f"unsupported format: {source.suffix}")
                    content, _ = build_ddj(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    outputs.append(target)
                except Exception as exc:
                    failures.append(f"{source.name}: {exc}")
                self.after(0, self._ddj_progress, index, len(jobs))
            self.after(0, self._ddj_complete, outputs, failures)

        threading.Thread(target=worker, daemon=True).start()

    def _ddj_progress(self, completed: int, total: int) -> None:
        self.ddj_progress.configure(value=completed)
        self.ddj_status.configure(text=f"Converting {completed}/{total}...", fg=GOLD)

    def _ddj_complete(self, outputs: list[Path], failures: list[str]) -> None:
        self.ddj_files_button.configure(state="normal")
        self.ddj_folder_button.configure(state="normal")
        if outputs:
            self.last_ddj_output = outputs[0].parent
        if failures:
            self.ddj_status.configure(
                text=f"{len(outputs)} complete · {len(failures)} error(s)", fg=ERROR
            )
            self._set_ddj_log("ERRORS\n" + "\n".join(failures))
        else:
            self.ddj_status.configure(text=f"{len(outputs)} DDJ file(s) created", fg=SUCCESS)
            self._set_ddj_log("COMPLETE\n" + "\n".join(str(path) for path in outputs))

    def open_ddj_output(self) -> None:
        if self.last_ddj_output and self.last_ddj_output.exists():
            os.startfile(self.last_ddj_output)
        else:
            messagebox.showinfo(APP_NAME, "Convert a texture first.")

    def _field(self, master, label: str, variable: tk.StringVar) -> tk.Frame:
        frame = tk.Frame(master, bg=PANEL, padx=14, pady=12)
        tk.Label(
            frame,
            text=label,
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", pady=(0, 5))
        tk.Entry(
            frame,
            textvariable=variable,
            bg="#091522",
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 10),
        ).pack(fill="x", ipady=7)
        return frame

    def pick_obj(self) -> None:
        value = filedialog.askopenfilename(
            title="Select OBJ model", filetypes=(("Wavefront OBJ", "*.obj"),)
        )
        if value:
            self.obj_picker.value.set(value)
            if not self.name_value.get().strip():
                self.name_value.set(safe_name(Path(value).stem))

    def pick_texture(self) -> None:
        value = filedialog.askopenfilename(
            title="Select texture",
            filetypes=(
                ("Textures", "*.png *.dds *.jpg *.jpeg *.bmp"),
                ("All files", "*.*"),
            ),
        )
        if value:
            self.texture_picker.value.set(value)

    def pick_output(self) -> None:
        value = filedialog.askdirectory(title="Select extracted PK2 root")
        if value:
            self.output_picker.value.set(value)

    def _set_log(self, value: str, color: str = TEXT) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", value)
        self.log.tag_add("color", "1.0", "end")
        self.log.tag_configure("color", foreground=color)
        self.log.configure(state="disabled")

    def generate(self) -> None:
        try:
            scale = float(self.scale_value.get().replace(",", "."))
        except ValueError:
            messagebox.showerror(APP_NAME, "Invalid scale.")
            return
        values = (
            Path(self.obj_picker.value.get()),
            Path(self.texture_picker.value.get()),
            Path(self.output_picker.value.get()),
            self.name_value.get(),
            scale,
            self.axis_value.get(),
            self.mode_value.get(),
        )
        self.generate_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.progress.start(10)
        self._set_log("Building and validating package...", GOLD)
        threading.Thread(target=self._worker, args=values, daemon=True).start()

    def _worker(self, *values) -> None:
        try:
            result = create_package(*values)
        except Exception as exc:
            self.after(0, self._failed, str(exc))
        else:
            self.after(0, self._complete, result)

    def _failed(self, error: str) -> None:
        self.progress.stop()
        self.generate_button.configure(state="normal")
        self._set_log(f"ERROR\n{error}", ERROR)
        messagebox.showerror(APP_NAME, error)

    def _complete(self, result: dict) -> None:
        self.progress.stop()
        self.generate_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.last_output = result["folder"]
        mesh: Mesh = result["mesh"]
        paths = result["paths"]
        if paths["resource"]:
            details = (
                f"CUSTOM PROP / RESOURCE (BSR)\n{paths['resource']}\n\n"
                f"MESH (BMS)\n{paths['mesh']}\n\n"
                f"MATERIAL\n{paths['material']}\n\n"
                f"TEXTURE\n{paths['texture']}"
            )
        else:
            details = (
                f"PROP SKILL / DIRECT EFP MESH\n{paths['mesh']}\n\n"
                f"DIRECT EFP TEXTURE\n{paths['texture']}\n\n"
                "Clone a RenderMesh/Shape node in the EFP and replace these two paths."
            )
        self._set_log(
            f"COMPLETE · {mesh.vertex_count} vertices · {mesh.face_count} faces\n\n{details}",
            SUCCESS,
        )

    def open_result(self) -> None:
        if self.last_output and self.last_output.exists():
            os.startfile(self.last_output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--obj", type=Path)
    parser.add_argument("--texture", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--name", default="")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--axis", choices=("etheral", "blender"), default="etheral")
    parser.add_argument(
        "--mode", choices=("PROP SKILL", "CUSTOM PROP"), default="PROP SKILL"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.obj or args.texture or args.output:
        if not (args.obj and args.texture and args.output):
            raise SystemExit("--obj, --texture, and --output must be used together")
        result = create_package(
            args.obj,
            args.texture,
            args.output,
            args.name or args.obj.stem,
            args.scale,
            args.axis,
            args.mode,
        )
        print(json.dumps({"folder": str(result["folder"]), **result["paths"]}, indent=2))
        return
    App().mainloop()


if __name__ == "__main__":
    main()
