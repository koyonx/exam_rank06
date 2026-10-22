# mini_serv テスター

| ファイル | 役割 |
|---|---|
| `tester.py` | 厳しめテスター。課題文の要件と、壊れやすいケース (大量データ・遅い/読まないクライアント・同時切断・SIGPIPE・リーク) を 1 項目ずつ判定する |
| `exam_sim.py` | 本番採点のシミュレーター。公開されている本番トレースから分かった形式を真似て、同じシナリオを何百回も回し、取りこぼし率を出す |

## 使い方

macOS でも動くが、本番は Linux (x86_64, Linux 5.15, `cc` = clang)。TCP の挙動が違うので、最終確認は Linux で行うこと。

```bash
python3 tester.py ../complete/mini_serv.c
```

```bash
python3 exam_sim.py ../complete/mini_serv.c -s test6 -n 200
```

Docker で Linux 上で実行する (alpine に gcc / clang / python3 / valgrind を入れる):

```bash
docker run --name miniserv-setup alpine:3.20 sh -c 'apk add --no-cache gcc clang musl-dev python3 valgrind lsof binutils' && docker commit miniserv-setup miniserv-alpine && docker rm miniserv-setup
```

```bash
docker run --rm -v "$PWD/..":/m -w /m/tester miniserv-alpine python3 tester.py ../complete/mini_serv.c
```

```bash
docker run --rm -v "$PWD/..":/m -w /m/tester miniserv-alpine python3 exam_sim.py ../complete/mini_serv.c -s test6 -n 200
```

主なオプション:

- `tester.py -k lazy,flood` 名前に一致するテストだけ実行 / `--list` で一覧 / `--asan` で ASan ビルド / `--clients N` で大量接続の人数
- `exam_sim.py -s test6,test7` シナリオ / `--gap 0.003,0.01` 操作の間隔 (秒) / `-n` 回数 / `-v` 失敗例を表示 / ファイルを複数渡すと実装の比較

## 本番採点について分かっていること

GitHub に公開されている本番のトレース (採点ログ) から読み取ったもの。公式の情報ではない。

- `cc -Wextra -Wall -Werror mini_serv.c fcntl.c -o user_exe` でビルド (`fcntl.c` の中身は不明)
- 模範解答のサーバーと同じ操作をして、出力を `sort` してから diff。9 ケース全部 OK で合格
  - test 1: 引数なし
  - test 2: ポート `22` (bind 失敗 → `Fatal error`)
  - test 3〜9: `./tester <exe> generatedN.test <seed> | sort`。出力は `received <接続番号>: <受信した行>`
- 各ケースの中身 (シードで変わるが種類は同じ):
  - test 5: 2 人。client 0 が空行を 1 つ送って退出
  - test 6: 2 人。お互いに空行 (`"\n"`) を 20〜30 個ずつ送り合い、その後 client 0 が退出
  - test 7: 2 人。空行と 100 文字前後の行を混ぜて 25 行ずつ (TCP 上で分割されて届く)
  - test 8: 3 人。100KB 前後の 1 行
- test 6 で落ちた実例:
  - 1 回の recv で読んだ塊の先頭にしか `client %d: ` を付けていない → 2 行目以降に prefix が無い
  - client 1 の空行のうち 2 行が client 0 に届いていない (取りこぼし/遅延)

出典:
- 合格トレースと提出コード: https://github.com/hersdani/Exam_Rank_06
- test 6 / 7 / 8 で落ちたトレース: https://github.com/hebaja/exam_05
- test 6 で行が足りなかったトレース: https://github.com/552020/exam_06/blob/main/docs/test_outputs/ana.md

`exam_sim.py` はこれを元にした推測の再現で、本物の `tester` の待ち時間などは分からない。
「間隔を詰めても取りこぼさない」実装ほど本番でも安全、という目安として使う。

## test 6 で落ちる原因 (検証結果)

`client %d: ` と本文を別々に `send()` していると、2 人が交互に送り合う状況 (= test 6) で、
後から送った本文が TCP の Nagle アルゴリズム + 遅延 ACK で **約 40ms** 待たされる (Linux)。
1 回の recv に複数行が入っていて、行ごとに `send()` している場合も同じことが起きる。
その間に採点側が client 0 を閉じると、最後の行が届かずに diff が合わない。

Linux (Docker) で test 6 と同じ形を計測した結果:

| 実装 | 1 行が届くまで (中央値) | `exam_sim.py` test6 失敗率 (操作間隔 3ms / 10ms / 30ms / 40ms) |
|---|---|---|
| 2 回 send (hersdani の合格コード、melswonder、元の precomplete) | 約 41ms | 98〜100% / 95〜100% / 35〜50% / 0〜3% |
| 1 行 = 1 回 send (今の `mini_serv.c`) | 0.2ms | 0% / 0% / 0〜5% / 0% |

本番トレースのタイムスタンプから、本番 tester の 1 操作の間隔はおよそ 40ms と推定できる
(test 6 の 1 回の実行が約 2.2 秒、操作は約 53 回)。2 回 send の遅れ (約 41ms) とほぼ同じなので、
2 回 send は「間に合うかどうか運次第」になる。実際に 2 回 send で合格した例も落ちた例もある。

test7 は操作間隔を 3ms / 10ms まで詰めると、1 回 send でも 2 回 send でも同じくらい落ちる
(連続した send が遅延 ACK を待つため。`setsockopt` が禁止なので避けられない)。40ms 間隔ではどれも 0%。
対策は **1 回の recv で取り出せた行をまとめて 1 回の `send()` で送る** こと (`../complete/mini_serv.c`)。

その他の注意点:

- `write_set` で送信先を絞ると、select が毎回すぐ返ってCPUを使い切る上、送信バッファが詰まったクライアント宛てのメッセージを黙って捨てる。本番の合格トレースのコードは `write_set` を使わず全員に送っている
- 切断済みのクライアントに `send()` すると SIGPIPE でサーバーごと落ちることがある。`send(..., MSG_NOSIGNAL)` で防げる (関数ではなくフラグなので許可関数のチェックには引っかからないはずだが、本番で使った実例は未確認。不安なら `0` に戻してもよい)
