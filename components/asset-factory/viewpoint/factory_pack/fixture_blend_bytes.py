# SPDX-License-Identifier: AGPL-3.0-only
"""Serialize the offline fixture's Blender 4.0.2 address graph reproducibly.

The pinned SDNA describes typed fields of this uncompressed fixture format.
This writer only accepts the Blender-generated scene from the fixture pipeline.
"""
from functools import lru_cache
from hashlib import sha256
import re
import struct

HEADER = b"BLENDER-v400"
BLOCK = struct.Struct("<4siQii")
ADDRESS = struct.Struct("<Q")
DNA_SHA256 = "fdf86810ea0deedd60c7ccc4d2c6ca7feb4962503f6089aca379ae276eb36787"
UNTYPED_BLOCKS = {b"REND", b"DNA1", b"ENDB"}
# Serialized as raw DATA rounded up to four bytes. The live bytes are
# the element counts of the cube, plane and ordered sphere in this fixture.
BOOL_ATTRIBUTE_COUNTS = {
    b".select_vert": {4: 4, 8: 8, 484: 482},
    b".select_edge": {4: 4, 12: 12, 992: 992},
    b".select_poly": {4: 1, 8: 6, 512: 512},
    b"sharp_face": {4: 1, 8: 6, 512: 512},
}
STRING_POINTERS = {
    ("FCurve", "*rna_path"), ("SpreadsheetColumn", "*display_name"),
    ("SpreadsheetColumnID", "*name"), ("ConsoleLine", "*line"),
}

RUNTIME_FIELDS = {
    "Collection": {"runtime.*gobject_hash"},
    "CurveMapping": {"cm[4][0].*table"},
    "CurveProfile": {"*table"},
    "CustomDataLayer": {"*sharing_info"},
    "Editing": {"*displayed_channels", "*seqbasep"},
    "Scene": {"*depsgraph_hash"},
    "ViewLayer": {"*object_bases_hash"},
    "bNode": {"*panel_states_array", "*runtime", "*typeinfo"},
    "bNodeSocket": {"*runtime", "*typeinfo"},
    "bNodeTree": {"*runtime", "*typeinfo", "tree_interface.*runtime",
                   "tree_interface.root_panel.**items_array"},
    "bPose": {"*chanhash"},
    "bScreen": {"*context"},
    "wmWindowManager": {
        "*addonconf", "*autosavetimer", "*defaultconf", "*message_bus",
        "*notifier_queue_set", "*userconf", "keyconfigs.*first", "keyconfigs.*last",
        "notifier_queue.*first", "notifier_queue.*last",
    },
}


def require(condition, detail):
    if not condition:
        raise ValueError(f"fixture_blend_format:{detail}")


def blocks_of(data):
    require(data.startswith(HEADER), "header")
    offset = len(HEADER)
    blocks = []
    while offset < len(data):
        start = offset + BLOCK.size
        require(start <= len(data), "truncated_block_header")
        code, length, address, layout, count = BLOCK.unpack_from(data, offset)
        offset = start + length
        require(length >= 0 and 0 <= layout and 0 <= count and offset <= len(data), "block_bounds")
        blocks.append((start, code, length, address, layout, count))
        if code == b"ENDB":
            break
    require(blocks and blocks[-1][1:] == (b"ENDB", 0, 0, 0, 0)
            and offset == len(data), "end_marker")
    return blocks


def schema(data, blocks):
    dna_blocks = [(start, size) for start, code, size, *_ in blocks if code == b"DNA1"]
    require(len(dna_blocks) == 1, "dna_block_count")
    start, size = dna_blocks[0]
    dna = data[start:start + size]
    require(sha256(dna).hexdigest() == DNA_SHA256, "unqualified_sdna")

    def strings(offset):
        count, = struct.unpack_from("<I", dna, offset)
        offset += 4
        values = []
        for _ in range(count):
            end = dna.index(b"\0", offset)
            values.append(dna[offset:end].decode("ascii"))
            offset = end + 1
        return values, (offset + 3) & ~3

    # The qualified SDNA hash binds the NAME, TYPE, TLEN and STRC tables.
    names, offset = strings(8)
    types, offset = strings(offset + 4)
    sizes = struct.unpack_from(f"<{len(types)}H", dna, offset + 4)
    offset = (offset + 4 + 2 * len(types) + 3) & ~3
    count, = struct.unpack_from("<I", dna, offset + 4)
    offset += 8
    layouts = {}
    for _ in range(count):
        index, field_count = struct.unpack_from("<HH", dna, offset)
        offset += 4
        members = []
        for _ in range(field_count):
            type_index, name_index = struct.unpack_from("<HH", dna, offset)
            offset += 4
            members.append((type_index, names[name_index]))
        layouts[index] = members
    return types, sizes, layouts


def raw_payload(block):
    start, code, size, _, layout, count = block
    require(code == b"DATA" and layout == 0 and count == 1, "unexpected_raw_payload")
    return start, size


