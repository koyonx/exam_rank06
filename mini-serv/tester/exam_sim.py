#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本番の採点 (./tester <exe> generatedN.test <seed> | sort) を真似たシミュレーター

公開されている本番トレースから分かっている形式:
  - 各クライアントが受信した行を "received <接続番号>: <行>" として出力し、sort して模範解答と diff
  - test 5: 2 人。client 0 が空行を 1 つ送って退出
  - test 6: 2 人。お互いに空行 ("\\n") を 20〜30 個ずつ送り合い、その後 client 0 が退出
  - test 7: 2 人。空行と 100 文字程度の行を混ぜて 25 行ずつ (TCP 上で分割される)
  - test 8: 3 人。100KB 程度の 1 行
操作の間隔 (--gap) は本番の値が分からないので、いくつか変えて取りこぼし率を見る。

使い方:
  python3 exam_sim.py ../complete/mini_serv.c                    # 全シナリオ × 既定の間隔
  python3 exam_sim.py ../complete/mini_serv.c -s test6 -n 500     # test6 だけ 500 回
  python3 exam_sim.py a.c b.c c.c -s test6 --gap 0,0.0005,0.002   # 複数の実装を比較
"""
import argparse
import os
import random
import select
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tester import Server, build, raise_nofile  # noqa: E402


def scenario_test5(rng):
    return 2, [("send", 0, "\n"), ("close", 0, None)]


def scenario_test6(rng):
    n = rng.randint(20, 30)
    acts = []
    for _ in range(n):
        acts.append(("send", 0, "\n"))
        acts.append(("send", 1, "\n"))
    acts.append(("close", 0, None))
    return 2, acts


def scenario_test6_burst(rng):
    """全部の空行を 1 回の send でまとめて送る版"""
    n = rng.randint(20, 30)
    return 2, [("send", 0, "\n" * n), ("send", 1, "\n" * n), ("close", 0, None)]


def scenario_test7(rng):
    acts = []
    for who in (0, 1):
        text = ""
        for _ in range(25):
            if rng.random() < 0.4:
                text += "\n"
            else:
                text += "".join(rng.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
                                for _ in range(rng.randint(80, 120))) + "\n"
        i = 0
        while i < len(text):
            k = rng.randint(1, 60)
            acts.append(("send", who, text[i:i + k]))
            i += k
    per = {0: [a for a in acts if a[1] == 0], 1: [a for a in acts if a[1] == 1]}
    merged = []
    while per[0] or per[1]:
        who = rng.choice([w for w in (0, 1) if per[w]])
        merged.append(per[who].pop(0))
    merged.append(("close", 0, None))
    return 2, merged


def scenario_test8(rng):
    text = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(100000)) + "\n"
    return 3, [("send", 1, text), ("close", 1, None)]


SCENARIOS = {
    "test5": scenario_test5,
    "test6": scenario_test6,
    "test6_burst": scenario_test6_burst,
    "test7": scenario_test7,
    "test8": scenario_test8,
}


def expected_lines(nclients, acts):
    """遅延ゼロの理想的なサーバーなら各接続が受け取る行"""
    out = []
    alive = set()
    bufs = {i: "" for i in range(nclients)}
    for i in range(nclients):
        for j in alive:
            out.append("received %d: server: client %d just arrived" % (j + 1, i))
        alive.add(i)
    for kind, who, data in acts:
        if kind == "send":
            bufs[who] += data
            while "\n" in bufs[who]:
                ln, bufs[who] = bufs[who].split("\n", 1)
                for j in alive:
                    if j != who:
                        out.append("received %d: client %d: %s" % (j + 1, who, ln))
        else:
            alive.discard(who)
            for j in alive:
                out.append("received %d: server: client %d just left" % (j + 1, who))
    return sorted(out)


def run_once(binary, scen, seed, gap, tail):
    rng = random.Random(seed)
    nclients, acts = SCENARIOS[scen](rng)
    srv = Server(binary)
    socks, bufs, got = [], [], []
    try:
        for i in range(nclients):
            s = socket.create_connection(("127.0.0.1", srv.port))
            socks.append(s)
            bufs.append(b"")
            time.sleep(0.01)   # arrival の順番を確定させる

        def pump(until):
            while True:
                left_t = until - time.time()
                live = [s for s in socks if s is not None]
                r, _, _ = select.select(live, [], [], max(left_t, 0))
                for s in r:
                    i = socks.index(s)
                    try:
                        d = s.recv(65536)
                    except OSError:
                        d = b""
                    if not d:
                        socks[i] = None
                        continue
                    bufs[i] += d
                    while b"\n" in bufs[i]:
                        ln, bufs[i] = bufs[i].split(b"\n", 1)
                        got.append("received %d: %s" % (i + 1, ln.decode(errors="replace")))
                if left_t <= 0:
                    return

        pump(time.time() + 0.05)
        for kind, who, data in acts:
            if kind == "send":
                socks[who].sendall(data.encode())
            else:
                socks[who].close()
                socks[who] = None
            pump(time.time() + gap)
        pump(time.time() + tail)
        alive = srv.alive()
        reason = None if alive else srv.death_reason()
    finally:
        for s in socks:
            if s is not None:
                s.close()
        srv.stop()
    exp = expected_lines(nclients, acts)
    got.sort()
    return got == exp, exp, got, reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="+")
    ap.add_argument("-s", "--scenario", default=",".join(SCENARIOS))
    ap.add_argument("-n", type=int, default=200, help="1 条件あたりの試行回数")
    ap.add_argument("--gap", default="0,0.0005,0.002", help="操作の間隔 (秒, カンマ区切り)")
    ap.add_argument("--tail", type=float, default=0.15, help="最後の操作のあと受信を待つ秒数")
    ap.add_argument("-v", action="store_true", help="失敗した diff を 1 件表示")
    args = ap.parse_args()
    raise_nofile(4096)
    gaps = [float(g) for g in args.gap.split(",")]
    scens = args.scenario.split(",")
    work = tempfile.mkdtemp(prefix="exam_sim_")
    for src in args.src:
        binary = os.path.join(work, os.path.basename(src) + ".bin")
        ok, log = build(src, binary)
        if not ok:
            print("%s: ビルド失敗\n%s" % (src, log))
            continue
        print("== %s" % src)
        for scen in scens:
            for gap in gaps:
                fails, crashes, shown = 0, 0, False
                for k in range(args.n):
                    ok, exp, got, reason = run_once(binary, scen, k, gap, args.tail)
                    if not ok:
                        fails += 1
                        if reason:
                            crashes += 1
                        if args.v and not shown:
                            shown = True
                            missing = [l for l in exp if l not in got or exp.count(l) > got.count(l)]
                            extra = [l for l in got if l not in exp or got.count(l) > exp.count(l)]
                            print("    例 seed=%d: 足りない %r / 余計 %r %s"
                                  % (k, sorted(set(missing))[:4], sorted(set(extra))[:4], reason or ""))
                print("  %-12s gap=%-7s 失敗 %3d/%d%s" % (scen, gap, fails, args.n,
                                                          " (うちサーバー死亡 %d)" % crashes if crashes else ""))
                sys.stdout.flush()


if __name__ == "__main__":
    main()
