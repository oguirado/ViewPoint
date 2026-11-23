"""Raw TRELLIS generation boundary; an OK result is never appearance approval.

The injected Service is an in-process protocol, not a claim about an existing
HTTP wire contract. Its owner implements multipart POST /generate only after
F1-U1 defines effective-parameter and inference-quiescence evidence. No live
transport is guessed here. Scheduler admission and model release belong to U3;
this module neither takes nor releases its lease.

output_dir names a NEW generation directory. raw.glb, request.json and
result.json are published together by one directory rename. result.json is an
af.generate/v1 result, not an af.manifest/v1 cache entry or a visual QA receipt.
"""
from __future__ import annotations

import asyncio
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import threading
import time
from typing import AsyncIterator, Protocol

from asset_factory import core

DEFAULT_TIMEOUT_S = 1800  # [TARGET: STRATEGY_ASSET_FACTORY §3 F1-U2]
MAX_IMAGE_BYTES = 32 * 2**20  # [TARGET: bounded adapter input, DESIGN_CHOICE]
MAX_GLB_BYTES = 256 * 2**20  # [TARGET: bounded adapter output, DESIGN_CHOICE]
CLEANUP_TIMEOUT_S = 1  # [TARGET: independent inference-cancel budget]
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_PIN = re.compile(r'[0-9a-f]{40}\Z')


class Refusal(Exception):
    def __init__(self, rc: int, code: str):
        self.rc, self.code = rc, code
        super().__init__(code)


@dataclass
class Response:
    """Service claims, verified against caller bindings before publication."""
    request: dict
    provider_sha256: str
    output_sha256: str
    chunks: AsyncIterator[bytes]
    geometry_only: bool
    raw_preserved: bool
    peak_rss_bytes: int | None = None
    rc: int = 0
    peak_measurement_ref: dict | None = None


class Service(Protocol):
    """Cooperative transport: cancellation/finalizers must finish within cleanup budget.

    Task cancellation is translated to an INCOMPLETE result with uncancel(); a
    second cancellation propagates. Response streams implement bounded aclose.
    Capability fields are service claims, never proof of a clean runtime build.
    """
    async def generate(self, image: bytes, request: dict, *, deadline_ns: int) -> Response: ...

    async def cancel(self, request: dict, *, deadline_ns: int) -> dict:
        """Return request_id, request_sha256 and quiescent (inference only).

        No HTTP cancellation endpoint is assumed. A false or absent ack never
        proves inference stopped, and no ack proves scheduler model release.
        """
        ...


def _require(condition, rc, code):
    if not condition:
        raise Refusal(rc, code)


def _digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _integer(value, *, minimum=0):
    return type(value) is int and value >= minimum