def stable_bytes(data):
    blocks = blocks_of(data)
    types, sizes, layouts = schema(data, blocks)
    block_types = tuple(layouts)

    @lru_cache(maxsize=None)
    def fields(index):
        """Yield serialized pointer positions and known fixture random fields."""
        offset = 0
        result = []
        for field_type, name in layouts[index]:
            declarator = name.split("[", 1)[0]
            indirect = declarator.count("*")
            axes = re.findall(r"\[(\d+)\]", name)
            field_count = 1
            for axis in axes:
                field_count *= int(axis)
            width = ADDRESS.size if indirect else sizes[field_type]
            if types[index] == "FileSelectParams" and name == "dir[1090]":
                result.append((offset, name, 0))
            for item in range(field_count):
                position = offset + item * width
                label = f"{name}[{item}]" if axes else name
                if indirect or (types[index], name) in {
                    ("bNode", "identifier"), ("Mesh", "face_sets_color_seed")
                }:
                    result.append((position, label, indirect))
                elif field_type in layouts:
                    result.extend((position + pos, f"{label}.{subfield}", level)
                                  for pos, subfield, level in fields(field_type))
            offset += field_count * width
        require(offset == sizes[index], f"layout_size:{types[index]}")
        return result

    addresses = {}
    for number, block in enumerate(blocks, 1):
        _, code, _, addr, _, _ = block
        if addr:
            previous = addresses.get(addr)
            if previous:
                require((previous[1], code) == (b"REND", b"GLOB"), "duplicate_old_address")
            # REND uses the GLOB address but has no blend data of its own.
            addresses[addr] = (number * ADDRESS.size, code, block)

    result = bytearray(data)
    pointer_arrays = set()
    for number, block in enumerate(blocks, 1):
        start, code, size, addr, layout, count = block
        mapped = addresses[addr][0] if addr else 0
        ADDRESS.pack_into(result, start - BLOCK.size + 8, mapped)
        if code in UNTYPED_BLOCKS or layout == 0:
            require(code in UNTYPED_BLOCKS or code == b"DATA", "unknown_untyped_block")
            continue
        require(layout < len(block_types), "unknown_layout")
        index = block_types[layout]
        name = types[index]
        stride = sizes[index]
        require(count * stride == size, f"payload_size:{name}")
        for item in range(count):
            for offset, field, indirect in fields(index):
                struct_start = start + item * stride
                pos = struct_start + offset
                if not indirect:
                    # Only the fixture's built-in shader nodes are present. Their RNA
                    # defaults and links use the remapped block addresses.
                    if field == "identifier":
                        require(name == "bNode" and count == 1, "shader_node_identifier")
                        struct.pack_into("<I", result, pos, number)
                    elif name == "FileSelectParams" and field == "dir[1090]":
                        # File-browser UI state can point at the producer's private HOME.
                        result[pos:pos + 1090] = b"//" + bytes(1088)
                    else:
                        # The fixture has no sculpt face sets.
                        require(name == "Mesh" and field == "face_sets_color_seed", "mesh_seed")
                        struct.pack_into("<I", result, pos, 0)
                    continue
                address, = ADDRESS.unpack_from(data, pos)
                target = addresses.get(address)
                require(not address or target or field in RUNTIME_FIELDS.get(name, ()),
                        f"missing_pointer_target:{name}:{field}")
                if target and indirect == 2:
                    require(name in {"Mesh", "Object"} and field == "**mat",
                            f"unknown_pointer_array:{name}:{field}")
                    pointer_arrays.add(target[2])
                elif target:
                    require(indirect == 1, f"unsupported_pointer_depth:{name}:{field}")
                ADDRESS.pack_into(result, pos, target[0] if target else 0)
                if not target:
                    continue
                if (name, field) in STRING_POINTERS:
                    payload, length = raw_payload(target[2])
                    used = data.index(b"\0", payload, payload + length) - payload + 1
                    result[payload + used:payload + length] = bytes(length - used)
                if (name, field) == ("CustomDataLayer", "*data"):
                    # CD_PROP_BOOL and name offsets come from the pinned SDNA.
                    kind, = struct.unpack_from("<I", data, struct_start)
                    if kind == 50:
                        name_start = struct_start + 32
                        attribute = data[name_start:name_start + 68].split(b"\0", 1)[0]
                        payload, length = raw_payload(target[2])
                        used = BOOL_ATTRIBUTE_COUNTS.get(attribute, {}).get(length)
                        require(used is not None and set(data[payload:payload + used]) <= {0, 1},
                                "unexpected_fixture_boolean_layer")
                        result[payload + used:payload + length] = bytes(length - used)

    for start, code, size, _, layout, count in pointer_arrays:
        require(code == b"DATA" and layout == 0 and count == 1
                and size % ADDRESS.size == 0, "invalid_pointer_array")
        for pos in range(start, start + size, ADDRESS.size):
            address, = ADDRESS.unpack_from(data, pos)
            target = addresses.get(address)
            require(not address or (target and target[1] == b"MA\0\0"), "unknown_material_pointer")
            ADDRESS.pack_into(result, pos, target[0] if target else 0)
    return bytes(result)


def write_fixture_blend(source, destination):
    # Finish serialization before publishing a new file. Consumers, including the
    # independent Blender readback and the registry, open these exact bytes.
    canonical = stable_bytes(source.read_bytes())
    with destination.open("xb") as stream:
        stream.write(canonical)
