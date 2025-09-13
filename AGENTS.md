# AGENTS.md — HYPERLIQUID (Cloud Codex 用)

## セットアップ
- Python: pyproject.tomlの指定に従う（なければ 3.11）
- 依存: `poetry install --no-interaction --no-ansi`
  （Poetry未使用なら `pip install -r requirements.txt`）

## 検証コマンド
- テスト: `pytest -q`
- 静的検査: `ruff . --fix` / `black .` （存在する設定を優先）
- 型（任意）: `mypy src` が通る範囲で

## ネットワーク/安全
- **基本はネットワークOFFで作業**（外部APIはモックに置換）
- 実売買を禁止。`run_bot.py` 実行時は **DRY_RUN=1**（デフォルト）で
- 資格情報は一切追加しない。必要なら環境変数名だけをREADMEに追記

## 実装ポリシー
- 仕様互換を最優先（破壊的変更は別PR提案）
- 失敗テストの**最小修正**→必要なら**最小の回帰テスト**を追加
- 変更理由・影響範囲・ロールバック手順をPRに要約

## 成功条件
- `pytest -q` が緑
- フォーマッタ/リンタ準拠（既存設定を尊重）
