cooldown_sec	int	2	同一サイドの連続発注を抑制するクールダウン秒数
funding_buffer_sec	int	90	Funding 支払い時刻の N 秒前から発注をブロック
max_equity_ratio	float	1.0	使用証拠金 ÷ 総資産 が上限を超えると発注しない
python run_bot.py pfpl --order_usd 50 --testnet --dry-run
