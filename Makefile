## HYPERLIQUID BOT Makefile
# function: BOT開発でよく使うコマンドを短縮。CIと同じ検証(verify)を一発で実行できる。

.PHONY: help setup verify test lint typecheck fix run-hello

help: ## function: よく使うターゲット名を表示
	@echo "make setup verify test lint typecheck fix run-hello"

setup: ## function: dev依存込みでインストール（ruff/pyright/pytest等）
	poetry install --with dev

verify: ## function: CIと同じ検証(テスト/リンタ/型)をまとめて実行
	poetry run pytest
	poetry run ruff check .
	poetry run pyright

test: ## function: ユニットテスト（pytest.iniのaddoptsでlive除外&静かに実行）
	poetry run pytest

lint: ## function: コード規約チェック
	poetry run ruff check .

typecheck: ## function: 型チェック
	poetry run pyright

fix: ## function: 自動整形（安全な範囲でruff fix）
	poetry run ruff --fix .

run-hello: ## function: Helloボットを実行（発注なし）
	poetry run python -m bots.hello.main


これで make verify だけ覚えれば、CIと同じ基準でローカル検証できます。
