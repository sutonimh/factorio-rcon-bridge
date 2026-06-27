#!/usr/bin/env python3
"""Minimal Source-RCON client for Factorio.

Usage:
    python3 rcon.py "<command>"          run one command, print the response
    echo "<command>" | python3 rcon.py   read command from stdin
    python3 rcon.py --ping                connectivity check

Reads host/port/password from env or the local files:
    FACTORIO_RCON_HOST  (default 127.0.0.1)
    FACTORIO_RCON_PORT  (default 27015)
    FACTORIO_RCON_PASS  (default contents of ./rcon.pass)

Factorio command notes:
    /sc <lua>            silent-command: run Lua, no console echo
    /sc rcon.print(x)    return data x back over RCON (use this for reads)
    /c  <lua>            command: runs Lua and echoes (disables achievements)
"""
import os, sys, socket, struct, pathlib

HERE = pathlib.Path(__file__).resolve().parent
HOST = os.environ.get("FACTORIO_RCON_HOST", "127.0.0.1")
PORT = int(os.environ.get("FACTORIO_RCON_PORT", "27015"))
PASS = os.environ.get("FACTORIO_RCON_PASS") or (HERE / "rcon.pass").read_text().strip()

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_RESPONSE_VALUE = 0


def _pack(pid, ptype, body):
    payload = struct.pack("<ii", pid, ptype) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _read(sock):
    raw_len = b""
    while len(raw_len) < 4:
        chunk = sock.recv(4 - len(raw_len))
        if not chunk:
            raise ConnectionError("socket closed reading length")
        raw_len += chunk
    (length,) = struct.unpack("<i", raw_len)
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("socket closed reading body")
        data += chunk
    pid, ptype = struct.unpack("<ii", data[:8])
    body = data[8:-2].decode("utf-8", "replace")
    return pid, ptype, body


def run(command, timeout=10.0):
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        # auth
        s.sendall(_pack(1, SERVERDATA_AUTH, PASS))
        pid, ptype, _ = _read(s)
        # some servers send an empty RESPONSE_VALUE first; read until AUTH_RESPONSE
        while ptype != SERVERDATA_AUTH_RESPONSE:
            pid, ptype, _ = _read(s)
        if pid == -1:
            raise PermissionError("RCON auth failed (bad password)")
        # send command, read the response packet(s). Factorio replies with a
        # single RESPONSE_VALUE for typical commands; drain any extras briefly.
        s.sendall(_pack(2, SERVERDATA_EXECCOMMAND, command))
        out = []
        pid, ptype, body = _read(s)
        out.append(body)
        s.settimeout(0.25)
        try:
            while True:
                _, _, body = _read(s)
                out.append(body)
        except (TimeoutError, socket.timeout):
            pass
        return "".join(out)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--ping":
        print(run("/sc rcon.print('pong tick='..game.tick)"))
        sys.exit(0)
    cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    cmd = cmd.strip()
    if not cmd:
        print("no command given", file=sys.stderr)
        sys.exit(2)
    print(run(cmd))
