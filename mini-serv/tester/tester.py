#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mini_serv 厳しめテスター (42 exam rank 06)

使い方:
  python3 tester.py ../complete/mini_serv.c                 # 全テスト
  python3 tester.py ../complete/mini_serv.c -k lazy,flood   # 名前に一致するテストだけ
  python3 tester.py ../complete/mini_serv.c --asan          # ASan/UBSan ビルドで実行 (メモリ破壊検出)
  python3 tester.py ../complete/mini_serv.c --clients 300   # 大量接続テストの人数 (既定 1000, 最大 1015)
  python3 tester.py --list                                  # テスト一覧

判定:
  PASS  OK
  FAIL  課題文の要件違反、または本番の採点で落ちる可能性が高いもの (exit code 1)
  WARN  要件とは言い切れないが挙動が怪しいもの
  SKIP  環境の都合で実行できなかったもの

本番 (Linux) とは TCP の挙動が少し違うので、可能なら Linux (Docker など) でも実行すること。
"""
import argparse
import os
import random
import re
import resource
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HOST = "127.0.0.1"

ALLOWED = {
    "write", "close", "select", "socket", "accept", "listen", "send", "recv", "bind",
    "strstr", "malloc", "realloc", "free", "calloc", "bzero", "atoi", "sprintf",
    "strlen", "exit", "strcpy", "strcat", "memset",
}
# コンパイラ / libc が自動で差し込むシンボル (自分で呼んでいなくても出てくる)
COMPILER_SYMS = {
    "__stack_chk_fail", "__stack_chk_guard", "__darwin_check_fd_set_overflow",
    "__error", "__errno_location", "__sprintf_chk", "__fdelt_chk", "__libc_start_main",
    "__cxa_finalize", "__gmon_start__", "_ITM_deregisterTMCloneTable",
    "_ITM_registerTMCloneTable", "dyld_stub_binder", "__bzero", "__memset_chk",
    "__strcpy_chk", "__strcat_chk", "__memcpy_chk", "memcpy", "memmove", "htons",
    "htonl", "_GLOBAL_OFFSET_TABLE_", "abort", "__register_frame_info",
    "__deregister_frame_info",
}

USE_COLOR = sys.stdout.isatty()


def color(s, code):
    return "\033[%sm%s\033[0m" % (code, s) if USE_COLOR else s


class Fail(Exception):
    pass


class Warn(Exception):
    pass


class Skip(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def arrived(i):
    return "server: client %d just arrived\n" % i


def left(i):
    return "server: client %d just left\n" % i


def line(i, text):
    return "client %d: %s\n" % (i, text)


def diff(exp, got, width=70):
    n = min(len(exp), len(got))
    i = 0
    step = 4096
    while i + step <= n and exp[i:i + step] == got[i:i + step]:
        i += step
    while i < n and exp[i] == got[i]:
        i += 1
    lo = max(0, i - width // 2)
    return ("  最初の不一致: %d byte 目 (期待 %d byte / 実際 %d byte)\n"
            "  期待: %r\n  実際: %r" % (i, len(exp), len(got), exp[lo:i + width], got[lo:i + width]))


def free_port():
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def local_ip():
    for target in ("10.255.255.255", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target, 1))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                return ip
        except OSError:
            pass
    return None


def is_listening(port, pid):
    if os.path.exists("/proc/net/tcp"):
        with open("/proc/net/tcp") as f:
            for l in f.readlines()[1:]:
                parts = l.split()
                if parts[3] == "0A" and int(parts[1].split(":")[1], 16) == port:
                    return True
        return False
    out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout
    return any(len(l.split()) > 1 and l.split()[1] == str(pid) for l in out.splitlines()[1:])


def fd_count(pid):
    p = "/proc/%d/fd" % pid
    if os.path.isdir(p):
        try:
            return len(os.listdir(p))
        except OSError:
            return None
    if not shutil.which("lsof"):
        return None
    out = subprocess.run(["lsof", "-nP", "-p", str(pid)], capture_output=True, text=True).stdout.splitlines()
    if not out:
        return None
    idx = out[0].split().index("FD")
    return sum(1 for l in out[1:] if len(l.split()) > idx and re.match(r"^\d+", l.split()[idx]))


class Server:
    def __init__(self, binary, wrap=None, start_timeout=10.0):
        self.port = free_port()
        self.next_id = 0
        self.proc = subprocess.Popen((wrap or []) + [binary, str(self.port)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.pid = self.proc.pid
        self.stopped = None
        deadline = time.time() + start_timeout
        while not is_listening(self.port, self.pid):
            if self.proc.poll() is not None:
                out, err = self.proc.communicate()
                raise Fail("サーバーが起動直後に終了 (exit %d) stderr=%r" % (self.proc.returncode, err[:200]))
            if time.time() > deadline:
                raise Fail("サーバーが %d 秒以内に listen しない" % start_timeout)
            time.sleep(0.02)

    def alive(self):
        return self.proc.poll() is None

    def death_reason(self):
        rc = self.proc.returncode
        if rc is None:
            return None
        if rc < 0:
            try:
                return "シグナル %s で終了" % signal.Signals(-rc).name
            except ValueError:
                return "シグナル %d で終了" % -rc
        return "exit %d で終了" % rc

    def assert_alive(self, when=""):
        if not self.alive():
            raise Fail("サーバーが落ちた%s (%s)" % (when, self.death_reason()))

    def stop(self):
        if self.stopped is not None:
            return self.stopped
        died_early = not self.alive()
        if not died_early:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        out, err = self.proc.communicate()
        self.stopped = (died_early, out, err)
        return self.stopped


class Client:
    def __init__(self, port, cid=None, rcvbuf=None, nodelay=False):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        if nodelay:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(15)
        s.connect((HOST, port))
        self.s = s
        self.id = cid
        self.buf = bytearray()
        self.eof = False
        self.closed = False

    def name(self):
        return "client %s" % self.id

    def send(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.s.sendall(data)

    def fill(self, timeout):
        if self.eof or self.closed:
            return False
        r, _, _ = select.select([self.s], [], [], max(timeout, 0))
        if not r:
            return False
        try:
            d = self.s.recv(1 << 20)
        except (ConnectionResetError, OSError):
            d = b""
        if not d:
            self.eof = True
            return False
        self.buf += d
        return True

    def read_until(self, pred, timeout):
        deadline = time.time() + timeout
        while not pred(self.buf):
            left_t = deadline - time.time()
            if left_t <= 0 or self.eof:
                return False
            self.fill(left_t)
        return True

    def expect(self, text, timeout=3.0, what=None):
        exp = text.encode() if isinstance(text, str) else text
        self.read_until(lambda b: len(b) >= len(exp), timeout)
        got = bytes(self.buf[:len(exp)])
        if got != exp:
            extra = " (接続が切られている)" if self.eof else ""
            raise Fail("%s の受信内容が期待と違う%s\n%s" % (what or self.name(), extra, diff(exp, bytes(self.buf))))
        del self.buf[:len(exp)]

    def drain(self, quiet=0.2, limit=10.0):
        deadline = time.time() + limit
        while time.time() < deadline and self.fill(quiet):
            pass
        data = bytes(self.buf)
        self.buf.clear()
        return data

    def is_open(self):
        if self.eof:
            return False
        self.s.setblocking(False)
        try:
            d = self.s.recv(1, socket.MSG_PEEK)
            return d != b""
        except BlockingIOError:
            return True
        except OSError:
            return False
        finally:
            self.s.settimeout(15)

    def close(self):
        if not self.closed:
            self.closed = True
            self.s.close()

    def reset(self):
        """RST で切断 (いきなり切れたクライアント)"""
        if not self.closed:
            self.s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.close()


class Reader(threading.Thread):
    """別スレッドで受信し続ける (大量データ用)。chunk_sleep を入れると「遅い」クライアントになる"""

    def __init__(self, client, chunk=1 << 20, chunk_sleep=0.0):
        super().__init__(daemon=True)
        self.c = client
        self.chunk = chunk
        self.chunk_sleep = chunk_sleep
        self.data = bytearray()
        self.lock = threading.Lock()
        self.stopped = False
        self.eof = False
        self.start()

    def run(self):
        s = self.c.s
        while not self.stopped:
            try:
                r, _, _ = select.select([s], [], [], 0.05)
                if not r:
                    continue
                d = s.recv(self.chunk)
            except (OSError, ValueError):
                break
            if not d:
                self.eof = True
                break
            with self.lock:
                self.data += d
            if self.chunk_sleep:
                time.sleep(self.chunk_sleep)

    def size(self):
        with self.lock:
            return len(self.data)

    def wait_len(self, n, timeout):
        deadline = time.time() + timeout
        while self.size() < n:
            if time.time() > deadline or self.eof:
                return False
            time.sleep(0.01)
        return True

    def wait_for(self, needle, timeout):
        deadline = time.time() + timeout
        while True:
            with self.lock:
                if needle in self.data:
                    return True
            if time.time() > deadline or self.eof:
                return False
            time.sleep(0.01)

    def snapshot(self):
        with self.lock:
            return bytes(self.data)

    def stop(self):
        self.stopped = True


def send_async(client, data, chunk=None, rng=None):
    """sendall を別スレッドで (サーバーが詰まってもテスト本体は止まらない)"""
    if isinstance(data, str):
        data = data.encode()
    err = []

    def run():
        try:
            if chunk is None:
                client.s.sendall(data)
            else:
                i = 0
                while i < len(data):
                    n = rng.randint(1, chunk) if rng else chunk
                    client.s.sendall(data[i:i + n])
                    i += n
        except OSError as e:
            err.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.err = err
    t.start()
    return t


class Ctx:
    def __init__(self, binary, args):
        self.bin = binary
        self.args = args
        self.servers = []
        self.clients = []
        self.readers = []
        self.allow_death = False
        self.allow_output = False

    def server(self, wrap=None, start_timeout=10.0):
        srv = Server(self.bin, wrap, start_timeout)
        self.servers.append(srv)
        return srv

    def join(self, srv, *watchers, **kw):
        """接続して id を割り当てる。watchers 全員に arrival が届くことを確認する"""
        c = Client(srv.port, srv.next_id, **kw)
        srv.next_id += 1
        self.clients.append(c)
        for w in watchers:
            w.expect(arrived(c.id), what="%s (client %d の arrival)" % (w.name(), c.id))
        return c

    def reader(self, client, **kw):
        r = Reader(client, **kw)
        self.readers.append(r)
        return r

    def cleanup(self):
        issues = []
        for r in self.readers:
            r.stop()
        for srv in self.servers:
            died, out, err = srv.stop()
            if died and not self.allow_death:
                issues.append("サーバーがテスト中に落ちていた (%s)" % srv.death_reason())
            if (out or err) and not self.allow_output:
                issues.append("サーバーが stdout/stderr に何か出力している: stdout=%r stderr=%r"
                              % (out[:200], err[:300]))
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass
        return issues


def leave_one_by_one(clients, observer, use_reset=False):
    """1 人ずつ切断し、その都度 observer に left が届くのを待つ (同時切断のレースを起こさない)"""
    for i, c in enumerate(clients):
        c.reset() if (use_reset and i % 2) else c.close()
        if isinstance(observer, Reader):
            check(observer.wait_for(left(c.id).encode(), 5), "client %d の left が届かない" % c.id)
        else:
            observer.read_until(lambda b: left(c.id).encode() in b, 5)
            check(left(c.id).encode() in observer.buf, "client %d の left が届かない" % c.id)


def no_extra(clients, wait=0.25):
    time.sleep(wait)
    for c in clients:
        if c.closed:
            continue
        while c.fill(0):
            pass
        if c.buf:
            raise Fail("%s が余計なデータを受信している: %r" % (c.name(), bytes(c.buf[:200])))


TESTS = []


def test(name, desc, level="FAIL"):
    def deco(f):
        TESTS.append((name, desc, level, f))
        return f
    return deco


# ---------------------------------------------------------------- 起動・引数

@test("args", "引数なし / 引数が多い → stderr に 'Wrong number of arguments\\n' で exit 1")
def t_args(ctx):
    for argv in ([], ["8080", "extra"]):
        p = subprocess.run([ctx.bin] + argv, capture_output=True, timeout=5)
        check(p.stderr == b"Wrong number of arguments\n", "引数 %r: stderr = %r" % (argv, p.stderr))
        check(p.returncode == 1, "引数 %r: exit code = %d" % (argv, p.returncode))
        check(p.stdout == b"", "引数 %r: stdout に出力がある %r" % (argv, p.stdout))


@test("bind_fail", "使用中のポートを渡す → stderr に 'Fatal error\\n' で exit 1")
def t_bind_fail(ctx):
    blocker = socket.socket()
    blocker.bind((HOST, 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        p = subprocess.run([ctx.bin, str(port)], capture_output=True, timeout=5)
    except subprocess.TimeoutExpired:
        raise Fail("bind が失敗するはずなのに終了しない")
    finally:
        blocker.close()
    check(p.stderr == b"Fatal error\n", "stderr = %r" % p.stderr)
    check(p.returncode == 1, "exit code = %d" % p.returncode)


@test("loopback_only", "127.0.0.1 以外のアドレスからは接続できない")
def t_loopback(ctx):
    ip = local_ip()
    if not ip:
        raise Skip("ループバック以外の IPv4 アドレスが見つからない")
    srv = ctx.server()
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect((ip, srv.port))
    except OSError:
        return
    finally:
        s.close()
    raise Fail("%s:%d に接続できてしまう (INADDR_ANY で bind している?)" % (ip, srv.port))


# ---------------------------------------------------------------- 基本動作

@test("arrival", "arrival 通知は既存クライアントだけに届き本人には届かない / id は 0 からの連番")
def t_arrival(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    no_extra([a])
    b = ctx.join(srv, a)
    c = ctx.join(srv, a, b)
    d = ctx.join(srv, a, b, c)
    no_extra([a, b, c, d])


@test("message", "1 行のメッセージが送信者以外の全員に 'client %d: ' 付きで届く")
def t_message(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    b = ctx.join(srv, a)
    c = ctx.join(srv, a, b)
    a.send("hello\n")
    b.expect(line(0, "hello"))
    c.expect(line(0, "hello"))
    c.send("  spaces  and\ttab  \n")
    a.expect(line(2, "  spaces  and\ttab  "))
    b.expect(line(2, "  spaces  and\ttab  "))
    no_extra([a, b, c])


@test("multiline", "1 回の send に複数行・空行が入っていても各行に prefix が付く")
def t_multiline(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    b = ctx.join(srv, a)
    a.send("first\nsecond\n\nthird\n")
    b.expect(line(0, "first") + line(0, "second") + line(0, "") + line(0, "third"))
    a.send("\n\n\n")
    b.expect(line(0, "") * 3)
    no_extra([a, b])


@test("split", "1 行が複数回の send に分かれて届いても正しく 1 行になる")
def t_split(ctx):
    srv = ctx.server()
    a = ctx.join(srv, nodelay=True)
    b = ctx.join(srv, a)
    for part in ("hel", "lo\nwor", "ld\n", "abc", "def", "\nxyz\n"):
        a.send(part)
        time.sleep(0.1)
    b.expect(line(0, "hello") + line(0, "world") + line(0, "abcdef") + line(0, "xyz"))
    no_extra([a, b])


@test("byte_by_byte", "1 byte ずつ送られても正しく 1 行になる")
def t_byte(ctx):
    srv = ctx.server()
    a = ctx.join(srv, nodelay=True)
    b = ctx.join(srv, a)
    for ch in "one byte at a time\nsecond\n":
        a.send(ch)
        time.sleep(0.003)
    b.expect(line(0, "one byte at a time") + line(0, "second"))
    no_extra([a, b])


@test("leave", "切断で 'server: client %d just left' が残り全員に届く / id は再利用しない")
def t_leave(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    b = ctx.join(srv, a)
    c = ctx.join(srv, a, b)
    b.close()
    a.expect(left(1))
    c.expect(left(1))
    d = ctx.join(srv, a, c)
    check(d.id == 3, "内部エラー")
    d.send("after\n")
    a.expect(line(3, "after"))
    c.expect(line(3, "after"))
    a.close()
    c.expect(left(0))
    d.expect(left(0))
    no_extra([c, d])
    srv.assert_alive()


@test("reset", "RST でいきなり切断されても left 通知が届き、サーバーが生きている")
def t_reset(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    b = ctx.join(srv, a)
    c = ctx.join(srv, a, b)
    b.reset()
    a.expect(left(1))
    c.expect(left(1))
    srv.assert_alive()
    a.send("still\n")
    c.expect(line(0, "still"))
    no_extra([a, c])


@test("partial_then_leave", "改行なしの途中行を残して切断しても left 通知が届く")
def t_partial_leave(ctx):
    srv = ctx.server()
    a = ctx.join(srv, nodelay=True)
    b = ctx.join(srv, a)
    a.send("no newline at end")
    time.sleep(0.15)
    a.close()
    exp = left(0).encode()
    ok = b.read_until(lambda buf: buf.endswith(exp), 3)
    got = bytes(b.buf)
    b.buf.clear()
    allowed = (exp, b"client 0: no newline at end" + exp)
    check(ok and got in allowed, "受信内容: %r\n  期待 (どちらか): %r" % (got, allowed))
    srv.assert_alive()
    c = ctx.join(srv, b)
    b.send("ok\n")
    c.expect(line(1, "ok"))


@test("partial_isolation", "改行待ちの途中行に、別クライアントの行が割り込まない", level="WARN")
def t_partial_iso(ctx):
    srv = ctx.server()
    a = ctx.join(srv, nodelay=True)
    b = ctx.join(srv, a)
    c = ctx.join(srv, a, b)
    a.send("abc")
    time.sleep(0.15)
    b.send("x\n")
    time.sleep(0.15)
    a.send("def\n")
    try:
        c.expect(line(1, "x") + line(0, "abcdef"))
    except Fail as e:
        raise Warn("%s\n  (改行が来る前に途中までを転送しているため、他人の行と混ざって見える)" % e)


# ---------------------------------------------------------------- 大きなデータ

@test("boundary", "recv サイズ境界付近 (999〜1002, 4095〜4097, 65535 byte) の行が欠けない")
def t_boundary(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    b = ctx.join(srv, a)
    rb = ctx.reader(b)
    lens = (1, 998, 999, 1000, 1001, 1002, 4094, 4095, 4096, 4097, 65535, 1)
    texts = [("%d:" % n + "a" * n)[:n] for n in lens]
    send_async(a, "".join(t + "\n" for t in texts))
    exp = "".join(line(0, t) for t in texts).encode()
    rb.wait_len(len(exp), 10)
    got = rb.snapshot()
    check(got == exp, "境界サイズの行が壊れた\n" + diff(exp, got))


@test("big_line", "1MB の 1 行が欠けずに全員に届く")
def t_big_line(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    b = ctx.join(srv, a)
    c = ctx.join(srv, a, b)
    rb, rc = ctx.reader(b), ctx.reader(c)
    text = "".join(chr(ord("A") + i % 26) for i in range(1000000))
    send_async(a, text + "\n")
    exp = line(0, text).encode()
    for r, who in ((rb, "client 1"), (rc, "client 2")):
        r.wait_len(len(exp), 30)
        got = r.snapshot()
        check(got == exp, "%s: 1MB の行が正しく届かない\n%s" % (who, diff(exp, got)))
    srv.assert_alive()


@test("flood", "2 万行を一気に送っても全行が順番通り・欠けなく届く")
def t_flood(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    b = ctx.join(srv, a)
    c = ctx.join(srv, a, b)
    rb, rc = ctx.reader(b), ctx.reader(c)
    texts = ["msg-%05d-%s" % (i, "q" * (i * 13 % 90)) for i in range(20000)]
    send_async(a, "".join(t + "\n" for t in texts), chunk=3000, rng=random.Random(1))
    exp = "".join(line(0, t) for t in texts).encode()
    for r, who in ((rb, "client 1"), (rc, "client 2")):
        r.wait_len(len(exp), 30)
        got = r.snapshot()
        check(got == exp, "%s: 大量の行が正しく届かない\n%s" % (who, diff(exp, got)))


@test("interleave", "5 人が同時に大量送信しても行が混ざらず、送信者ごとの順序が保たれる")
def t_interleave(ctx):
    k, per = 5, 400
    srv = ctx.server()
    cs = []
    for _ in range(k):
        cs.append(ctx.join(srv, *cs))
    readers = [ctx.reader(c) for c in cs]
    texts = {c.id: ["s%d-%05d-%s" % (c.id, j, "w" * (j * 7 % 120)) for j in range(per)] for c in cs}
    for c in cs:
        send_async(c, "".join(t + "\n" for t in texts[c.id]), chunk=700, rng=random.Random(c.id))
    pat = re.compile(r"^client (\d+): s(\d+)-(\d+)-w*$")
    for c, r in zip(cs, readers):
        total = sum(len(line(o.id, t)) for o in cs if o is not c for t in texts[o.id])
        r.wait_len(total, 30)
        got = r.snapshot().decode(errors="replace")
        check(len(got) == total, "client %d: 受信量が違う (期待 %d / 実際 %d byte)" % (c.id, total, len(got)))
        seqs = {}
        for ln in got.split("\n")[:-1]:
            m = pat.match(ln)
            check(m is not None, "client %d: 壊れた行を受信: %r" % (c.id, ln[:120]))
            check(m.group(1) == m.group(2), "client %d: prefix の id と中身が食い違う行: %r" % (c.id, ln[:120]))
            sid = int(m.group(1))
            check(sid != c.id, "client %d: 自分の送った行が返ってきた" % c.id)
            seqs.setdefault(sid, []).append(int(m.group(3)))
        for o in cs:
            if o is not c:
                check(seqs.get(o.id) == list(range(per)),
                      "client %d: client %d からの行の順序/個数がおかしい" % (c.id, o.id))


# ---------------------------------------------------------------- 遅い / 読まないクライアント

@test("lazy", "全く読まないクライアント (10MB 溜まる) がいても他への配送が止まらない", level="WARN")
def t_lazy(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    lazy = ctx.join(srv, a, rcvbuf=4096)
    b = ctx.join(srv, a)
    rb = ctx.reader(b)
    texts = ["%06d" % i + "x" * 993 for i in range(10000)]   # 10MB
    send_async(a, "".join(t + "\n" for t in texts))
    exp = "".join(line(0, t) for t in texts).encode()
    if not rb.wait_len(len(exp), 30):
        srv.assert_alive()
        raise Fail("読まないクライアントがいる状態で配送が止まった (client 2 は %d / %d byte しか受信できていない)。\n"
                   "  読まないクライアントへの send でサーバー全体がブロックしている。\n"
                   "  write_set で送信先を絞れば止まらないが、今度は遅いクライアントでメッセージが欠ける (slow_reader)。\n"
                   "  本番の合格コードもここは通らない (本番の 9 ケースにこの状況は無いと思われる)"
                   % (rb.size(), len(exp)))
    got = rb.snapshot()
    check(got == exp, "client 2 の受信内容が違う\n" + diff(exp, got))
    srv.assert_alive()
    check(lazy.is_open(), "読まないクライアントが切断された")
    lazy.drain(quiet=0.5, limit=20)
    a.send("ping\n")
    lazy.expect(line(0, "ping"), what="読み始めた lazy クライアント")
    check(rb.wait_len(len(exp) + len(line(0, "ping")), 3), "client 2 に ping が届かない")


@test("slow_reader", "読むのが遅いクライアントにもメッセージが欠けずに届く")
def t_slow(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    slow = ctx.join(srv, a, rcvbuf=8192)
    b = ctx.join(srv, a)
    rs = ctx.reader(slow, chunk=8192, chunk_sleep=0.002)
    rb = ctx.reader(b)
    texts = ["%05d" % i + "y" * 494 for i in range(6000)]    # 3MB
    send_async(a, "".join(t + "\n" for t in texts))
    exp = "".join(line(0, t) for t in texts).encode()
    exp_slow = arrived(2).encode() + exp     # slow (client 1) は client 2 の arrival も受け取る
    rb.wait_len(len(exp), 30)
    rs.wait_len(len(exp_slow), 30)
    gb, gs = rb.snapshot(), rs.snapshot()
    check(gb == exp, "速いクライアントの受信内容が違う\n" + diff(exp, gb))
    if gs != exp_slow:
        lost = len(exp_slow) - len(gs)
        raise Fail("遅いクライアントでメッセージが欠けた (%d byte 不足)。\n"
                   "  select の write_set に入っていない fd への送信を捨てている可能性\n%s" % (lost, diff(exp_slow, gs)))


# ---------------------------------------------------------------- 送信の速さ

@test("latency", "2 人が交互に送り合っても各行が遅延なく 1 回で届く (本番 test 6 と同じ形)")
def t_latency(ctx):
    srv = ctx.server()
    a = ctx.join(srv)
    b = ctx.join(srv, a)
    times, split = [], 0
    msgs = ["", "", "hello %d", "", "multi %d-a\nmulti %d-b", "%d"]
    for i in range(120):
        for snd, rcv in ((a, b), (b, a)):
            text = msgs[i % len(msgs)].replace("%d", str(i))
            exp = "".join(line(snd.id, t) for t in text.split("\n")).encode()
            t0 = time.perf_counter()
            snd.send(text + "\n")
            rcv.fill(2)
            if len(rcv.buf) < len(exp):
                split += 1
            rcv.expect(exp, timeout=2)
            times.append(time.perf_counter() - t0)
    times.sort()
    med, p90, mx = times[len(times) // 2], times[int(len(times) * 0.9)], times[-1]
    summary = ("中央値 %.1fms / p90 %.1fms / 最大 %.1fms / 1 回の recv で揃わなかった回数 %d/%d"
               % (med * 1e3, p90 * 1e3, mx * 1e3, split, len(times)))
    check(med < 0.01 and p90 < 0.02,
          "メッセージの到着が遅い: %s\n"
          "  'client %%d: ' と本文を別々に send すると、本文が Nagle + 遅延 ACK で約 40ms 待たされる (Linux)。\n"
          "  本番 test 6 (空行の送り合い → client 0 退出) で最後の行を取りこぼす原因になる。1 行 = 1 回の send にする" % summary)
    if split > len(times) // 10:
        raise Warn("遅延は小さいが分割されて届くことが多い: %s" % summary)


# ---------------------------------------------------------------- 大量接続・耐久

@test("many_clients", "大量接続 (--clients 人) の arrival / broadcast / 順番に切断")
def t_many(ctx):
    n = ctx.args.clients
    srv = ctx.server()
    base = fd_count(srv.pid)
    obs = ctx.join(srv)
    cs = [obs]
    for _ in range(1, n):
        cs.append(ctx.join(srv, obs))
    for k, c in enumerate(cs[1:], 1):
        c.expect("".join(arrived(j) for j in range(k + 1, n)), timeout=10)
    talker = cs[n // 2]
    talker.send("hello everyone\n")
    for c in cs:
        if c is not talker:
            c.expect(line(talker.id, "hello everyone"))
    no_extra(cs, wait=0.3)
    for c in cs[1:]:
        c.close()
        obs.expect(left(c.id), timeout=5)
    srv.assert_alive()
    if base is not None:
        time.sleep(0.3)
        now = fd_count(srv.pid)
        check(now == base + 1, "全員切断後の fd 数 %d (期待 %d) → close 漏れ" % (now, base + 1))


@test("churn", "接続→切断を 300 回繰り返しても id と通知が正しい")
def t_churn(ctx):
    srv = ctx.server()
    base = fd_count(srv.pid)
    obs = ctx.join(srv)
    ro = ctx.reader(obs)
    rounds, batch = 300, 6
    expected_bytes = 0
    i = 1
    while i <= rounds:
        group = []
        for _ in range(batch):
            if i > rounds:
                break
            group.append(Client(srv.port, i))
            srv.next_id += 1
            expected_bytes += len(arrived(i)) + len(left(i))
            i += 1
        for c in group:
            ro.wait_for(arrived(c.id).encode(), 5)
        for j, c in enumerate(group):
            if j % 3 == 0:
                c.send("bye from %d\n" % c.id)
                expected_bytes += len(line(c.id, "bye from %d" % c.id))
                ro.wait_for(line(c.id, "bye from %d" % c.id).encode(), 5)
            c.reset() if j % 2 else c.close()
            ro.wait_for(left(c.id).encode(), 5)
    ro.wait_len(expected_bytes, 20)
    time.sleep(0.2)
    got = ro.snapshot().decode(errors="replace")
    check(len(got) == expected_bytes, "受信量が違う (期待 %d / 実際 %d byte)" % (expected_bytes, len(got)))
    arr, lft = [], {}
    for idx, ln in enumerate(got.split("\n")[:-1]):
        m = re.match(r"^server: client (\d+) just (arrived|left)$", ln)
        if m:
            cid = int(m.group(1))
            if m.group(2) == "arrived":
                arr.append((cid, idx))
            else:
                check(cid not in lft, "client %d の left が 2 回届いた" % cid)
                lft[cid] = idx
        else:
            check(re.match(r"^client (\d+): bye from \1$", ln) is not None, "想定外の行: %r" % ln)
    check([c for c, _ in arr] == list(range(1, rounds + 1)), "arrival の id が連番になっていない")
    for cid, idx in arr:
        check(cid in lft and lft[cid] > idx, "client %d の left が無い/順番がおかしい" % cid)
    srv.assert_alive()
    if base is not None:
        now = fd_count(srv.pid)
        check(now == base + 1, "churn 後の fd 数 %d (期待 %d) → close 漏れ" % (now, base + 1))


@test("mass_disconnect", "多数のクライアントが同時に切断しても全員分の left が届き、サーバーが落ちない")
def t_mass(ctx):
    srv = ctx.server()
    obs = ctx.join(srv)
    ro = ctx.reader(obs)
    for rnd in range(5):
        cs = []
        for _ in range(30):
            c = Client(srv.port, srv.next_id)
            srv.next_id += 1
            ctx.clients.append(c)
            cs.append(c)
            check(ro.wait_for(arrived(c.id).encode(), 5), "client %d の arrival が届かない" % c.id)
        for i, c in enumerate(cs):
            c.reset() if (rnd % 2 and i % 2) else c.close()
        for c in cs:
            if not ro.wait_for(left(c.id).encode(), 5):
                srv.assert_alive(" (同時切断の処理中)")
                raise Fail("client %d の left が届かない" % c.id)
    srv.assert_alive()
    obs.send("still alive\n")
    time.sleep(0.2)
    srv.assert_alive()


@test("sigpipe", "切断直後のクライアントへ送信しても SIGPIPE で落ちない", level="WARN")
def t_sigpipe(ctx):
    srv = ctx.server()
    s = ctx.join(srv)
    o = ctx.join(srv, s)
    ro = ctx.reader(o)
    rs = ctx.reader(s)
    for r in range(40):
        v = Client(srv.port, srv.next_id)
        srv.next_id += 1
        ctx.clients.append(v)
        ro.wait_for(arrived(v.id).encode(), 5)
        v.reset() if r % 2 else v.close()
        try:
            s.send("".join("spam %d-%d\n" % (r, k) for k in range(60)))
        except OSError:
            break
        if not srv.alive():
            break
    time.sleep(0.5)
    if not srv.alive():
        ctx.allow_death = True
        raise Warn("切断済みクライアントへの send でサーバーが落ちた (%s)。\n"
                   "  切断を検知する前に send すると SIGPIPE が飛ぶ。採点で起きるかは不明" % srv.death_reason())


# ---------------------------------------------------------------- リーク

def leak_scenario(ctx, srv, timeout):
    obs = ctx.join(srv)
    cs = []
    for _ in range(12):
        cs.append(ctx.join(srv))
        obs.read_until(lambda b: arrived(cs[-1].id).encode() in b, timeout)
    for i, c in enumerate(cs):
        c.send("line one\nline two\n" + ("partial-%d" % i) * (1 + i * 300))
    time.sleep(0.5)
    for i, c in enumerate(cs[:6]):
        c.send(" done\n")
    time.sleep(0.5)
    for i, c in enumerate(cs):
        c.reset() if i % 2 else c.close()
        obs.read_until(lambda b: left(c.id).encode() in b, timeout)
    obs.close()
    time.sleep(0.5)


@test("mem_leak", "切断したクライアントのバッファを free している (Linux: valgrind / macOS: leaks)")
def t_mem(ctx):
    if sys.platform.startswith("linux"):
        if not shutil.which("valgrind"):
            raise Skip("valgrind が無い")
        log = tempfile.mktemp(prefix="vg_", suffix=".log")
        srv = ctx.server(wrap=["valgrind", "--leak-check=full", "--show-leak-kinds=all",
                               "--log-file=" + log], start_timeout=30)
        leak_scenario(ctx, srv, 15)
        srv.stop()
        with open(log) as f:
            rep = f.read()
        os.unlink(log)
        bad = re.findall(r"(Invalid (?:read|write|free)[^\n]*|Conditional jump[^\n]*|Mismatched free[^\n]*)", rep)
        check(not bad, "valgrind がメモリエラーを検出: %s" % bad[:3])
        m = re.search(r"definitely lost: ([\d,]+) bytes", rep)
        m2 = re.search(r"indirectly lost: ([\d,]+) bytes", rep)
        lost = (int(m.group(1).replace(",", "")) if m else 0) + (int(m2.group(1).replace(",", "")) if m2 else 0)
        check(lost == 0, "valgrind: %d byte リーク\n%s" % (lost, rep[-1500:]))
        m3 = re.search(r"still reachable: ([\d,]+) bytes", rep)
        reach = int(m3.group(1).replace(",", "")) if m3 else 0
        if reach > 4096:
            raise Warn("全員切断後も %d byte 確保されたまま (still reachable)" % reach)
    elif sys.platform == "darwin":
        if not shutil.which("leaks"):
            raise Skip("leaks コマンドが無い")
        srv = ctx.server()
        leak_scenario(ctx, srv, 5)
        p = subprocess.run(["leaks", str(srv.pid)], capture_output=True, text=True, timeout=120)
        m = re.search(r"(\d+) leaks? for (\d+) total leaked bytes", p.stdout)
        if not m:
            raise Skip("leaks がプロセスを調べられない (macOS の制限)。Linux/Docker の valgrind で確認すること")
        check(m.group(1) == "0", "leaks: %s leaks / %s byte\n%s" % (m.group(1), m.group(2), p.stdout[-1500:]))
    else:
        raise Skip("未対応の OS")


@test("fd_leak", "切断したクライアントの fd を close している")
def t_fd(ctx):
    srv = ctx.server()
    base = fd_count(srv.pid)
    if base is None:
        raise Skip("fd 数を数えられない (lsof も /proc も無い)")
    obs = ctx.join(srv)
    for rnd in range(3):
        cs = [ctx.join(srv, obs) for _ in range(40)]
        for i, c in enumerate(cs):
            if i % 4 == 0:
                c.send("partial without newline")
        time.sleep(0.1)
        leave_one_by_one(cs, obs, use_reset=True)
        obs.buf.clear()
    time.sleep(0.3)
    now = fd_count(srv.pid)
    check(now == base + 1, "fd 数 %d (期待 %d) → close 漏れ" % (now, base + 1))


# ---------------------------------------------------------------- 静的チェック

def static_checks(src, binary, results):
    with open(src, errors="replace") as f:
        code = f.read()
    m = re.search(r"^\s*#\s*define\b.*$", code, re.M)
    results.append(("no_define", "#define を使っていない",
                    "FAIL" if m else "PASS", m.group(0).strip() if m else ""))
    p = subprocess.run(["nm", "-u", binary], capture_output=True, text=True)
    if p.returncode != 0:
        results.append(("functions", "許可された関数だけを使っている", "SKIP", "nm が使えない"))
        return
    used = set()
    for l in p.stdout.splitlines():
        sym = l.split()[-1] if l.split() else ""
        if sys.platform == "darwin" and sym.startswith("_"):
            sym = sym[1:]
        sym = re.split(r"[@$]", sym)[0]
        if sym:
            used.add(sym)
    bad = sorted(s for s in used if s not in ALLOWED and s not in COMPILER_SYMS)
    results.append(("functions", "許可された関数だけを使っている",
                    "FAIL" if bad else "PASS", ("禁止関数: " + ", ".join(bad)) if bad else ""))


def build(src, out, extra=()):
    cc = os.environ.get("CC", "gcc")
    cmd = [cc, "-Wall", "-Wextra", "-Werror"] + list(extra) + [src, "-o", out]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode == 0, " ".join(cmd) + "\n" + p.stdout + p.stderr


def raise_nofile(need):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = need if hard == resource.RLIM_INFINITY else min(need, hard)
    if soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError):
            pass
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


def main():
    ap = argparse.ArgumentParser(description="mini_serv 厳しめテスター")
    ap.add_argument("src", nargs="?", help="mini_serv.c のパス")
    ap.add_argument("-k", help="実行するテスト名 (カンマ区切り、部分一致)")
    ap.add_argument("--clients", type=int, default=1000, help="many_clients の人数 (既定 1000)")
    ap.add_argument("--asan", action="store_true", help="-fsanitize=address,undefined でビルドして実行")
    ap.add_argument("--list", action="store_true", help="テスト一覧を表示")
    args = ap.parse_args()

    if args.list:
        for name, desc, level, _ in TESTS:
            print("%-20s %-5s %s" % (name, level, desc))
        return 0
    if not args.src:
        ap.error("mini_serv.c のパスを指定してください")

    soft = raise_nofile(4096)
    # select は fd < FD_SETSIZE(1024) まで。サーバー側は 0,1,2,listen + クライアント
    args.clients = max(2, min(args.clients, 1015, soft // 2 - 20))

    work = tempfile.mkdtemp(prefix="mini_serv_test_")
    binary = os.path.join(work, "mini_serv")
    results = []
    ok, log = build(args.src, binary)
    results.append(("build", "%s -Wall -Wextra -Werror でビルドできる" % os.environ.get("CC", "gcc"),
                    "PASS" if ok else "FAIL", "" if ok else log))
    if not ok:
        print_one(*results[0])
        return report(results)
    static_checks(args.src, binary, results)
    for r in results:
        print_one(*r)
    if args.asan:
        binary = os.path.join(work, "mini_serv_asan")
        os.environ["ASAN_OPTIONS"] = "detect_leaks=0"
        ok, log = build(args.src, binary, ["-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer"])
        if not ok:
            results.append(("build_asan", "ASan ビルド", "FAIL", log))
            print_one(*results[-1])
            return report(results)

    only = [k.strip() for k in args.k.split(",")] if args.k else None
    for name, desc, level, fn in TESTS:
        if only and not any(k in name for k in only):
            continue
        if args.asan and name == "mem_leak":
            continue
        if USE_COLOR:
            print(color("RUN ", "2") + name, end="", flush=True)
        ctx = Ctx(binary, args)
        status, msg = "PASS", ""
        t0 = time.time()
        try:
            fn(ctx)
        except Fail as e:
            status, msg = level, str(e)
        except Warn as e:
            status, msg = "WARN", str(e)
        except Skip as e:
            status, msg = "SKIP", str(e)
        except Exception as e:
            status, msg = level, "テスト中に例外 %s: %s" % (type(e).__name__, e)
        finally:
            issues = ctx.cleanup()
        if issues:
            if status in ("PASS", "SKIP"):
                status = level
            msg = (msg + "\n" if msg else "") + "\n".join(issues)
        if USE_COLOR:
            print("\r" + " " * (len(name) + 4) + "\r", end="")
        results.append((name, desc, status, msg))
        print_one(name, desc, status, msg, time.time() - t0)
    shutil.rmtree(work, ignore_errors=True)
    return report(results)


COLORS = {"PASS": "32", "FAIL": "31;1", "WARN": "33;1", "SKIP": "36"}


def print_one(name, desc, status, msg, dt=None):
    t = " (%.1fs)" % dt if dt is not None else ""
    print("[%s] %-19s %s%s" % (color(status, COLORS[status]), name, desc, color(t, "2")))
    if msg and status != "PASS":
        for l in msg.splitlines():
            print("        " + l)


def report(results):
    counts = {k: sum(1 for r in results if r[2] == k) for k in COLORS}
    print("\n" + "  ".join(color("%s %d" % (k, v), COLORS[k]) for k, v in counts.items()))
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
