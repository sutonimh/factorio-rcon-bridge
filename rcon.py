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
import itertools, os, sys, socket, struct, pathlib

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


def _run_once(command, timeout=10.0):
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



def run(command, timeout=10.0):
    """Bounded retry on CONNECT-phase failures (refused/reset) - one RCON blip was
    process-fatal and drove 18 crash-restarts in 108 min (audit 2026-08-29). Refused/reset
    mean nothing executed, so retrying is safe; timeouts retry once only (a post-send
    timeout re-sent could double-execute a mutating /sc)."""
    import socket as _s
    import time as _t
    for attempt in (1, 2, 3):
        try:
            return _run_once(command, timeout)
        except (ConnectionRefusedError, ConnectionResetError) as e:
            if attempt == 3:
                raise
            _t.sleep(1.5 * attempt)
        except (_s.timeout, TimeoutError):
            if attempt >= 2:
                raise
            _t.sleep(2.0)


class ChunkedReadError(RuntimeError):
    """A chunked read that did not come back WHOLE. Never a partial payload."""


READ_CHUNK = 3000                 # chars per slice; one large RCON response truncates
_READ_SEQ = itertools.count(1)


def _mint_store():
    """A scratch Lua global no other read can be using: pid + a process-local counter."""
    return "storage._rd%d_%d" % (os.getpid(), next(_READ_SEQ))


def read_chunked(build_lua, chunk=READ_CHUNK, tries=2, run=None, empty="{}"):
    """Build a payload into a PRIVATE Lua global and read it back whole. Returns the payload.

    `build_lua(store)` is a CALLABLE that returns one /sc which assigns the payload to `store`
    (the full Lua expression, e.g. `storage._rd4711_3`) and rcon.prints its length. It is a
    callable and not a string ON PURPOSE: a fixed key is the bug this function exists to
    remove, and a string would have to be rewritten by hand to be made private, which is the
    same mistake with extra steps.

    WHY A PRIVATE KEY. The build and the N slice reads are N+1 separate RCON round-trips, and
    the invariant thread, the builder loop and the pole-plan verify all ran chunked reads
    against the SAME `storage._pgrid` concurrently. A writer landing between two slice reads
    swapped the buffer out from under the reader, so the reassembled string was the head of one
    document spliced onto the tail of another. Live, 2026-08-29 23:27:07: the audit read 46926
    chars of one scan, the builder's array_grid scan replaced it mid-read, and what came back
    was `..."working"},{"n":","bb":[-6,5,...` - two valid documents, one meaningless
    `Expecting ',' delimiter: line 1 column 9003` at the chunk boundary where they met.

    WHY THE LENGTH IS CHECKED. Nothing in the repo compared the reassembled length against the
    length Lua reported, so a splice could only ever surface as a JSONDecodeError at an offset
    that means nothing. With unique keys a concurrent writer can no longer cause a mismatch at
    all; the check is the backstop that makes any REMAINING cause loud instead of silent.

    A non-int head RAISES and is never read as zero. A Lua runtime error comes back as prose,
    and int(prose) swallowed would read as "this area has no entities" - a failed read must be
    indistinguishable from no read at all, never from an answer (supply_planner._chunked).
    """
    if not callable(build_lua):
        raise TypeError("read_chunked(build_lua) takes a callable(store) -> lua, so the "
                        "buffer key can be minted per read; a fixed key is the race")
    send = run or globals()["run"]
    last = None
    for attempt in range(1, int(tries) + 1):
        store = _mint_store()
        try:
            head = (send(build_lua(store)) or "").strip()
            # AN EMPTY RESPONSE IS NOT A ZERO. `int(head or "0")` folded "" into 0 and then
            # returned `empty` as a successful answer, which is precisely the invariant this
            # function documents two paragraphs up: a failed read must be indistinguishable
            # from no read at all, never from an answer. The builder ALWAYS ends in
            # `rcon.print(#store)`, so a well-formed reply is at minimum the string "0" - and
            # "0" stays a legitimate answer (an area with no entities). Nothing back at all
            # means the command never ran: a dropped connection, a truncated /sc, a Lua error
            # swallowed before the print.
            if not head:
                raise ChunkedReadError(
                    "chunked read into %s: the server returned NOTHING where a payload length "
                    "was expected - the command did not run, so this is unreadable, not an "
                    "empty answer" % store)
            try:
                n = int(head)
            except ValueError:
                raise ChunkedReadError(
                    "chunked read into %s: expected a payload length, the server returned %r "
                    "- treating this as unreadable, not as an empty answer" % (store, head[:160]))
            if n == 0:
                return empty
            parts, i = [], 1
            while i <= n:
                # `or ""` so a dropped slice becomes a SHORT read the length check names,
                # rather than an AttributeError on None from inside a verify_fn
                parts.append((send("/sc rcon.print(%s:sub(%d,%d))"
                                   % (store, i, i + chunk - 1)) or "").rstrip("\r\n"))
                i += chunk
            body = "".join(parts)
            if len(body) != n:
                raise ChunkedReadError(
                    "chunked read of %s: reassembled %d chars, Lua reported %d (delta %+d) - "
                    "the buffer was clobbered mid-read or a slice came back short"
                    % (store, len(body), n, len(body) - n))
            return body
        except ChunkedReadError as e:
            last = e
            if attempt >= int(tries):
                raise
        finally:
            # never leave a 47 kB string behind in the save, least of all on the raising path
            try:
                send("/sc %s=nil" % store)
            except Exception:
                pass
    raise last


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
