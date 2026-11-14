"""Independent struct-authored W8UF fixtures and fail-closed control tests.

The locally supplied W8UH and W8UE numeric values are *test-only* injected
contract values: they are not proposed production wire assignments.
"""
import hashlib
import json
import struct
import unittest

from vadapt import w8u_wire as wire

# Struct-authored vectors. No fixture uses the production W8UF encoder (none
# exists) or the production XWD encoder. All image bytes remain in memory.
FRAME = struct.Struct(">4sHHQQQQIIIHHQ32s32s")
CONTROL = struct.Struct(">4sHHI32s")
TEST_CONF_KIND = 187
TEST_END_KIND = 188
TEST_END_CODES = {0: "test-normal-completion", 5: "test-rejection"}
RGB_PIXELS = bytes((10, 20, 30, 40, 50, 60))
RGB_HASH = hashlib.sha256(RGB_PIXELS).digest()


def binding(cap=4096, inflight=2, tick_mode="committed", **changes):
    b = {"schema": "independent.test.binding/v1", "run_id": "synthetic-run-1",
         "lease_id": "lease-test", "seat_grant_sha256": "a"*64,
         "container_id": "container-test", "target_handle": {"kind": "synthetic",
             "canonical_id": "synthetic-window", "generation": 1},
         "epoch": 0, "display": ":99", "xid": 1, "width": 2,
         "height": 1, "frames_root": "/dev/shm/vqa-ramws",
         "frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/synthetic-run-1",
         "image_sha256": "b"*64, "reader_sha256": "c"*64,
         "exec_allowlist_sha256": "d"*64, "max_record_bytes": cap,
         "max_inflight_frames": inflight, "tick_mode": tick_mode}
    b.update(changes)
    return b


def limits(cap=4096, inflight=2):
    return wire.WireLimits(max_record_bytes=cap, max_control_bytes=16384,
                           max_inflight_frames=inflight,
                           binding_schema="independent.test.binding/v1",
                           xwd_error_codes={"E_XWD_SIZE":"E_LENGTH",
                              "E_XWD_SHORT":"E_LENGTH", "E_XWD_GEOMETRY":"E_GEOMETRY",
                              "E_XWD_BYTE_ORDER":"E_WIRE", "E_XWD_VERSION":"E_WIRE",
                              "E_XWD_FORMAT":"E_WIRE", "E_XWD_VISUAL":"E_WIRE"})