def _validate_call(image, request, provider, decision, factory, policy, seed, pipeline_type, timeout_s):
    _require(type(image) is bytes and 0 < len(image) <= MAX_IMAGE_BYTES, 2, 'image_invalid')
    _require(_integer(seed) and isinstance(pipeline_type, str) and bool(pipeline_type), 2, 'params_invalid')
    _require(_integer(timeout_s, minimum=1) and timeout_s <= DEFAULT_TIMEOUT_S, 2, 'timeout_invalid')
    for obj in (request, provider, decision, factory, policy):
        _require(isinstance(obj, dict), 2, 'schema_mismatch')
        core.canonical_json(obj)
    core.validate(request, {'required': ['schema', 'kind', 'slot', 'request_id', 'provider_id',
                   'provider_revision', 'inputs', 'params', 'context'],
                   'properties': {'schema': {'const': 'af.generate/v1'}}})
    for payload, schema_id, fields in (
        (provider, 'af.provider/v1', ('schema', 'id', 'slot', 'kind', 'runnable', 'revision',
          'licence', 'territory_excluded', 'commercial', 'gated', 'weights', 'runtime', 'known_issues')),
        (decision, 'af.licence-decision/v1', ('schema', 'rc', 'verdict', 'allowed', 'provider_id',
          'operation', 'jurisdiction', 'commercial_use', 'monetisable', 'reason_codes',
          'evaluated_dependencies', 'configuration_sha256', 'provider_sha256', 'policy_sha256')),
        (factory, 'af.factory-config/v1', ('schema', 'jurisdiction', 'commercial_use', 'models_path',
          'memory', 'slots', 'gates')),
        (policy, 'af.territory-groups/v1', ('schema', 'groups', 'countries')),
    ):
        core.validate(payload, {'required': list(fields),
                               'properties': {'schema': {'const': schema_id}}})
    _require(set(request) == {'schema', 'kind', 'slot', 'request_id', 'provider_id',
             'provider_revision', 'inputs', 'params', 'context'}, 2, 'request_invalid')
    _require(request['kind'] == 'request' and request['slot'] == 'generate'
             and isinstance(request['request_id'], str) and bool(request['request_id']), 2, 'request_invalid')
    _require(provider.get('schema') == 'af.provider/v1' and provider.get('id') == 'trellis2'
             and provider.get('slot') == 'generate' and provider.get('kind') == 'model', 2, 'provider_invalid')
    _require(type(provider.get('runnable')) is bool, 2, 'provider_invalid')
    _require(provider['runnable'], 5, 'provider_blocked')
    _require(isinstance(provider.get('revision'), str) and _PIN.fullmatch(provider['revision']), 2, 'unpinned_revision')
    _require(isinstance(provider['licence'], dict)
             and all(key in provider['licence'] for key in ('code', 'weights', 'deps'))
             and isinstance(provider['licence']['code'], str)
             and isinstance(provider['licence']['weights'], str)
             and isinstance(provider['licence']['deps'], list)
             and isinstance(provider['territory_excluded'], list)
             and type(provider['commercial']) is bool and type(provider['gated']) is bool
             and isinstance(provider['runtime'], dict) and isinstance(provider['known_issues'], list),
             2, 'provider_invalid')
    runtime = provider['runtime']
    peak = runtime.get('ram_peak_gb_measured')
    measurement = runtime.get('ram_peak_measurement_ref')
    _require((peak is None and measurement is None)
             or (_integer(peak, minimum=1) and _measurement_ref(measurement)),
             2, 'peak_measurement_invalid')
    weights = provider.get('weights')
    _require(isinstance(weights, list) and bool(weights), 3, 'weights_inventory_missing')
    for weight in weights:
        _require(isinstance(weight, dict) and isinstance(weight.get('repo'), str)
                 and isinstance(weight.get('revision'), str) and _PIN.fullmatch(weight['revision']), 2, 'weight_pin_invalid')
        _require(isinstance(weight.get('files'), list) and bool(weight['files']), 3, 'weights_inventory_missing')
        for item in weight['files']:
            _require(isinstance(item, dict) and isinstance(item.get('path'), str)
                     and bool(item['path']) and _digest(item.get('sha256')), 2, 'weight_inventory_invalid')
    _require(request['provider_id'] == provider['id']
             and request['provider_revision'] == provider['revision'], 4, 'provider_stale')
    params = request['params']
    _require(isinstance(params, dict) and params.get('seed') == seed
             and type(params.get('seed')) is int and params.get('pipeline_type') == pipeline_type
             and all(type(v) in (str, int, bool) for v in params.values()), 2, 'params_invalid')
    inputs = request['inputs']
    _require(isinstance(inputs, list) and len(inputs) == 1 and isinstance(inputs[0], dict), 2, 'image_invalid')
    item = inputs[0]
    _require(set(item) == {'role', 'path', 'media_type', 'sha256'} and item['role'] == 'image'
             and isinstance(item['path'], str) and bool(item['path'])
             and item['media_type'] in ('image/png', 'image/jpeg') and _digest(item['sha256']), 2, 'image_invalid')
    _require(hashlib.sha256(image).hexdigest() == item['sha256'], 4, 'input_stale')
    context = request['context']
    _require(isinstance(context, dict) and set(context) == {'jurisdiction', 'commercial_use',
             'policy_sha256', 'toolchain_sha256', 'timeout_s'}, 2, 'context_invalid')
    _require(isinstance(context['jurisdiction'], str) and type(context['commercial_use']) is bool
             and _digest(context['policy_sha256']) and _digest(context['toolchain_sha256'])
             and type(context['timeout_s']) is int and context['timeout_s'] == timeout_s, 2, 'context_invalid')
    _require(factory.get('schema') == 'af.factory-config/v1'
             and policy.get('schema') == 'af.territory-groups/v1', 2, 'policy_invalid')
    _require(context['jurisdiction'] == factory.get('jurisdiction')
             and type(factory.get('commercial_use')) is bool
             and context['commercial_use'] == factory['commercial_use'], 4, 'configuration_stale')
    _require(context['jurisdiction'] in policy.get('countries', []), 2, 'unknown_jurisdiction')
    _require(decision.get('schema') == 'af.licence-decision/v1'
             and type(decision.get('rc')) is int and decision['rc'] in core.RC
             and decision.get('verdict') == core.VERDICT[decision['rc']]
             and type(decision.get('allowed')) is bool, 2, 'decision_invalid')
    _require(decision.get('provider_id') == provider['id'] and decision.get('operation') == 'generate'
             and decision.get('jurisdiction') == context['jurisdiction']
             and type(decision.get('commercial_use')) is bool
             and decision['commercial_use'] == context['commercial_use'], 4, 'decision_stale')
    _require(decision.get('provider_sha256') == core.sha256_json(provider)
             and decision.get('configuration_sha256') == core.sha256_json(factory)
             and decision.get('policy_sha256') == core.sha256_json(policy)
             and context['policy_sha256'] == core.sha256_json(policy), 4, 'decision_stale')
    _require(decision['allowed'] and decision['rc'] == 0, 5, 'licence_refused')
    if peak is not None:
        # The delivered observation covers only 512/low_vram with the native
        # pipeline defaults. A provider-wide number cannot qualify other modes
        # or extra workload controls. Scheduler admission must call this guard
        # before copying a measured peak (integration request in repair report).
        _require(pipeline_type == '512' and set(params) == {'seed', 'pipeline_type'},
                 3, 'calibration_unavailable')


