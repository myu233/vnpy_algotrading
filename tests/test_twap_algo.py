"""
TwapAlgo 单元测试

重点验证「小手数不空转」修复：当 volume*interval/time 经 round_to 后
小于 min_volume 时，每片量应保底为 min_volume，确保能成交；
总量守恒由 on_timer 的 min(order_volume, left_volume) 保证。

测试策略：
  用 MagicMock 构造 algo_engine，桩住：
    - get_contract → 返回带 min_volume 的 ContractData
    - get_tick → 返回满足方向价格条件的 TickData
    - send_order → 捕获 buy/sell 的下单量
  直接构造 TwapAlgo 实例，模拟 start() + 多次 update_timer()，
  断言最终 traded 与下单序列。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vnpy.trader.constant import Direction, Offset, Exchange, OrderType, Product
from vnpy.trader.object import TickData, ContractData
from vnpy_algotrading.base import AlgoStatus
from vnpy_algotrading.algos.twap_algo import TwapAlgo


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------

def make_contract(min_volume: float = 1.0, pricetick: float = 1.0) -> ContractData:
    """构造带 min_volume 的合约"""
    return ContractData(
        gateway_name="CTP",
        symbol="rb2610",
        exchange=Exchange.SHFE,
        name="rb2610",
        product=Product.FUTURES,
        size=1,
        pricetick=pricetick,
        min_volume=min_volume,
    )


def make_tick(last_price: float, ask_price_1: float, bid_price_1: float) -> TickData:
    return TickData(
        gateway_name="CTP",
        symbol="rb2610",
        exchange=Exchange.SHFE,
        datetime=None,
        name="rb2610",
        last_price=last_price,
        ask_price_1=ask_price_1,
        bid_price_1=bid_price_1,
    )


def make_algo(
    volume: float,
    time: int = 120,
    interval: int = 20,
    min_volume: float = 1.0,
    direction: Direction = Direction.LONG,
    price: float = 3000.0,
    ask_price_1: float = 2990.0,   # 默认 ask < price，LONG 方向能满足
    bid_price_1: float = 3010.0,   # 默认 bid > price，SHORT 方向能满足
) -> tuple[TwapAlgo, MagicMock]:
    """构造 TwapAlgo 实例 + 捕获 send_order 的 mock engine。

    返回 (algo, send_order_mock)，send_order_mock.call_args_list 可查每次下单量。
    """
    algo_engine = MagicMock()
    algo_engine.get_contract.return_value = make_contract(min_volume=min_volume)
    algo_engine.get_tick.return_value = make_tick(price, ask_price_1, bid_price_1)
    algo_engine.send_order.return_value = "CTP.1"  # 返回假 vt_orderid
    algo_engine.write_log = MagicMock()
    algo_engine.put_algo_event = MagicMock()

    algo = TwapAlgo(
        algo_engine=algo_engine,
        algo_name="TwapAlgo_test",
        vt_symbol="rb2610.SHFE",
        direction=direction,
        offset=Offset.OPEN,
        price=price,
        volume=volume,
        setting={"time": time, "interval": interval},
    )
    return algo, algo_engine.send_order


def run_algo_to_completion(algo: TwapAlgo) -> list[float]:
    """启动 algo 并驱动 on_timer 直到结束，返回每次实际下单量序列。

    模拟「下单即全部成交」：每次 send_order 后立即触发 update_trade，
    使 traded 累加，从而驱动 on_trade 的 finish 判定。
    """
    algo.start()
    order_volumes: list[float] = []

    def tracking_send(*args, **kwargs):
        # AlgoTemplate.buy/sell 调用 algo_engine.send_order(self, Direction, price, volume, ...)
        # volume 在位置参数 args[3]
        volume = args[3] if len(args) >= 4 else kwargs.get("volume", 0)
        if volume > 0:
            order_volumes.append(volume)
            # 模拟立即成交：构造 TradeData 触发 update_trade 累加 traded
            from vnpy.trader.object import TradeData
            trade = TradeData(
                gateway_name="CTP",
                symbol="rb2610",
                exchange=Exchange.SHFE,
                orderid="1",
                tradeid=f"t{len(order_volumes)}",
                direction=args[1] if len(args) >= 2 else Direction.LONG,
                offset=Offset.OPEN,
                price=algo.price,
                volume=volume,
                datetime=None,
            )
            algo.update_trade(trade)
        return f"CTP.{len(order_volumes)}"

    algo.algo_engine.send_order = tracking_send

    # 驱动定时器直到 finish（status != RUNNING）
    max_ticks = algo.time + 10  # 安全上限
    for _ in range(max_ticks):
        if algo.status != AlgoStatus.RUNNING:
            break
        algo.update_timer()

    return order_volumes


# ---------------------------------------------------------------------------
# order_volume 计算（修复核心）
# ---------------------------------------------------------------------------

def test_order_volume_not_zero_for_small_volume_min1():
    """volume=1,time=120,interval=20,min=1：修复前归零，修复后应为 1"""
    algo, _ = make_algo(volume=1, time=120, interval=20, min_volume=1.0)
    assert algo.order_volume == 1.0, f"修复前应为 0，修复后应为 1，实际 {algo.order_volume}"


def test_order_volume_not_zero_for_volume2_min1():
    """volume=2,time=120,interval=20,min=1：修复前归零（0.333→0），修复后应为 1"""
    algo, _ = make_algo(volume=2, time=120, interval=20, min_volume=1.0)
    assert algo.order_volume == 1.0


def test_order_volume_normal_case():
    """volume=10,time=120,interval=20,min=1：正常场景 1.67→2"""
    algo, _ = make_algo(volume=10, time=120, interval=20, min_volume=1.0)
    assert algo.order_volume == 2.0


def test_order_volume_respects_min_volume_5():
    """min_volume=5 时，order_volume 应是 5 的倍数且不小于 5"""
    # volume=3,time=600,interval=60,min=5：raw=0.3→0，保底为 5
    algo, _ = make_algo(volume=3, time=600, interval=60, min_volume=5.0)
    assert algo.order_volume == 5.0


def test_order_volume_fractional_min_volume():
    """min_volume=0.1（股票类），小手数也不应归零"""
    # volume=1,time=120,interval=20,min=0.1：raw=0.167→0.2（round_to 0.1）
    algo, _ = make_algo(volume=1, time=120, interval=20, min_volume=0.1)
    assert algo.order_volume == 0.2  # round_to(0.167, 0.1) = 0.2


# ---------------------------------------------------------------------------
# 端到端：小手数能成交且不超量
# ---------------------------------------------------------------------------

def test_small_volume_actually_trades():
    """volume=1 小单：修复前空转（traded=0），修复后应成交 1 手"""
    algo, _ = make_algo(volume=1, time=120, interval=20, min_volume=1.0)
    orders = run_algo_to_completion(algo)
    assert algo.traded == 1.0, f"应成交 1 手，实际 {algo.traded}"
    assert sum(orders) == 1.0


def test_small_volume_does_not_exceed_total():
    """volume=2 小单：成交不超量"""
    algo, _ = make_algo(volume=2, time=120, interval=20, min_volume=1.0)
    orders = run_algo_to_completion(algo)
    assert algo.traded <= 2.0, f"不应超量，实际 traded={algo.traded}"
    assert algo.traded == 2.0, f"应成交 2 手，实际 {algo.traded}"


def test_normal_volume_trades_correctly():
    """volume=5 正常单：分 5 片各 1 手"""
    algo, _ = make_algo(volume=5, time=120, interval=20, min_volume=1.0)
    orders = run_algo_to_completion(algo)
    assert algo.traded == 5.0
    assert all(v == 1.0 for v in orders), f"每片应为 1，实际 {orders}"


def test_large_volume_trades_correctly():
    """volume=10 大单：每片 2 手（round_to），总量守恒"""
    algo, _ = make_algo(volume=10, time=120, interval=20, min_volume=1.0)
    orders = run_algo_to_completion(algo)
    assert algo.traded <= 10.0  # 不超量
    assert algo.traded == 10.0  # 应足额成交


def test_short_direction_trades():
    """SHORT 方向小单也能成交"""
    algo, _ = make_algo(
        volume=1, time=120, interval=20, min_volume=1.0,
        direction=Direction.SHORT,
        bid_price_1=3010.0,  # bid > price，SHORT 能成交
    )
    orders = run_algo_to_completion(algo)
    assert algo.traded == 1.0


# ---------------------------------------------------------------------------
# 价格条件未满足时不下单（原有行为不破坏）
# ---------------------------------------------------------------------------

def test_no_order_when_price_unfavorable():
    """ask > price 时 LONG 方向不下单（价格条件未满足）"""
    algo, _ = make_algo(
        volume=10, time=120, interval=20, min_volume=1.0,
        direction=Direction.LONG,
        price=3000.0,
        ask_price_1=3100.0,  # ask > price，不满足
    )
    orders = run_algo_to_completion(algo)
    # 价格不满足，一笔都不下
    assert algo.traded == 0
    assert orders == []


def test_algo_finishes_after_time():
    """time 到期后算法应 FINISHED"""
    algo, _ = make_algo(volume=100, time=30, interval=5, min_volume=1.0)
    run_algo_to_completion(algo)
    assert algo.status == AlgoStatus.FINISHED


def test_algo_finishes_when_traded_reaches_volume():
    """成交达到 volume 时应 FINISHED"""
    algo, _ = make_algo(volume=3, time=120, interval=20, min_volume=1.0)
    run_algo_to_completion(algo)
    assert algo.status == AlgoStatus.FINISHED
    assert algo.traded == 3.0


# ---------------------------------------------------------------------------
# 回归：确认大单正常分片（不破坏现有行为）
# ---------------------------------------------------------------------------

def test_large_volume_slice_count():
    """volume=20,time=120,interval=20,min=1：每片 round_to(3.33,1)=3

    注：原版 TwapAlgo 在 total_count >= time 时立即 finish 且本片不下单，
    故 5 片 × 3 = 15（最后一片因时间到期被跳过，剩余 5 手不下单）。
    这是原版的固有行为（非本次「小手数归零」修复范围），
    本测试只验证 order_volume 计算正确且分片大小符合预期。
    """
    algo, _ = make_algo(volume=20, time=120, interval=20, min_volume=1.0)
    # order_volume = 20/(120/20) = 3.33 → round_to(1) = 3
    assert algo.order_volume == 3.0
    orders = run_algo_to_completion(algo)
    # 每片都是 3 手（order_volume 的正确性）
    assert all(v == 3.0 for v in orders), f"每片应为 3，实际 {orders}"
    # 不超量
    assert algo.traded <= 20.0
