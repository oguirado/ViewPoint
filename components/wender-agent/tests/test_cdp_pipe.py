import os
import unittest

from wender.perceive import cdp_pipe


class TestFraming(unittest.TestCase):
    def test_frame_is_nul_terminated_compact(self):
        self.assertEqual(cdp_pipe.frame({"id": 1, "method": "X"}), b'{"id":1,"method":"X"}\x00')

    def test_deframer_split_across_chunks(self):
        d = cdp_pipe.Deframer()
        data = cdp_pipe.frame({"id": 1}) + cdp_pipe.frame({"method": "E", "p": "\u00e9"})
        out = []
        for i in range(len(data)):
            out += d.feed(data[i:i + 1])
        self.assertEqual(out, [{"id": 1}, {"method": "E", "p": "\u00e9"}])

    def test_args_use_pipe_not_port(self):
        a = cdp_pipe.chrome_args("/tmp/x")
        self.assertIn("--remote-debugging-pipe", a)
        self.assertFalse(any(x.startswith("--remote-debugging-port") for x in a))

    def test_missing_chrome_bin(self):
        old = os.environ.pop("CHROME_BIN", None)
        try:
            with self.assertRaises(cdp_pipe.CdpError):
                cdp_pipe.CdpPipe()
        finally:
            if old is not None:
                os.environ["CHROME_BIN"] = old

    def test_fake_browser_roundtrip_and_timeout(self):
        """A python child that speaks the fd 3/4 protocol stands in for Chromium."""
        import sys
        import tempfile
        fake = tempfile.NamedTemporaryFile("w", suffix=".py", dir="/tmp", delete=False)
        fake.write(
            "import json,os,sys\n"
            "buf=b''\n"
            "while True:\n"
            "  d=os.read(3,65536)\n"
            "  if not d: break\n"
            "  buf+=d\n"
            "  while b'\\0' in buf:\n"
            "    m,buf=buf.split(b'\\0',1); m=json.loads(m)\n"
            "    if m['method']=='Silent': continue\n"
            "    if m['method']=='Browser.close': sys.exit(0)\n"
            "    os.write(4,json.dumps({'method':'Ev.x','params':{}}).encode()+b'\\0')\n"
            "    os.write(4,json.dumps({'id':m['id'],'result':{'echo':m['method']}}).encode()+b'\\0')\n")
        fake.close()
        wrapper = tempfile.NamedTemporaryFile("w", suffix=".sh", dir="/tmp", delete=False)
        wrapper.write('#!/bin/sh\nexec %s %s\n' % (sys.executable, fake.name))
        wrapper.close()
        os.chmod(wrapper.name, 0o755)
        try:
            with cdp_pipe.CdpPipe(chrome_bin=wrapper.name, timeout=5) as c:
                self.assertEqual(c.send("A.b"), {"echo": "A.b"})
                self.assertEqual(c.wait_event("Ev.x")["method"], "Ev.x")
                with self.assertRaises(cdp_pipe.CdpError):
                    c.send("Silent", timeout=0.2)
                udd = c.user_data_dir
            self.assertFalse(os.path.exists(udd))
            self.assertIsNotNone(c.proc.returncode)
        finally:
            os.unlink(fake.name)
            os.unlink(wrapper.name)


if __name__ == "__main__":
    unittest.main()