def _strict_json(data):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise Refusal(2, 'glb_duplicate_key')
            obj[key] = value
        return obj
    def constant(_):
        raise Refusal(2, 'glb_nonfinite')
    try:
        # glTF geometry legitimately uses floats; stage JSON remains integer-only.
        obj = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
        def finite(value):
            if isinstance(value, float):
                _require(math.isfinite(value), 2, 'glb_nonfinite')
            elif isinstance(value, dict):
                for child in value.values():
                    finite(child)
            elif isinstance(value, list):
                for child in value:
                    finite(child)
        finite(obj)
        return obj
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refusal(2, 'glb_json_invalid') from exc


def validate_glb(data: bytes, *, deadline_ns: int | None = None,
                 stop_event: threading.Event | None = None) -> None:
    """Validate GLB framing and embedded triangle buffer/accessor bounds.

    Deliberately bounded geometry-only subset: TRIANGLES, embedded BIN, dense
    uncompressed position/index accessors. Other layouts fail closed for the
    adapter, rather than being labelled validated. Appearance is not assessed.
    """
    _require(type(data) is bytes and 20 <= len(data) <= MAX_GLB_BYTES, 2, 'glb_invalid')
    magic, version, length = struct.unpack_from('<4sII', data)
    _require(magic == b'glTF' and version == 2 and length == len(data), 2, 'glb_header_invalid')
    def budget():
        if stop_event is not None:
            _require(not stop_event.is_set(), 3, 'cancelled')
        if deadline_ns is not None:
            _check_deadline(deadline_ns)
    budget()
    chunks, offset = [], 12
    while offset < len(data):
        _require(offset + 8 <= len(data), 2, 'glb_chunk_invalid')
        size, kind = struct.unpack_from('<I4s', data, offset)
        offset += 8
        _require(size > 0 and size % 4 == 0 and size <= len(data) - offset, 2, 'glb_chunk_invalid')
        chunks.append((kind, data[offset:offset + size]))
        offset += size
    _require(len(chunks) == 2 and chunks[0][0] == b'JSON' and chunks[1][0] == b'BIN\x00', 2, 'glb_chunks_unsupported')
    doc = _strict_json(chunks[0][1])
    _require(isinstance(doc, dict) and isinstance(doc.get('asset'), dict)
             and doc['asset'].get('version') == '2.0', 2, 'glb_asset_invalid')
    _require(not doc.get('extensionsRequired'), 2, 'glb_extensions_unsupported')
    binary = chunks[1][1]
    buffers, views, accessors = doc.get('buffers'), doc.get('bufferViews'), doc.get('accessors')
    _require(isinstance(buffers, list) and len(buffers) == 1 and isinstance(buffers[0], dict)
             and 'uri' not in buffers[0] and _integer(buffers[0].get('byteLength'), minimum=1)
             and 0 <= len(binary) - buffers[0]['byteLength'] <= 3, 2, 'glb_buffer_invalid')
    _require(isinstance(views, list) and isinstance(accessors, list), 2, 'glb_accessor_invalid')
    sizes = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
    widths = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT2': 4, 'MAT3': 9, 'MAT4': 16}
    for view in views:
        _require(isinstance(view, dict) and type(view.get('buffer')) is int and view['buffer'] == 0
                 and _integer(view.get('byteOffset', 0)) and _integer(view.get('byteLength'), minimum=1)
                 and view.get('byteOffset', 0) + view['byteLength'] <= buffers[0]['byteLength'], 2, 'glb_view_invalid')
    for accessor in accessors:
        budget()
        _require(isinstance(accessor, dict) and 'sparse' not in accessor
                 and _integer(accessor.get('bufferView')) and accessor['bufferView'] < len(views)
                 and _integer(accessor.get('count'), minimum=1)
                 and type(accessor.get('componentType')) is int and accessor['componentType'] in sizes
                 and accessor.get('type') in widths and _integer(accessor.get('byteOffset', 0)), 2, 'glb_accessor_invalid')
        view = views[accessor['bufferView']]
        element = sizes[accessor['componentType']] * widths[accessor['type']]
        stride = view.get('byteStride', element)
        _require(_integer(stride, minimum=element) and stride <= 252
                 and stride % sizes[accessor['componentType']] == 0
                 and accessor.get('byteOffset', 0) % sizes[accessor['componentType']] == 0
                 and accessor.get('byteOffset', 0) + (accessor['count'] - 1) * stride + element <= view['byteLength'],
                 2, 'glb_accessor_bounds')
    meshes = doc.get('meshes')
    _require(isinstance(meshes, list) and bool(meshes), 2, 'glb_mesh_missing')
    _validate_scene_graph(doc, meshes, budget)
    for mesh in meshes:
        _require(isinstance(mesh, dict) and isinstance(mesh.get('primitives'), list)
                 and bool(mesh['primitives']), 2, 'glb_mesh_invalid')
        for prim in mesh['primitives']:
            _require(isinstance(prim, dict) and type(prim.get('mode', 4)) is int and prim.get('mode', 4) == 4
                     and isinstance(prim.get('attributes'), dict), 2, 'glb_primitive_invalid')
            _require(not prim.get('extensions') and not prim.get('targets'), 2, 'glb_layout_unsupported')
            for name, attribute in prim['attributes'].items():
                if name == 'POSITION':
                    continue  # Preserve the specific position refusal below.
                _require(_integer(attribute) and attribute < len(accessors), 2, 'glb_attribute_invalid')
            if 'material' in prim:
                _require(_integer(prim['material']) and isinstance(doc.get('materials'), list)
                         and prim['material'] < len(doc['materials']), 2, 'glb_material_invalid')
            pos = prim['attributes'].get('POSITION')
            _require(_integer(pos) and pos < len(accessors) and accessors[pos]['type'] == 'VEC3'
                     and accessors[pos]['componentType'] == 5126, 2, 'glb_positions_invalid')
            position = accessors[pos]
            position_view = views[position['bufferView']]
            position_start = position_view.get('byteOffset', 0) + position.get('byteOffset', 0)
            position_stride = position_view.get('byteStride', 12)
            for vertex in range(position['count']):
                if vertex % 4096 == 0:
                    budget()
                xyz = struct.unpack_from('<3f', binary, position_start + vertex * position_stride)
                _require(all(math.isfinite(component) for component in xyz), 2, 'glb_nonfinite_position')
            count = position['count']
            if 'indices' in prim:
                idx = prim['indices']
                _require(_integer(idx) and idx < len(accessors) and accessors[idx]['type'] == 'SCALAR'
                         and accessors[idx]['componentType'] in (5121, 5123, 5125), 2, 'glb_indices_invalid')
                entry, count = accessors[idx], accessors[idx]['count']
                view = views[entry['bufferView']]
                fmt, size = {5121: ('B', 1), 5123: ('H', 2), 5125: ('I', 4)}[entry['componentType']]
                start = view.get('byteOffset', 0) + entry.get('byteOffset', 0)
                stride = view.get('byteStride', size)
                for n in range(count):
                    if n % 4096 == 0:
                        budget()
                    value = struct.unpack_from('<' + fmt, binary, start + n * stride)[0]
                    _require(value < accessors[pos]['count'], 2, 'glb_index_bounds')
            _require(count >= 3 and count % 3 == 0, 2, 'glb_triangles_invalid')


