import argparse
from pathlib import Path

import pandas as pd


# --- metrics.py の load_csv と summary 関数を次のコードに置き換えてください ---


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # クローズ行だけ抽出（pnl_usd が空でない行）
    df = df[df["pnl_usd"].notna() & (df["pnl_usd"] != "")]
    df["pnl_usd"] = df["pnl_usd"].astype(float)
    return df


def summary(df: pd.DataFrame) -> None:
    total = len(df)
    wins = (df["pnl_usd"] > 0).sum()
    losses = total - wins

    buys = (df["side"].str.upper() == "BUY").sum()
    sells = total - buys

    print(f"trades         : {total}")
    if total:
        print(f" win / loss    : {wins} / {losses}  ({wins/total:.2%} win-rate)")
        print(f" BUY / SELL    : {buys} / {sells}")
        print(f" avg PnL (USD) : {df['pnl_usd'].mean():.4f}")
        print(f" total PnL     : {df['pnl_usd'].sum():.4f} USD")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("csv_dir", help="strategy_<SYMBOL>.csv のフォルダ")
    args = p.parse_args()

    csv_dir = Path(args.csv_dir)
    for csv_path in csv_dir.glob("strategy_*.csv"):
        print(f"\n=== {csv_path.stem} ===")
        summary(load_csv(csv_path))


if __name__ == "__main__":
    main()
