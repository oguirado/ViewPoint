"""Test-only Unix API and VSOCK peer with selectable reply failures."""
from pathlib import Path
import json
import re
import socket
import sys

api_path, trace, fault = sys.argv[1:]
trace = Path(trace)


def record(**data):
    with trace.open("a") as file:
        file.write(json.dumps(data) + "\n")


def listener(path):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen()
    return server


def text_line(stream):
    return stream.readline().decode("ascii").rstrip("\n")


with listener(api_path) as api:
    for _ in range(4):
        conn, _ = api.accept()
        with conn, conn.makefile("rb") as stream:
            method, path, version = text_line(stream).rstrip("\r").split()
            headers = {}
            while (line := text_line(stream).rstrip("\r")):
                key, value = line.split(":", 1)
                headers[key.lower()] = value.strip()
            body = json.loads(stream.read(int(headers["content-length"])))
            record(method=method, path=path, body=body)
            if path == "/boot-source":
                nonce = re.search(r"mv_nonce=([0-9a-f]{64})$", body["boot_args"])[1]
            if path == "/vsock":
                vsock_path = body["uds_path"]
            if fault == "api_reject" and path == "/machine-config":
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                sys.exit(3)
            conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

with listener(vsock_path) as vsock:
    while True:
        conn, _ = vsock.accept()
        with conn, conn.makefile("rb") as stream:
            if text_line(stream) != "CONNECT 19001":
                continue
            conn.sendall(b"OK 12345\n")
            while (line := text_line(stream)):
                op, received, *args = line.split()
                record(guest_op=op, args=args)
                if received != nonce:
                    break
                if op == "HELLO":
                    reply = f"HELLO {nonce}"
                elif op == "PING":
                    reply = f"PONG {nonce}"
                elif op == "UNAME":
                    reply = f'UNAME {nonce} Linux test {"x86_64" if fault == "wrong_isa" else "aarch64"}'
                elif op == "SUMSQ":
                    n = int(args[0])
                    value = sum(i * i for i in range(1, n + 1))
                    if fault == "lost_job":
                        break
                    reply = f'SUMSQ {nonce} {n} {value + (fault == "wrong_result")}'
                elif op == "STOP":
                    if fault == "lost_stop":
                        break
                    conn.sendall(f"STOPPING {nonce}\n".encode())
                    sys.exit(0)
                else:
                    break
                conn.sendall((reply + "\n").encode())