def _validate_scene_graph(doc, meshes, budget):
    """Validate the static geometry tree, including unused scenes and nodes."""
    _require(not any(doc.get(key) for key in ('skins', 'animations', 'cameras',
                 'extensionsUsed', 'materials', 'images', 'textures', 'samplers')), 2, 'glb_layout_unsupported')
    nodes, scenes = doc.get('nodes'), doc.get('scenes')
    _require(isinstance(nodes, list) and bool(nodes) and isinstance(scenes, list)
             and bool(scenes) and _integer(doc.get('scene')) and doc['scene'] < len(scenes),
             2, 'glb_scene_invalid')
    parents = [0] * len(nodes)
    children = []
    for node in nodes:
        budget()
        _require(isinstance(node, dict) and not any(key in node for key in
                 ('skin', 'camera', 'weights', 'extensions')), 2, 'glb_node_invalid')
        if 'mesh' in node:
            _require(_integer(node['mesh']) and node['mesh'] < len(meshes), 2, 'glb_node_invalid')
        _require(not ('matrix' in node and any(key in node for key in ('translation', 'rotation', 'scale'))),
                 2, 'glb_node_invalid')
        for key, count in (('matrix', 16), ('translation', 3), ('rotation', 4), ('scale', 3)):
            if key in node:
                value = node[key]
                _require(isinstance(value, list) and len(value) == count
                         and all(type(v) in (int, float) and math.isfinite(v) for v in value),
                         2, 'glb_node_invalid')
        refs = node.get('children', [])
        _require(isinstance(refs, list) and all(_integer(i) and i < len(nodes) for i in refs)
                 and len(set(refs)) == len(refs), 2, 'glb_node_invalid')
        for child in refs:
            parents[child] += 1
            _require(parents[child] <= 1, 2, 'glb_node_invalid')
        children.append(refs)
    for scene in scenes:
        _require(isinstance(scene, dict) and not scene.get('extensions'), 2, 'glb_scene_invalid')
        refs = scene.get('nodes')
        _require(isinstance(refs, list) and bool(refs)
                 and all(_integer(i) and i < len(nodes) and parents[i] == 0 for i in refs)
                 and len(set(refs)) == len(refs), 2, 'glb_scene_invalid')
    pending = [index for index, degree in enumerate(parents) if degree == 0]
    visited = 0
    for index in pending:
        budget(); visited += 1
        pending.extend(children[index])
    _require(visited == len(nodes), 2, 'glb_node_cycle')
    active = list(scenes[doc['scene']]['nodes']); found_mesh = False
    for index in active:
        budget(); found_mesh |= 'mesh' in nodes[index]
        active.extend(children[index])
    _require(found_mesh, 2, 'glb_scene_empty')


