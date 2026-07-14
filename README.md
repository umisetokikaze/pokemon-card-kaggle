# Pokémon TCG AI Battle Challenge

このリポジトリは、Kaggle の「The Pokémon Company - PTCG AI Battle Challenge Simulation」へ提出するエージェントの作業環境である。

現在は、公式の Mega Lucario ex ルールベースエージェントを基準実装として展開している。

## 編集するファイル

- `main.py`：盤面と合法手を受け取り、選択する手のインデックスを返すエージェントである。
- `deck.csv`：カード ID を1行に1枚ずつ、合計60枚記述する。
- `cg/`：公式のカードゲーム API と Linux 用ネイティブライブラリである。
- `ptcg_policy/`：Mega Lucario 固有の攻撃・効果評価と、match / turn state を保持する純粋 Python package である。

`cg/libcg.so` は Linux 用の ELF 共有ライブラリであるため、ローカル対戦は WSL2 の Ubuntu 22.04 で実行する。

## ローカル環境の準備

PowerShell から次のコマンドを実行する。

```powershell
pwsh -File .\tools\setup_local.ps1
```

このスクリプトは、WSL 内の `~/.venvs/pokemon-card-kaggle` に Python 3.11 の仮想環境を作り、`cabt` を収録した `kaggle-environments==1.32.0` を導入する。

仮想環境を Linux 側へ置くのは、Windows 側へ置いた場合に多数の依存ファイルの読み込みが遅くなるためである。

大会ページに表示される `1.14.10` は PyPI に公開されていないため、2026年7月14日時点で公開済みの最新版を固定している。

## 自己対戦による確認

次のコマンドは、現在のデッキを両方のプレイヤーへ設定し、公式エージェントと合法手をランダムに選ぶエージェントの対戦を1試合実行する。

```powershell
pwsh -File .\tools\run_smoke_wsl.ps1
```

`ERROR` になった場合は、提出前に `main.py` の例外と返り値を修正する。

## 候補エージェントの比較

**paired arena** は、現在の champion と変更後の challenger を1組につき2局対戦させる評価コマンドである。

2局目では player 0 と player 1 の seat を交換する。

ただし、native engine が決める先攻は arena から指定できないため、seat の交換だけで先攻回数が均等になるとは限らない。

arena は各局の `firstPlayer` を読み取り、実際に先攻したエージェントを JSON に記録する。

候補を `experiments/candidate/` に置いた場合は、PowerShell から次のように実行する。

```powershell
pwsh -File .\tools\run_arena_wsl.ps1 `
  -Challenger experiments/candidate/main.py `
  -ChallengerDeck experiments/candidate/deck.csv `
  -Pairs 25 `
  -Matchup mega-lucario `
  -OutputPath artifacts/arena/candidate-vs-champion.json
```

`-Pairs 25` は50局を実行する。

arena は次の情報を保存する。

- 各局の seat、実際の先攻、勝敗、status、reward
- agent の例外、不正な action、ローカル timeout
- agent 呼出し時間の平均、最大値、p95、p99
- challenger 視点の win、loss、draw、win rate、Wilson 95% 信頼区間
- champion と challenger の両視点の集計
- agent と deck の SHA-256

`win_rate` の分母は fault を除く正常終了局であり、draw は非勝利として数える。

`score_rate` は draw を0.5勝として数え、`decisive_win_rate` は draw を分母から外す。

`wilson_95` は保守的な `win_rate` に対して計算する。

1回の agent 呼出しには、既定で1,000 msのローカル timeout を設定する。

Python が実行を続けている場合は、この timeout で遅い action を中断する。

C extension や停止した入出力は直ちに中断できない場合があるため、arena は各試合を独立した子 process で逐次実行する。

1試合が既定の900秒を超えた場合は子 process を終了し、`process_timeout` として記録する。

上限は `-GameTimeoutSeconds` で変更できる。

これらはローカル評価を停止させないための制限であり、本番の1試合10分という制限と同じものではない。

native engine の乱数は Python の `seed` から制御できないため、paired arena は同じ乱数系列を使う完全な paired test ではない。

50局は変更候補を絞る screening に使う。

昇格判定には200局以上を使い、Wilson 95% 下限が0.50を上回り、fault が0件であることを要求する。

arena が fault を検出した場合も JSON は保存されるが、PowerShell コマンドは失敗として終了する。

集計処理の unit test は、native engine を使わず Windows と WSL の両方で実行できる。

```powershell
python -m unittest -v tests.test_attack_semantics tests.test_policy_state tests.test_arena_core tests.test_arena_runner
```

## 提出物の作成

次のコマンドは、`main.py`、`deck.csv`、`cg/`、`ptcg_policy/` を `dist/submission.tar.gz` へ格納する。

```powershell
pwsh -File .\tools\build_submission.ps1
```

ビルド後は、アーカイブの階層、容量、60枚のデッキ、`agent` 関数を自動検査する。

生成された `dist/submission.tar.gz` を Kaggle の My Submissions から提出する。

## 開発の順序

最初の目標は、公式サンプルを変更せずに自己対戦と Kaggle の validation を通すことである。

その後は、対戦ログから判断ミスを分類し、`main.py` の評価関数と行動選択を一つずつ変更する。

デッキを変更するときは、`deck.csv` の60枚制約と大会指定カードの範囲を同時に確認する。

## 公式資料

- [大会ページ](https://www.kaggle.com/competitions/pokemon-tcg-ai-battle)
- [公式サンプル Notebook](https://www.kaggle.com/code/kiyotah/a-sample-rule-based-agent-mega-lucario-ex-deck)
- [cabt Engine Documentation](https://matsuoinstitute.github.io/cabt/)
