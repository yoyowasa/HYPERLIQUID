# 〔このモジュールがすること〕
# VRLG の発注まわり（post-only Iceberg、TTL、OCO、IOC解消、クールダウン）を司ります。
# 実際の取引所 API 呼び出しは hl_core.api.http のプレースホルダに委譲し、未実装でも落ちないようにします。

from __future__ import annotations

import asyncio
import time

from typing import Optional, Callable, Dict, Any  # 〔この行がすること〕 オーダーイベント用のコールバック型を使えるようにする


from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.exec")


def _safe(cfg, section: str, key: str, default):
    """〔この関数がすること〕 設定オブジェクト/辞書の両対応で値を安全に取得します。"""
    try:
        sec = getattr(cfg, section)
        return getattr(sec, key, default)
    except Exception:
        try:
            return cfg[section].get(key, default)  # type: ignore[index]
        except Exception:
            return default


def _round_to_tick(x: float, tick: float) -> float:
    """〔この関数がすること〕 価格をティックサイズに丸めます。"""
    if tick <= 0:
        return x
    return round(x / tick) * tick


class ExecutionEngine:
    """〔このクラスがすること〕
    - post-only Iceberg 指値をミッド±0.5tick に同時提示
    - TTL 経過でキャンセル
    - 充足後は IOC で即解消（Time-Stop/OCOは後続差し込み）
    - フィル後の同方向クールダウン（2×R* 秒）を管理
    """


    on_order_event: Optional[Callable[[str, Dict[str, Any]], None]]

    _open_maker_btc: float
    _order_size: dict[str, float]

    def __init__(self, cfg, paper: bool) -> None:
        """〔このメソッドがすること〕 コンフィグを読み込み、発注パラメータと内部状態を初期化します。"""
        self.paper = paper
        self.symbol: str = _safe(cfg, "symbol", "name", "BTCUSD-PERP")
        self.tick: float = float(_safe(cfg, "symbol", "tick_size", 0.5))
        self.ttl_ms: int = int(_safe(cfg, "exec", "order_ttl_ms", 1000))
        self.display_ratio: float = float(_safe(cfg, "exec", "display_ratio", 0.25))
        self.min_display: float = float(_safe(cfg, "exec", "min_display_btc", 0.01))
        self.max_exposure: float = float(_safe(cfg, "exec", "max_exposure_btc", 0.8))
        self.cooldown_factor: float = float(_safe(cfg, "exec", "cooldown_factor", 2.0))
        self.side_mode: str = str(_safe(cfg, "exec", "side_mode", "both")).lower()  # 〔この行がすること〕 片面/両面モード設定を保持
        self.splits: int = int(_safe(cfg, "exec", "splits", 1))  # 〔この行がすること〕 1クリップを何分割で出すか（片面あたりの子注文本数）
        # 〔この行がすること〕 通常置きのオフセットを保持
        self.offset_ticks_normal: float = float(_safe(cfg, "exec", "offset_ticks_normal", 0.5))
        # 〔この行がすること〕 深置きのオフセットを保持
        self.offset_ticks_deep: float = float(_safe(cfg, "exec", "offset_ticks_deep", 1.5))

        # 内部状態
        self._last_fill_side: Optional[str] = None  # "BUY" or "SELL"
        self._last_fill_time: float = 0.0
        self._period_s: float = 1.0  # RotationDetector から更新注入予定

        self.on_order_event: Optional[Callable[[str, Dict[str, Any]], None]] = None  # 〔この行がすること〕 'skip'/'submitted'/'reject'/'cancel' を Strategy 側へ通知するコールバック
        self.trace_id: Optional[str] = None  # 〔この行がすること〕 Strategy から注入される相関IDを保持します

        self._open_maker_btc: float = 0.0  # 〔この属性がすること〕 未キャンセルの maker 注文サイズ合計（BTC）を管理
        self._order_size: Dict[str, float] = {}  # 〔この属性がすること〕 order_id → 発注 total サイズの対応

    def set_period_hint(self, period_s: float) -> None:
        """〔このメソッドがすること〕 R*（推定周期）ヒントを注入し、クールダウン計算に使います。"""
        if period_s > 0:
            self._period_s = float(period_s)

    async def place_two_sided(self, mid: float, total: float, deepen: bool = False) -> list[str]:
        """〔このメソッドがすること〕
        ミッド±offset_ticks×tick に post-only のアイスバーグ指値を出します。
        - side_mode に応じて BUY / SELL / 両面 を選択
        - splits>1 のとき、片面あたり splits 本の「子注文」を出します
          child_total = total / splits
          child_display = min(max(child_total*display_ratio, min_display), child_total)
        - クールダウンや露出上限で片側や子注文をスキップします
        """
        offset_ticks = self.offset_ticks_deep if deepen else self.offset_ticks_normal
        px_bid = _round_to_tick(mid - offset_ticks * self.tick, self.tick)
        px_ask = _round_to_tick(mid + offset_ticks * self.tick, self.tick)

        if total <= 0.0:
            return []

        sides = [("BUY", px_bid), ("SELL", px_ask)]
        if getattr(self, "side_mode", "both") == "buy":
            sides = [("BUY", px_bid)]
        elif getattr(self, "side_mode", "both") == "sell":
            sides = [("SELL", px_ask)]

        splits = max(1, int(self.splits))
        child_total = float(total) / float(splits)

        ids: list[str] = []
        for side, price in sides:
            if self._in_cooldown(side):
                try:
                    if self.on_order_event:
                        self.on_order_event(
                            "skip",
                            {
                                "side": side,
                                "reason": "cooldown",
                                "open_maker_btc": float(self._open_maker_btc),
                                "trace_id": self.trace_id,
                            },
                        )
                except Exception:
                    pass
                continue

            for _ in range(splits):
                if (self._open_maker_btc + child_total) > self.max_exposure:
                    try:
                        if self.on_order_event:
                            self.on_order_event(
                                "skip",
                                {
                                    "side": side,
                                    "reason": "exposure",
                                    "open_maker_btc": float(self._open_maker_btc),
                                    "trace_id": self.trace_id,
                                },
                            )
                    except Exception:
                        pass
                    break

                child_display = min(
                    max(child_total * self.display_ratio, self.min_display),
                    child_total,
                )

                oid = await self._post_only_iceberg(side, price, child_total, child_display, self.ttl_ms / 1000.0)
                if oid:
                    self._open_maker_btc += float(child_total)
                    self._order_size[str(oid)] = float(child_total)
                    try:
                        if self.on_order_event:
                            self.on_order_event(
                                "submitted",
                                {
                                    "side": side,
                                    "price": float(price),
                                    "order_id": str(oid),
                                    "open_maker_btc": float(self._open_maker_btc),
                                    "trace_id": self.trace_id,
                                },
                            )
                    except Exception:
                        pass
                    ids.append(oid)
                else:
                    try:
                        if self.on_order_event:
                            self.on_order_event(
                                "reject",
                                {
                                    "side": side,
                                    "price": float(price),
                                    "open_maker_btc": float(self._open_maker_btc),
                                    "trace_id": self.trace_id,
                                },
                            )
                    except Exception:
                        pass
        return ids

    async def wait_fill_or_ttl(self, order_ids: list[str], timeout_s: float) -> None:
        """〔このメソッドがすること〕
        TTL またはフィル完了のどちらか早い方まで待機し、その後に未約定分をキャンセルします。
        実約定検知は後続（fills stream）で置き換え可能なように簡易に実装します。
        """
        if not order_ids:
            return
        try:
            await asyncio.sleep(timeout_s)
        finally:
            await self._cancel_many(order_ids)
            # 〔このブロックがすること〕 キャンセル完了後に、未約定メーカー露出を減算
            for _oid in order_ids:
                self._reduce_open_maker(_oid)
            # 〔このブロックがすること〕 TTL/解消でキャンセルした事実を片側ごとに通知（露出も併記）
            try:
                if self.on_order_event:
                    for _oid in order_ids:
                        self.on_order_event(
                            "cancel",
                            {
                                "order_id": str(_oid),
                                "open_maker_btc": float(self._open_maker_btc),
                                "trace_id": self.trace_id,
                            },
                        )
            except Exception:
                pass


    async def flatten_ioc(self) -> None:
        """〔このメソッドがすること〕 市場成行（IOC）で素早くフラット化します（スケルトン）。"""
        try:
            from hl_core.api.http import flatten_ioc  # type: ignore
        except Exception:
            logger.info("[paper=%s] IOC flatten (placeholder) executed.", self.paper)
            return
        try:
            await flatten_ioc(self.symbol)  # type: ignore[misc]
        except Exception as e:
            logger.warning("flatten_ioc failed: %s", e)

    async def place_reverse_stop(self, fill_side: str, ref_mid: float, stop_ticks: float) -> Optional[str]:
        """〔このメソッドがすること〕
        充足したポジションを守る「逆指値（STOP）」を 1 本だけ出します（OCOの片翼）。
        - fill_side="BUY" のとき: 防御は SELL STOP（ref_mid − stop_ticks×tick）
        - fill_side="SELL" のとき: 防御は BUY  STOP（ref_mid + stop_ticks×tick）
        - 取引所APIが未実装ならプレースホルダで安全にログだけ出し、疑似 order_id を返します。
        """
        side = str(fill_side).upper()
        if side not in ("BUY", "SELL"):
            logger.warning("place_reverse_stop: invalid side=%s", fill_side)
            return None
        stop_px = ref_mid - stop_ticks * self.tick if side == "BUY" else ref_mid + stop_ticks * self.tick
        stop_px = _round_to_tick(stop_px, self.tick)

        payload = {
            "symbol": self.symbol,
            "side": "SELL" if side == "BUY" else "BUY",
            "stop_price": stop_px,
            "size": min(self.max_exposure, 1.0),  # 後で実ポジションサイズに合わせて上書き想定
            "time_in_force": "GTC",
            "reduce_only": True,
            "type": "STOP",
            "paper": self.paper,
        }
        try:
            from hl_core.api.http import place_order  # type: ignore
        except Exception:
            logger.info("[paper=%s] place_reverse_stop placeholder: %s", self.paper, payload)
            return f"paper-stop-{payload['side']}-{int(time.time()*1000)}"

        try:
            resp = await place_order(**payload)  # type: ignore[misc]
            return str(resp.get("order_id", "")) or None  # type: ignore[union-attr]
        except Exception as e:
            logger.warning("place_reverse_stop failed: %s", e)
            return None

    async def cancel_order_safely(self, order_id: Optional[str]) -> None:
        """〔このメソッドがすること〕 単一注文のキャンセルを安全に実行します（存在しなくてもOK）。"""
        if not order_id:
            return
        try:
            from hl_core.api.http import cancel_order  # type: ignore
        except Exception:
            logger.info("[paper=%s] cancel placeholder: %s", self.paper, order_id)
            return
        try:
            await cancel_order(self.symbol, order_id)  # type: ignore[misc]
            # 〔この行がすること〕 手動キャンセルでも露出を減算
            self._reduce_open_maker(order_id)
            # 〔このブロックがすること〕 手動キャンセルの通知（露出も併記）
            try:
                if self.on_order_event:
                    self.on_order_event(
                        "cancel",
                        {
                            "order_id": str(order_id),
                            "open_maker_btc": float(self._open_maker_btc),
                            "trace_id": self.trace_id,
                        },
                    )
            except Exception:
                pass

        except Exception as e:
            logger.debug("cancel_order (safe) ignored: %s", e)

    async def time_stop_after(self, ms: int) -> None:
        """〔このメソッドがすること〕
        指定ミリ秒だけ待ってから **IOC で即時クローズ**します（Time‑Stop）。
        - 発注/充足状況に関わらず「時間で逃げる」最後の安全装置です。
        """
        try:
            await asyncio.sleep(max(0.0, float(ms) / 1000.0))
        except Exception:
            return
        await self.flatten_ioc()

    # ─────────────── 内部ユーティリティ（APIアダプタ呼び出し） ───────────────

    async def _post_only_iceberg(
        self, side: str, price: float, total: float, display: float, ttl_s: float
    ) -> Optional[str]:
        """〔このメソッドがすること〕 post-only のアイスバーグ指値を発注し、order_id を返します。"""
        payload = {
            "symbol": self.symbol,
            "side": side,
            "price": price,
            "size": total,
            "display_size": display,
            "time_in_force": "PO",   # Post Only
            "iceberg": True,
            "ttl_s": ttl_s,
            "paper": self.paper,
        }
        try:
            from hl_core.api.http import place_order  # type: ignore
        except Exception:
            logger.info("[paper=%s] place_order placeholder: %s", self.paper, payload)
            # 擬似 order_id（テスト用）
            return f"paper-{side}-{price}-{int(time.time()*1000)}"

        try:
            resp = await place_order(**payload)  # type: ignore[misc]
            oid = str(resp.get("order_id", ""))  # type: ignore[union-attr]
            return oid or None
        except Exception as e:
            logger.warning("place_order failed: %s", e)
            return None

    async def _cancel_many(self, order_ids: list[str]) -> None:
        """〔このメソッドがすること〕 複数注文をキャンセルします（存在しなくても安全）。"""
        if not order_ids:
            return
        try:
            from hl_core.api.http import cancel_order  # type: ignore
        except Exception:
            logger.info("[paper=%s] cancel placeholder: %s", self.paper, order_ids)
            return

        for oid in order_ids:
            try:
                await cancel_order(self.symbol, oid)  # type: ignore[misc]
            except Exception as e:
                logger.debug("cancel_order failed (ignored): %s", e)

    def _reduce_open_maker(self, order_id: str) -> None:
        """〔このメソッドがすること〕
        指定 order_id の発注サイズを台帳から引き当て、未約定メーカー露出を減算します。
        （同じ order_id に対しては一度だけ作用）
        """
        try:
            size = float(self._order_size.pop(order_id, 0.0))
        except Exception:
            size = 0.0
        if size > 0.0:
            self._open_maker_btc = max(0.0, self._open_maker_btc - size)

    # ─────────────── クールダウン管理（簡易） ───────────────

    def _in_cooldown(self, side: str) -> bool:
        """〔このメソッドがすること〕 直近フィルの同方向に対するクールダウン中かを返します。"""
        if self._last_fill_side != side:
            return False
        cool = self.cooldown_factor * max(self._period_s, 0.5)
        return (time.time() - self._last_fill_time) < cool

    def register_fill(self, side: str) -> None:
        """〔このメソッドがすること〕 フィル発生を記録し、クールダウンを開始します。"""
        self._last_fill_side = side
        self._last_fill_time = time.time()