def _validate_response(response, request, provider):
    _require(isinstance(response, Response) and type(response.rc) is int
             and response.rc in core.RC, 2, 'service_protocol')
    if response.rc:
        raise Refusal(response.rc, 'service_refused')
    _require(core.sha256_json(response.request) == core.sha256_json(request), 4, 'effective_request_stale')
    _require(response.provider_sha256 == core.sha256_json(provider), 4, 'service_provider_stale')
    _require(_digest(response.output_sha256), 2, 'output_hash_invalid')
    _require(response.geometry_only is True and response.raw_preserved is True, 5, 'capability_refused')
    _require(response.peak_rss_bytes is None or _integer(response.peak_rss_bytes, minimum=1), 2, 'telemetry_invalid')
    _require(response.peak_measurement_ref is None or
             (response.peak_rss_bytes is not None and _measurement_ref(response.peak_measurement_ref)),
             2, 'telemetry_invalid')


def _measurement_ref(value):
    return (isinstance(value, dict) and set(value) == {'path', 'sha256'}
            and isinstance(value['path'], str) and bool(value['path']) and _digest(value['sha256']))


def _check_deadline(deadline_ns):
    _require(time.monotonic_ns() < deadline_ns, 6, 'deadline_exceeded')


def _check_cancel(event):
    _require(not event.is_set(), 3, 'cancelled')