def canonical(b):
    return json.dumps(b, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(b):
    return hashlib.sha256(canonical(b)).digest()


def xwd(*, order=0, name=b"x\x00", ncolors=0, bit_order=0, stride=8,
        header_size=None, visual=4, width=2, height=1, version=7, masks=(0xFF0000,0xFF00,0xFF)):
    headsize = header_size if header_size is not None else 100 + len(name)
    fields = [headsize, version, 2, 24, width, height, 0, order, 32, bit_order,
              32, 32, stride, visual, *masks, 8, ncolors, ncolors, width,
              height, 0, 0, 0]
    # The XWD header is 25 big-endian unsigned 32-bit fields.
    assert len(fields) == 25, len(fields)
    pix = bytearray()
    for off in range(0, len(RGB_PIXELS), 3):
        r,g,b=RGB_PIXELS[off:off+3]
        pix.extend((b,g,r,0) if order==0 else (0,r,g,b))
    return struct.pack(">25I", *fields) + name + b"\0"*(12*ncolors) + bytes(pix)


def record(b, *, seq=1, tick=0, tick_valid=True, mono=99,
           version=1, flags=None, headbytes=128, recbytes=None,
           payloadbytes=None, inner_a=None, inner_b=None, hash_override=None,
           bind_digest=None, magic=b"W8UF", width=2, height=1, stride=6, fmt=1,
           length_a=None, length_b=None, tail=b""):
    a = xwd() if inner_a is None else inner_a
    z = xwd(order=1, name=b"other header\0") if inner_b is None else inner_b
    la = len(a) if length_a is None else length_a
    lb = len(z) if length_b is None else length_b
    body = struct.pack(">Q", la) + a + struct.pack(">Q", lb) + z + tail
    sz = 128 + len(body) if recbytes is None else recbytes
    pb = len(body) if payloadbytes is None else payloadbytes
    if flags is None: flags = 1 | (2 if tick_valid else 0)
    header = FRAME.pack(magic, version, headbytes, sz, seq, tick, mono,
                        width, height, stride, fmt, flags, pb,
                        RGB_HASH if hash_override is None else hash_override,
                        digest(b) if bind_digest is None else bind_digest)
    return header + body


def control(b, magic, kind, body=b"", n=None, bind_digest=None, ver=1):
    return CONTROL.pack(magic, ver, kind, len(body) if n is None else n,
                        digest(b) if bind_digest is None else bind_digest) + body


class Chunks:
    def __init__(self, source, *, chunk=None, overreturn=False):
        self.source = bytes(source)
        self.chunk = chunk
        self.overreturn = overreturn
        self.off = 0
        self.calls = []

    def read(self, n):
        self.calls.append(n)
        if self.off == len(self.source): return b""
        sz = (len(self.source) - self.off if self.overreturn else n)
        if self.chunk is not None: sz = min(sz,self.chunk)
        out = self.source[self.off:self.off+sz]
        self.off += len(out)
        return out


def verified_session(b, lim=None, *, initial_ack=True):
    lim = lim or limits()
    s = wire.WireSession.for_binding(b,lim)
    hello = control(b,b"W8UC",1,canonical(b))
    received = wire.parse_control(Chunks(hello),binding=b,limits=lim,session=s,
                      verify_hello=lambda bb,d: bb==b and d==digest(b))
    assert received.kind==1 and s.hello_received and not s.hello_confirmed
    # Numeric kind/body here are test-only, provided by an injected contract.
    confirm = control(b,b"W8UH",TEST_CONF_KIND,b"auth-ok")
    wire.parse_control(Chunks(confirm),binding=b,limits=lim,session=s,
             contract={"confirmation_kind":TEST_CONF_KIND},
             verify_confirmation=lambda body,bb,d: body==b"auth-ok" and d==digest(b))
    assert s.hello_confirmed
    if initial_ack:
        ack=control(b,b"W8UC",2,struct.pack(">QQ",0,min(2,b["max_inflight_frames"])))
        wire.parse_control(Chunks(ack),binding=b,limits=lim,session=s)
    return s


class WireTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding()
        self.lim = limits()

    def ready(self):
        return verified_session(self.binding,self.lim)

    def reject(self, raw, code, *, ready=True, b=None, lim=None, session=None):
        b = b or self.binding
        lim = lim or self.lim
        session = session or (verified_session(b,lim) if ready else wire.WireSession.for_binding(b,lim))
        before = session.next_seq
        with self.assertRaises(wire.WireError) as cm:
            wire.decode_record(Chunks(raw), binding=b, limits=lim, session=session)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(session.next_seq,before,"refused frame must not advance sequence")

    def test_positive_independent_rgb_pair(self):
        # A and B contain the same pixels but different XWD layouts and byte orders.
        s=self.ready()
        raw=record(self.binding,seq=1,tick=0)
        item=wire.decode_record(Chunks(raw),binding=self.binding,limits=self.lim,session=s)
        self.assertEqual(item.sha256,RGB_HASH.hex())
        self.assertNotEqual(item.xwd_a,item.xwd_b)
        self.assertEqual(item.tick,0,"committed tick zero is valid")
        self.assertEqual(set(item.metadata),{"seq","tick","tick_valid","capture_mono_ns",
                          "width","height","stride","pixel_format","sha256","binding_sha256"})
        self.assertEqual(s.next_seq,2)

    def test_positive_every_fragment_split_point(self):
        raw=record(self.binding)
        for cut in range(1,len(raw)):
            with self.subTest(split=cut):
                s=self.ready()
                class TwoChunk:
                    def __init__(self):self.i=0;self.parts=[raw[:cut],raw[cut:]]
                    def read(self,n):
                        if self.i>=len(self.parts):return b""
                        data=self.parts[self.i];self.i+=1
                        return data  # intentionally ignores n; reader buffers excess
                item=wire.decode_record(TwoChunk(),binding=self.binding,limits=self.lim,session=s)
                self.assertEqual(item.seq,1)

    def test_positive_coalesced_successors(self):
        s=self.ready()
        r=Chunks(record(self.binding,seq=1)+record(self.binding,seq=2),overreturn=True)
        self.assertEqual(wire.decode_record(r,binding=self.binding,limits=self.lim,session=s).seq,1)
        self.assertEqual(wire.decode_record(r,binding=self.binding,limits=self.lim,session=s).seq,2)
        self.assertIsNone(wire.decode_record(r,binding=self.binding,limits=self.lim,session=s))
        self.assertEqual(r.off,len(r.source))

    def test_empty_eof_vs_partial_header_and_body(self):
        self.assertIsNone(wire.decode_record(Chunks(b""),binding=self.binding,limits=self.lim,session=self.ready()))
        full=record(self.binding)
        for fragment in (b"W",full[:127],full[:131],full[:-1]):
            with self.subTest(fragment_size=len(fragment)):
                self.reject(fragment,"E_TORN")

    def test_reject_unconfirmed_before_read(self):
        b=self.binding;lim=self.lim;s=wire.WireSession.for_binding(b,lim);reader=Chunks(record(b))
        with self.assertRaises(wire.WireError) as cm:
            wire.decode_record(reader,binding=b,limits=lim,session=s)
        self.assertEqual(cm.exception.code,"E_BINDING")
        self.assertEqual(reader.off,0)

    def test_reject_magic_no_resynchronisation(self):
        self.reject(record(self.binding,magic=b"BAD!")+record(self.binding),"E_WIRE")

    def test_reject_version_header_format_flags(self):
        b=self.binding
        for kw in ({"version":2},{"headbytes":129},{"flags":0},{"flags":4},
                   {"flags":7},{"fmt":2}):
            with self.subTest(kw=kw):self.reject(record(b,**kw),"E_WIRE")

    def test_reject_size_overflow_before_body_read(self):
        b=self.binding
        raw=record(b,recbytes=(1<<64)-1)
        r=Chunks(raw)
        s=self.ready()
        with self.assertRaises(wire.WireError) as cm:
            wire.decode_record(r,binding=b,limits=self.lim,session=s)
        self.assertEqual(cm.exception.code,"E_LENGTH")
        self.assertEqual(r.off,128,"body must not be read following oversized header")

    def test_reject_payload_mismatch_and_underflow(self):
        for raw in (record(self.binding,payloadbytes=0),
                    record(self.binding,recbytes=127),
                    record(self.binding,payloadbytes=999)):
            self.reject(raw,"E_LENGTH")

    def test_reject_inner_length_overflow_without_allocation(self):
        b=self.binding
        raw=record(b,length_a=(1<<64)-1)
        r=Chunks(raw);s=self.ready()
        with self.assertRaises(wire.WireError) as cm:
            wire.decode_record(r,binding=b,limits=self.lim,session=s)
        self.assertEqual(cm.exception.code,"E_LENGTH")
        self.assertEqual(r.off,136)

    def test_reject_second_length_and_trailing_bytes(self):
        self.reject(record(self.binding,length_b=2),"E_LENGTH")
        self.reject(record(self.binding,tail=b"forged"),"E_LENGTH")

    def test_reject_outer_geometry_stride(self):
        for kw in ({"width":1},{"height":3},{"stride":7}):
            with self.subTest(kw=kw):self.reject(record(self.binding,**kw),"E_GEOMETRY")

    def test_reject_inner_geometry(self):
        self.reject(record(self.binding,inner_a=xwd(width=1)),"E_GEOMETRY")

    def test_reject_xwd_header_and_color_count(self):
        self.reject(record(self.binding,inner_a=xwd(header_size=65537)),"E_LENGTH")
        raw=bytearray(xwd());struct.pack_into(">I",raw,19*4,65537)
        self.reject(record(self.binding,inner_a=bytes(raw)),"E_LENGTH")

    def test_reject_bitmap_bit_order_masks_stride(self):
        self.reject(record(self.binding,inner_a=xwd(bit_order=2)),"E_WIRE")
        self.reject(record(self.binding,inner_a=xwd(masks=(0xff,0xff00,0xff0000))),"E_WIRE")
        raw=bytearray(xwd());struct.pack_into(">I",raw,12*4,65540)
        self.reject(record(self.binding,inner_a=bytes(raw)),"E_GEOMETRY")

    def test_reject_exact_inner_xwd_size(self):
        self.reject(record(self.binding,inner_a=xwd()+b"extra"),"E_LENGTH")
        self.reject(record(self.binding,inner_a=xwd()[:-1]),"E_LENGTH")

    def test_reject_wrong_rgb_digest_or_second_pair(self):
        self.reject(record(self.binding,hash_override=b"\0"*32),"E_HASH")
        corrupted=bytearray(xwd(order=1,name=b"other header\0"));corrupted[-1]^=0x10
        self.reject(record(self.binding,inner_b=bytes(corrupted)),"E_HASH")

    def test_reject_binding_digest(self):
        self.reject(record(self.binding,bind_digest=b"X"*32),"E_BINDING")

    def test_reject_seq_gap_duplicate(self):
        self.reject(record(self.binding,seq=2),"E_SEQ")
        s=self.ready()
        wire.decode_record(Chunks(record(self.binding,seq=1)),binding=self.binding,limits=self.lim,session=s)
        self.reject(record(self.binding,seq=1),"E_SEQ",session=s)
        self.reject(record(self.binding,seq=3),"E_SEQ",session=s)

    def test_tick_committed_and_unavailable(self):
        self.reject(record(self.binding,tick_valid=False),"E_TICK")
        b=binding(tick_mode="unavailable")
        lim=self.lim;s=verified_session(b,lim)
        item=wire.decode_record(Chunks(record(b,tick_valid=False,tick=0)),binding=b,limits=lim,session=s)
        self.assertFalse(item.tick_valid)
        self.assertIsNone(item.tick)
        for kw in ({"tick_valid":True,"tick":0},{"tick_valid":False,"tick":1}):
            self.reject(record(b,**kw),"E_TICK",b=b,lim=lim)

    def test_binding_strict_types_closed_root_and_capacity(self):
        for b,code in ((binding(width=True),"E_BINDING"),
                       (binding(width=1.0),"E_BINDING"),
                       (binding(unexpected="extra"),"E_BINDING"),
                       (binding(frames_root="/tmp"),"E_ROOT"),
                       (binding(frame_run_root="/dev/shm/vqa-ramws/../unsafe"),"E_ROOT"),
                       (binding(max_inflight_frames=3),"E_BINDING")):
            with self.subTest(b=b,code=code):
                with self.assertRaises(wire.WireError) as cm:
                    wire.validate_binding(b,self.lim)
                self.assertEqual(cm.exception.code,code)

    def test_hello_cannot_authorise_without_independent_grant(self):
        b=self.binding;s=wire.WireSession.for_binding(b,self.lim)
        h=control(b,b"W8UC",1,canonical(b))
        with self.assertRaises(wire.WireError) as cm:
            wire.parse_control(Chunks(h),binding=b,limits=self.lim,session=s)
        self.assertEqual(cm.exception.code,"E_BINDING")
        self.assertFalse(s.hello_received)
        self.assertFalse(s.hello_confirmed)
        self.assertEqual(s.credits.credit_total,0)

    def test_hello_reject_noncanonical_duplicate_unknown(self):
        b=self.binding
        for body in (json.dumps(b).encode(), canonical(b).replace(b'"width":2',b'"width":2,"width":2'),
                     canonical(b)+b" "):
            with self.subTest(body=body[:25]):
                s=wire.WireSession.for_binding(b,self.lim)
                with self.assertRaises(wire.WireError) as cm:
                    wire.parse_control(Chunks(control(b,b"W8UC",1,body)),binding=b,limits=self.lim,
                                       session=s,verify_hello=lambda *_:True)
                self.assertEqual(cm.exception.code,"E_BINDING")
                self.assertFalse(s.hello_received)

    def test_confirm_requires_frozen_contract_and_callback(self):
        b=self.binding;s=wire.WireSession.for_binding(b,self.lim)
        wire.parse_control(Chunks(control(b,b"W8UC",1,canonical(b))),binding=b,limits=self.lim,
                           session=s,verify_hello=lambda *_: True)
        conf=control(b,b"W8UH",TEST_CONF_KIND,b"auth-ok")
        for kw in ({},{"contract":{"confirmation_kind":TEST_CONF_KIND}}):
            with self.subTest(kw=kw):
                with self.assertRaises(wire.WireError):
                    wire.parse_control(Chunks(conf),binding=b,limits=self.lim,session=s,**kw)
                self.assertFalse(s.hello_confirmed)
        wire.parse_control(Chunks(conf),binding=b,limits=self.lim,session=s,
                           contract={"confirmation_kind":TEST_CONF_KIND},
                           verify_confirmation=lambda body,*_:body==b"auth-ok")
        self.assertTrue(s.hello_confirmed)

    def test_ack_credit_window_capacity_one_and_duplicate(self):
        w=wire.CreditWindow(1)
        w.accept(0,1,produced=0)
        w.require_credit(1)
        with self.assertRaises(wire.WireError) as cm:w.require_credit(2)
        self.assertEqual(cm.exception.code,"E_CREDIT")
        w.accept(0,1,produced=0)
        self.assertEqual(w.credit_total,1)
        w.accept(1,2,produced=1)
        w.require_credit(2)
        self.assertEqual(w.ack_seq,1)

    def test_ack_credit_reject_inflation_exhaustion_and_replay_mutation(self):
        w=wire.CreditWindow(2)
        for grant in (0,1,3):
            with self.subTest(grant=grant),self.assertRaises(wire.WireError) as cm:
                w.accept(0,grant,produced=0)
            self.assertEqual(cm.exception.code,"E_CREDIT")
            self.assertFalse(w.started)
        w.accept(0,2,produced=0)
        for a,t,p in ((0,3,0),(1,4,1),(2,5,1),(1,1,1)):
            with self.subTest(args=(a,t,p)),self.assertRaises(wire.WireError) as cm:
                w.accept(a,t,produced=p)
            self.assertEqual(cm.exception.code,"E_CREDIT")
            self.assertEqual(w.credit_total,2)

    def test_control_ack_parsing_requires_confirmation(self):
        b=self.binding
        s=wire.WireSession.for_binding(b,self.lim)
        raw=control(b,b"W8UC",2,struct.pack(">QQ",0,2))
        with self.assertRaises(wire.WireError) as cm:
            wire.parse_control(Chunks(raw),binding=b,limits=self.lim,session=s)
        self.assertEqual(cm.exception.code,"E_CREDIT")
        s=verified_session(b,self.lim,initial_ack=False)
        got=wire.parse_control(Chunks(raw),binding=b,limits=self.lim,session=s)
        self.assertEqual((got.ack_seq,got.cumulative_credit_total),(0,2))

    def test_frame_without_ack_is_credit_refusal_before_body(self):
        b=self.binding
        s=verified_session(b,self.lim,initial_ack=False)
        raw=record(b)
        r=Chunks(raw)
        with self.assertRaises(wire.WireError) as cm:
            wire.decode_record(r,binding=b,limits=self.lim,session=s)
        self.assertEqual(cm.exception.code,"E_CREDIT")
        self.assertEqual(r.off,128,"zero-credit frame body must never be fetched")
        self.assertEqual(s.next_seq,1)

    def test_credit_exhaustion_in_received_frames(self):
        b=binding(inflight=1)
        lim=limits(inflight=1)
        s=verified_session(b,lim)
        wire.decode_record(Chunks(record(b,seq=1)),binding=b,limits=lim,session=s)
        r=Chunks(record(b,seq=2))
        with self.assertRaises(wire.WireError) as cm:
            wire.decode_record(r,binding=b,limits=lim,session=s)
        self.assertEqual(cm.exception.code,"E_CREDIT")
        self.assertEqual(r.off,128)
        ack=control(b,b"W8UC",2,struct.pack(">QQ",1,2))
        with self.assertRaises(wire.WireError) as cm:
            wire.parse_control(Chunks(ack),binding=b,limits=lim,session=s)
        self.assertEqual(cm.exception.code,"E_CREDIT","missing publication/release evidence")
        got=wire.parse_control(Chunks(ack),binding=b,limits=lim,session=s,
                               verify_ack_release=lambda ack_seq,binding:ack_seq==1 and binding==b)
        self.assertEqual(got.cumulative_credit_total,2)
        self.assertEqual(wire.decode_record(Chunks(record(b,seq=2)),binding=b,limits=lim,session=s).seq,2)

    def test_stop_bypasses_exhausted_credit(self):
        b=self.binding;s=verified_session(b,self.lim,initial_ack=False)
        self.assertFalse(s.credits.started)
        got=wire.parse_control(Chunks(control(b,b"W8UC",3)),binding=b,limits=self.lim,session=s)
        self.assertEqual(got.kind,3)
        self.assertTrue(s.stopped)
        self.reject(record(b),"E_BINDING",session=s)

    def test_controls_reject_version_kinds_oversize(self):
        b=self.binding
        for raw in (control(b,b"BAD!",1),control(b,b"W8UC",22),
                    control(b,b"W8UC",3,b"x"),control(b,b"W8UC",1,b"x"*16385),
                    control(b,b"W8UC",1,canonical(b),ver=2)):
            s=wire.WireSession.for_binding(b,self.lim)
            with self.subTest(raw_head=raw[:12]),self.assertRaises(wire.WireError):
                wire.parse_control(Chunks(raw),binding=b,limits=self.lim,session=s,
                                   verify_hello=lambda *_: True)
            self.assertFalse(s.hello_confirmed)

    def test_control_partial_eof_and_fragmented_positive(self):
        b=self.binding
        h=control(b,b"W8UC",1,canonical(b))
        for cut in range(1,len(h)):
            with self.subTest(split=cut):
                s=wire.WireSession.for_binding(b,self.lim)
                class Split:
                    def __init__(self):self.i=0;self.parts=[h[:cut],h[cut:]]
                    def read(self,n):
                        if self.i >= len(self.parts):return b""
                        chunk=self.parts[self.i];self.i+=1
                        return chunk
                got=wire.parse_control(Split(),binding=b,limits=self.lim,session=s,
                                       verify_hello=lambda bb,d:bb==b and d==digest(b))
                self.assertEqual(got.kind,1)
                # A separate deliberately fragmented trace, not a production encoder.
                self.assertTrue(s.hello_received)
        for raw in (h[:1],h[:43],h[:-1]):
            with self.subTest(truncated=len(raw)):
                s=wire.WireSession.for_binding(b,self.lim)
                with self.assertRaises(wire.WireError) as cm:
                    wire.parse_control(Chunks(raw),binding=b,limits=self.lim,
                                       session=s,verify_hello=lambda *_:True)
                self.assertEqual(cm.exception.code,"E_TORN")
                self.assertFalse(s.hello_received)

    def test_control_refuse_oversized_before_body(self):
        b=self.binding
        raw=control(b,b"W8UC",1,b"",n=16385)
        stream=Chunks(raw)
        session=wire.WireSession.for_binding(b,self.lim)
        with self.assertRaises(wire.WireError) as cm:
            wire.parse_control(stream,binding=b,limits=self.lim,session=session,
                               verify_hello=lambda *_:True)
        self.assertEqual(cm.exception.code,"E_LENGTH")
        self.assertEqual(stream.off,44)
        self.assertFalse(session.hello_received)

    def test_oversize_coalesced_surplus_is_bounded(self):
        b=self.binding
        stream=Chunks(record(b)+b"x"*5000,overreturn=True)
        session=self.ready()
        with self.assertRaises(wire.WireError) as cm:
            wire.decode_record(stream,binding=b,limits=self.lim,session=session)
        self.assertEqual(cm.exception.code,"E_LENGTH")
        self.assertEqual(session.next_seq,1)

    def test_end_terminal_session_forbids_reuse(self):
        b=self.binding
        s=self.ready()
        end=control(b,b"W8UE",TEST_END_KIND,struct.pack(">QQI",0,0,0))
        contract={"end_kind":TEST_END_KIND,"end_error_codes":TEST_END_CODES}
        wire.parse_control(Chunks(end),binding=b,limits=self.lim,session=s,
                           contract=contract,verify_end_exit=lambda *_:True)
        self.assertTrue(s.ended)
        with self.assertRaises(wire.WireError) as cm:
            wire.parse_control(Chunks(control(b,b"W8UC",3)),binding=b,limits=self.lim,session=s)
        self.assertEqual(cm.exception.code,"E_WIRE")

    def test_end_requires_external_codes_eof_and_join(self):
        b=self.binding;con={"end_kind":TEST_END_KIND,"end_error_codes":TEST_END_CODES}
        raw=control(b,b"W8UE",TEST_END_KIND,struct.pack(">QQI",0,0,0))
        s=self.ready()
        with self.assertRaises(wire.WireError):
            wire.parse_control(Chunks(raw),binding=b,limits=self.lim,session=s)
        self.assertFalse(s.ended)
        for inp in (raw+b"trailing",control(b,b"W8UE",TEST_END_KIND,struct.pack(">QQI",0,0,99))):
            with self.subTest(raw=inp[-10:]),self.assertRaises(wire.WireError):
                wire.parse_control(Chunks(inp),binding=b,limits=self.lim,session=self.ready(),
                                   contract=con,verify_end_exit=lambda *_:True)
        nojoin=self.ready()
        with self.assertRaises(wire.WireError):
            wire.parse_control(Chunks(raw),binding=b,limits=self.lim,session=nojoin,contract=con)
        self.assertFalse(nojoin.ended)
        got=wire.parse_control(Chunks(raw),binding=b,limits=self.lim,session=self.ready(),
                               contract=con,verify_end_exit=lambda *args:args==(0,0,0))
        self.assertEqual((got.end_last_seq,got.end_sent,got.end_error_code),(0,0,0))


MANDATORY_CASES = (
    "tests.test_w8u_ve_wire_unit.WireTests.test_positive_independent_rgb_pair",
    "tests.test_w8u_ve_wire_unit.WireTests.test_positive_every_fragment_split_point",
    "tests.test_w8u_ve_wire_unit.WireTests.test_positive_coalesced_successors",
    "tests.test_w8u_ve_wire_unit.WireTests.test_empty_eof_vs_partial_header_and_body",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_unconfirmed_before_read",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_magic_no_resynchronisation",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_version_header_format_flags",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_size_overflow_before_body_read",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_payload_mismatch_and_underflow",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_inner_length_overflow_without_allocation",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_second_length_and_trailing_bytes",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_outer_geometry_stride",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_inner_geometry",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_xwd_header_and_color_count",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_bitmap_bit_order_masks_stride",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_exact_inner_xwd_size",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_wrong_rgb_digest_or_second_pair",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_binding_digest",
    "tests.test_w8u_ve_wire_unit.WireTests.test_reject_seq_gap_duplicate",
    "tests.test_w8u_ve_wire_unit.WireTests.test_tick_committed_and_unavailable",
    "tests.test_w8u_ve_wire_unit.WireTests.test_binding_strict_types_closed_root_and_capacity",
    "tests.test_w8u_ve_wire_unit.WireTests.test_hello_cannot_authorise_without_independent_grant",
    "tests.test_w8u_ve_wire_unit.WireTests.test_hello_reject_noncanonical_duplicate_unknown",
    "tests.test_w8u_ve_wire_unit.WireTests.test_confirm_requires_frozen_contract_and_callback",
    "tests.test_w8u_ve_wire_unit.WireTests.test_ack_credit_window_capacity_one_and_duplicate",
    "tests.test_w8u_ve_wire_unit.WireTests.test_ack_credit_reject_inflation_exhaustion_and_replay_mutation",
    "tests.test_w8u_ve_wire_unit.WireTests.test_control_ack_parsing_requires_confirmation",
    "tests.test_w8u_ve_wire_unit.WireTests.test_frame_without_ack_is_credit_refusal_before_body",
    "tests.test_w8u_ve_wire_unit.WireTests.test_credit_exhaustion_in_received_frames",
    "tests.test_w8u_ve_wire_unit.WireTests.test_stop_bypasses_exhausted_credit",
    "tests.test_w8u_ve_wire_unit.WireTests.test_controls_reject_version_kinds_oversize",
    "tests.test_w8u_ve_wire_unit.WireTests.test_control_partial_eof_and_fragmented_positive",
    "tests.test_w8u_ve_wire_unit.WireTests.test_control_refuse_oversized_before_body",
    "tests.test_w8u_ve_wire_unit.WireTests.test_oversize_coalesced_surplus_is_bounded",
    "tests.test_w8u_ve_wire_unit.WireTests.test_end_terminal_session_forbids_reuse",
    "tests.test_w8u_ve_wire_unit.WireTests.test_end_requires_external_codes_eof_and_join",
)