def _consume_exception(task):
    if not task.cancelled():
        task.exception()


async def _wait(awaitable, deadline_ns, cancel_event):
    task = asyncio.ensure_future(awaitable)
    cancelled = asyncio.create_task(cancel_event.wait())
    try:
        remaining = max(0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        done, _ = await asyncio.wait((task, cancelled), timeout=remaining,
                                     return_when=asyncio.FIRST_COMPLETED)
        _check_cancel(cancel_event)
        _check_deadline(deadline_ns)
        if task not in done:
            raise Refusal(6, 'deadline_exceeded')
        return task.result()
    finally:
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        if not task.done():
            task.cancel()
            done, _ = await asyncio.wait((task,), timeout=CLEANUP_TIMEOUT_S)
            if not done:
                task.add_done_callback(_consume_exception)
                raise Refusal(3, 'transport_cleanup_unproven')
            if not task.cancelled():
                task.exception()
        elif not task.cancelled():
            task.exception()


async def _cancel_inference(service, request):
    # This is solely inference quiescence; never scheduler or resident release.
    deadline_ns = time.monotonic_ns() + CLEANUP_TIMEOUT_S * 1_000_000_000
    try:
        ack = await _wait(service.cancel(request, deadline_ns=deadline_ns), deadline_ns, asyncio.Event())
        return (isinstance(ack, dict) and ack.get('quiescent') is True
                and ack.get('request_id') == request['request_id']
                and ack.get('request_sha256') == core.sha256_json(request))
    except Exception:
        return False


async def _validate_geometry(data, deadline_ns, event):
    stop = threading.Event()
    # Shield the worker so cancellation does not mark the underlying operation
    # complete before its cooperative stop/deadline checks have run.
    worker = asyncio.create_task(asyncio.to_thread(validate_glb, data,
                                 deadline_ns=deadline_ns, stop_event=stop))
    try:
        return await _wait(asyncio.shield(worker), deadline_ns, event)
    finally:
        stop.set()
        if not worker.done():
            done, _ = await asyncio.wait((worker,), timeout=CLEANUP_TIMEOUT_S)
            if not done:
                worker.add_done_callback(_consume_exception)
                raise Refusal(3, 'geometry_cleanup_unproven')
        if worker.done() and not worker.cancelled():
            worker.exception()


async def _close_stream(response):
    if not isinstance(response, Response):
        return
    close = getattr(response.chunks, 'aclose', None)
    _require(callable(close), 2, 'stream_close_unavailable')
    deadline_ns = time.monotonic_ns() + CLEANUP_TIMEOUT_S * 1_000_000_000
    try:
        await _wait(close(), deadline_ns, asyncio.Event())
    except Exception as exc:
        raise Refusal(3, 'stream_cleanup_unproven') from exc


def _sync_file(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _destination(output_dir):
    target = Path(output_dir).absolute()
    _require(target.name not in ('', '.', '..'), 2, 'output_path_invalid')
    parent = target.parent.resolve(strict=True)
    _require(target.parent == parent and not target.is_symlink(), 2, 'path_escape')
    _require(not target.exists(), 5, 'output_exists')
    return target


def _lock_destination(target):
    lock = target.with_name('.' + target.name + '.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        # rc 2 preserves the existing unit oracle; a stable code distinguishes
        # a foreign publication lock from an implementation exception.
        raise Refusal(2, 'publication_locked') from exc
    os.close(fd)
    return lock


def _unlock_destination(lock):
    if lock is not None:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            # A held lock is safe; recovery is explicitly not automatic.
            pass


def _commit_generation(stage, target):
    """Linux atomic no-replace directory commit; unsupported hosts refuse.

    JSON files still use core.atomic_write_json/os.replace during staging.
    os.replace on the public directory would overwrite a racing empty caller
    directory. renameat2(RENAME_NOREPLACE) closes that gap without a fallback.
    """
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise Refusal(3, 'publication_noreplace_unavailable') from exc
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Linux AT_FDCWD=-100, RENAME_NOREPLACE=1; no architecture syscall number.
        rc = rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1)
        if rc:
            code = ctypes.get_errno()
            if code in (errno.EEXIST, errno.ENOTEMPTY):
                raise Refusal(5, 'output_exists')
            if code in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
                raise Refusal(3, 'publication_noreplace_unavailable')
            raise OSError(code, os.strerror(code))
        try:
            os.fsync(parent_fd)
        except OSError:
            # Commit is already visible. Never return a byte-preserving refusal
            # after publication; crash durability is not an adapter verdict.
            pass
    finally:
        os.close(parent_fd)


def _publish(target, data, request, result, deadline_ns, cancel_event, evidence):
    stage = Path(tempfile.mkdtemp(prefix='.' + target.name + '.', dir=target.parent))
    try:
        _require(not target.exists(), 5, 'output_exists')
        _sync_file(stage / 'raw.glb', data)
        core.atomic_write_json(stage / 'request.json', request)
        core.atomic_write_json(stage / 'service-evidence.json', evidence)
        core.atomic_write_json(stage / 'result.json', result)
        _require(core.sha256_file(stage / 'raw.glb') == result['outputs'][0]['sha256']
                 and core.sha256_file(stage / 'request.json') == result['receipts'][0]['sha256']
                  and core.sha256_file(stage / 'service-evidence.json') == result['receipts'][1]['sha256']
                 and core.sha256_file(stage / 'result.json') == core.sha256_json(result), 4, 'staged_bytes_stale')
        directory_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _check_cancel(cancel_event)
        _check_deadline(deadline_ns)
        _require(not target.exists(), 5, 'output_exists')
        _commit_generation(stage, target)
    finally:
        # Cleanup after a successful commit cannot turn it into a refusal.
        shutil.rmtree(stage, ignore_errors=True)


def _service_evidence(response, request):
    # Local F1 development evidence, not a visual verdict or runtime attestation.
    # No new schema family is claimed: this data is bound by its content hash.
    return {'request': response.request, 'request_sha256': core.sha256_json(request),
            'provider_sha256': response.provider_sha256, 'output_sha256': response.output_sha256,
            'geometry_only': response.geometry_only, 'raw_preserved': response.raw_preserved,
            'service_rc': response.rc, 'service_peak_rss_bytes': response.peak_rss_bytes,
            'peak_measurement_ref': response.peak_measurement_ref}


def _result(request, rc, code, *, wall_ms=0, peak=None):
    result = {'schema': 'af.generate/v1', 'kind': 'result', 'slot': 'generate',
              'request_id': request['request_id'], 'provider_id': request['provider_id'],
              'provider_revision': request['provider_revision'], 'rc': rc, 'status': core.STATUS[rc],
              'outputs': [], 'receipts': [], 'metrics': {'wall_ms': wall_ms, 'peak_rss_bytes': peak},
              'absence_reasons': {}}
    if peak is None:
        result['absence_reasons']['peak_rss_bytes'] = 'service_peak_unmeasured'
    if rc:
        result['absence_reasons']['outputs'] = code
    return result


async def generate(image: bytes, *, seed: int, pipeline_type: str,
                   output_dir: Path, request: dict, provider: dict, decision: dict,
                   factory: dict, policy: dict, service: Service | None = None,
                   timeout_s: int = DEFAULT_TIMEOUT_S,
                   cancel_event: asyncio.Event | None = None) -> dict:
    """Generate checked raw bytes through an explicitly owned service.

    F0 licence decision and current parsed config/provider/policy are required;
    scheduler admission and input-gate proof remain caller preconditions. A
    missing service refuses rc 3. No model, download or container is launched.
    """
    started_ns = time.monotonic_ns()
    event = cancel_event if cancel_event is not None else asyncio.Event()
    attempted = False
    response = None
    lock = None
    try:
        _validate_call(image, request, provider, decision, factory, policy, seed, pipeline_type, timeout_s)
        _require(isinstance(event, asyncio.Event), 2, 'cancel_event_invalid')
        deadline_ns = started_ns + timeout_s * 1_000_000_000
        _check_cancel(event)
        target = _destination(output_dir)
        _require(service is not None, 3, 'service_transport_absent')
        lock = _lock_destination(target)
        attempted = True
        response = await _wait(service.generate(image, request, deadline_ns=deadline_ns), deadline_ns, event)
        _validate_response(response, request, provider)
        body = bytearray()
        iterator = response.chunks.__aiter__()
        while True:
            try:
                chunk = await _wait(iterator.__anext__(), deadline_ns, event)
            except StopAsyncIteration:
                break
            _require(type(chunk) is bytes and bool(chunk), 2, 'stream_invalid')
            _require(len(body) + len(chunk) <= MAX_GLB_BYTES, 6, 'output_budget_exceeded')
            body.extend(chunk)
        data = bytes(body)
        await _validate_geometry(data, deadline_ns, event)
        await _close_stream(response)
        evidence = _service_evidence(response, request)
        response_closed = response
        response = None
        _require(hashlib.sha256(data).hexdigest() == response_closed.output_sha256, 4, 'output_stale')
        _check_cancel(event)
        _check_deadline(deadline_ns)
        wall_ms = (time.monotonic_ns() - started_ns) // 1_000_000
        # A transport's reference dictionary is a claim, not authenticated
        # request/provider-bound evidence. Retain it in service-evidence.json.
        result = _result(request, 0, '', wall_ms=wall_ms, peak=None)
        if response_closed.peak_rss_bytes is not None:
            result['absence_reasons']['peak_rss_bytes'] = 'service_self_reported'
        result['outputs'] = [{'role': 'mesh_glb', 'path': 'raw.glb',
                              'media_type': 'model/gltf-binary', 'sha256': hashlib.sha256(data).hexdigest()}]
        result['receipts'] = [{'schema': 'af.generate/v1', 'path': 'request.json', 'sha256': core.sha256_json(request)}]
        # A generic JSON media type is deliberately not presented as a newly
        # approved af.* schema. Integration must assign the evidence family.
        result['receipts'].append({'schema': 'application/json', 'path': 'service-evidence.json',
                                   'sha256': core.sha256_json(evidence)})
        _publish(target, data, request, result, deadline_ns, event, evidence)
        _unlock_destination(lock)
        return result
    except asyncio.CancelledError:
        # Cancellation is a structured incomplete stage result, never success.
        asyncio.current_task().uncancel()
        failure = Refusal(3, 'cancelled')
    except Refusal as exc:
        failure = exc
    except core.ContractError as exc:
        failure = Refusal(2, 'publication_io' if isinstance(exc.__cause__, OSError) else exc.code)
    except OSError:
        failure = Refusal(2, 'publication_io')
    except Exception:
        failure = Refusal(2, 'adapter_error')
    try:
        try:
            await _close_stream(response)
        except Refusal as exc:
            failure = exc
        quiescent = await _cancel_inference(service, request) if attempted else None
        if not isinstance(request, dict) or not all(isinstance(request.get(key), str) and request[key]
                for key in ('request_id', 'provider_id', 'provider_revision')):
            raise core.ContractError(failure.code)
        result = _result(request, failure.rc, failure.code,
                         wall_ms=(time.monotonic_ns() - started_ns) // 1_000_000)
        if attempted and not quiescent:
            result['absence_reasons']['service_quiescence'] = 'unproven; scheduler lease must remain held'
        return result
    finally:
        _unlock_destination(lock)
