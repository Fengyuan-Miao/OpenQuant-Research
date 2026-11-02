"""A股中期动量研究基线（小资金版）。

适合先用约1万元本金研究；本金在回测界面设置，不在策略中覆盖账户。
月初调仓；126日动量跳过最近21日、120日趋势过滤，最多3只、旧持仓前5保留。
总仓位上限80%、单股30%、已知行业50%；未知行业单独限80%，不代表行业分散。
按昨日实际价格规划100股整数手，预留2%价格余量与费用；通常忽略不足1000元、
不足账户5%或单边预计费率超过0.5%的调整。清仓和超限减仓不受小额过滤限制。

参数示例：{"topk": 3, "buffer": 5, "risk": 0.8, "stock_cap": 0.3}
历史状态模式仍为research/strict；未配置状态时不能核实历史ST和行业。
调仓计划是基于前一日价格的估算，费用沿用本次回测设置；不是实时订单或成交保证。
股票池由context.universe决定，未成交不自动补单，不接入券商、不筛查账户权限。
"""
import math

import numpy as np
import pandas as pd

from apps.quant_workbench.data import symbol


UNKNOWN_INDUSTRY = "__unknown__"


def initialize(context):
    p = context.params
    context.risk = float(p.get("risk", 0.80))
    assert math.isfinite(context.risk) and 0 < context.risk <= 1, "risk 必须在 (0, 1] 内"
    integer_defaults = {"lookback": 126, "skip": 21, "trend_window": 120,
                        "vol_window": 60, "liquidity_window": 20, "liquidity_topn": 800,
                        "min_history": 252, "topk": 3, "buffer": 5}
    for key, default in integer_defaults.items():
        value = p.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == "skip" else 1):
            raise ValueError(f"{key} 必须为{'非负' if key == 'skip' else '正'}整数")
        setattr(context, key, value)
    if context.vol_window < 2 or context.buffer < context.topk:
        raise ValueError("vol_window >= 2，buffer >= topk")
    for key, default in {"stock_cap": 0.30, "industry_cap": 0.50, "unknown_industry_cap": 0.80,
                         "vol_floor": 0.05, "max_trade_cost_ratio": 0.005}.items():
        value = float(p.get(key, default))
        if not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError(f"{key} 必须在 (0, 1] 内")
        setattr(context, key, value)
    for key, default in {"min_trade_value": 1000., "rebalance_band": 0.05, "price_buffer": 0.02}.items():
        value = float(p.get(key, default))
        if not math.isfinite(value) or value < 0 or (key != "min_trade_value" and value >= 1):
            raise ValueError(f"{key} 必须非负且有限，比例必须小于1")
        setattr(context, key, value)
    # Framework exposes the actual backtest fee assumptions; notebook contexts can set the same mapping.
    context.fees = dict(getattr(context, "trading_costs", {"open_cost": .0005, "close_cost": .0015, "min_cost": 5.}))
    context.rebalance_plans = []
    context.rebalance_plan = pd.DataFrame()
    context.metadata_policy = p.get("metadata_policy", "research")
    if context.metadata_policy not in ("research", "strict"):
        raise ValueError("metadata_policy 只能是 research 或 strict")
    context.metadata = prepare_metadata(p.get("security_metadata", {}))
    if context.metadata_policy == "strict" and not context.metadata:
        raise ValueError("严格模式需要 security_metadata 历史状态；不能用当前 ST/行业名单代替")
    context.window = max(context.min_history, context.lookback + context.skip + 1,
                         context.trend_window, context.vol_window + 1, context.liquidity_window)
    context.last_rebalance = None
    log.info("小资金动量基线：股票池%d只，最多%d只，模式%s，历史窗口%d；仅月初调仓，未成交不补单",
             len(context.universe), context.topk, context.metadata_policy, context.window)
    log.info("目标上限：总仓位%.0f%%、单股%.0f%%；整数手100股，最低调整%.0f元；费用估算%s",
             context.risk * 100, context.stock_cap * 100, context.min_trade_value, context.fees)
    if context.metadata_policy == "research":
        log.warning("研究模式：缺少历史状态的股票无法排除 ST/退市整理；未知行业单独限仓 %.1f%%，不能验证行业分散，结果不代表完整可投资股票池",
                    context.unknown_industry_cap * 100)
    if len(context.universe) < context.topk:
        log.warning("股票池少于 topk；单股上限仍生效，不会为凑满仓位而放大权重")


def prepare_metadata(mapping):
    """Each dated row is a complete state, not a partial patch; params are snapshotted."""
    if not isinstance(mapping, dict):
        raise ValueError("security_metadata 必须是 股票代码:历史状态列表 的字典")
    result = {}
    for code, rows in mapping.items():
        code = symbol(code)
        if code in result or not isinstance(rows, list):
            raise ValueError("历史状态代码重复或不是列表")
        parsed, seen = [], set()
        for row in rows:
            required = {"effective_date", "known_at", "is_st", "is_delisting", "industry"}
            if not isinstance(row, dict) or not required.issubset(row):
                raise ValueError("每条历史状态需要 effective_date、known_at、is_st、is_delisting、industry")
            if type(row["is_st"]) is not bool or type(row["is_delisting"]) is not bool:
                raise ValueError("is_st 和 is_delisting 必须为 JSON 布尔值")
            item = dict(row)
            for key in ("effective_date", "known_at"):
                item[key] = pd.Timestamp(row[key])
                if pd.isna(item[key]) or item[key].tzinfo is not None:
                    raise ValueError("历史状态日期应为不带时区的有效日期")
            pair = (item["effective_date"], item["known_at"])
            if pair in seen or not isinstance(item["industry"], str) or not item["industry"].strip():
                raise ValueError("历史状态日期重复或行业为空")
            if item["industry"] == UNKNOWN_INDUSTRY:
                raise ValueError("行业不能使用保留的未知行业标记")
            seen.add(pair)
            parsed.append(item)
        result[code] = sorted(parsed, key=lambda row: (row["effective_date"], row["known_at"]))
    return result


def metadata_asof(rows, cutoff):
    # Both effective and knowledge dates must be visible. Later corrections stay hidden.
    visible = [row for row in rows if row["effective_date"] <= cutoff and row["known_at"] <= cutoff]
    return visible[-1] if visible else None


def rank_candidates(context, prices, data, cutoff):
    """Eligibility → liquidity top N → trend and momentum → deterministic ranking."""
    pool = {}
    diagnostics = dict(历史不足数=0, 状态缺失数=0, 状态剔除数=0, 流动性入围数=0)
    if not prices.index.is_monotonic_increasing:
        prices = prices.sort_index()
    for code, group in prices.groupby(level="instrument", sort=True):
        # Require a full market-day window; never treat missing dates as adjacent returns.
        if (len(group) != context.window or
                group.index.get_level_values("datetime")[-1] != cutoff or
                code not in data or data[code].volume <= 0):
            diagnostics["历史不足数"] += 1
            continue
        close = group.close
        amount = group.amount.tail(context.liquidity_window)
        close_values, amount_values = close.to_numpy(), amount.to_numpy()
        if (not np.isfinite(close_values).all() or (close_values <= 0).any() or
                not np.isfinite(amount_values).all() or (amount_values < 0).any()):
            diagnostics["历史不足数"] += 1
            continue
        state = metadata_asof(context.metadata.get(code, []), cutoff)
        if state is None:
            diagnostics["状态缺失数"] += 1
            if context.metadata_policy == "strict":
                continue
        elif state["is_st"] or state["is_delisting"]:
            diagnostics["状态剔除数"] += 1
            continue
        liquidity = float(amount.mean())  # history.amount is already in yuan.
        if liquidity <= 0:
            continue
        pool[code] = (close, liquidity, state['industry'] if state else UNKNOWN_INDUSTRY)
    # Compute expensive factors only until the original liquidity quota is filled.
    # Non-finite volatility did not qualify in the old implementation either: skip it
    # and continue down the liquidity ranking, without backfilling failed trend signals.
    candidates, liquid = {}, []
    for code in sorted(pool, key=lambda c: (-pool[c][1], c)):
        close, liquidity, industry = pool[code]
        # t is yesterday: -22 is t-21; -148 is t-147 (126-day return, skipping 21 days).
        momentum = float(close.iloc[-context.skip - 1] / close.iloc[-context.skip - context.lookback - 1] - 1)
        volatility = float(close.tail(context.vol_window + 1).pct_change(fill_method=None).dropna().std(ddof=1) * math.sqrt(252))
        if not math.isfinite(volatility):
            continue
        candidates[code] = dict(price=float(close.iloc[-1]), momentum=momentum, volatility=max(volatility, context.vol_floor),
            liquidity=liquidity, trend=bool(close.iloc[-1] > close.tail(context.trend_window).mean()),
            industry=industry)
        liquid.append(code)
        if len(liquid) == context.liquidity_topn:
            break
    diagnostics["流动性入围数"] = len(liquid)
    eligible = {code: candidates[code] for code in liquid
                if candidates[code]["momentum"] > 0 and candidates[code]["trend"]}
    ranked = sorted(eligible, key=lambda code: (-eligible[code]["momentum"], code))
    return eligible, ranked, diagnostics


def select_with_buffer(ranked, held, topk, buffer):
    # Retain ACTUAL holdings, not the previous target list (orders may have failed).
    selected = [code for code in ranked[:buffer] if code in held][:topk]
    for code in ranked[:topk]:
        if len(selected) >= topk:
            break
        if code not in selected:
            selected.append(code)
    return selected


def allocate_weights(selected, candidates, risk, stock_cap, industry_cap, unknown_industry_cap=None):
    inverse = {code: 1 / candidates[code]["volatility"] for code in selected}
    denominator = sum(inverse.values())
    weights = {code: min(stock_cap, risk * value / denominator) for code, value in inverse.items()}
    industries = {}
    for code, weight in weights.items():
        industry = candidates[code]["industry"]
        industries[industry] = industries.get(industry, 0.0) + weight
    for code in weights:
        total = industries[candidates[code]["industry"]]
        cap = (unknown_industry_cap if candidates[code]["industry"] == UNKNOWN_INDUSTRY
               and unknown_industry_cap is not None else industry_cap)
        if total > cap:
            weights[code] *= cap / total
    # Capped amounts stay in cash; no redistribution or hidden leverage.
    return weights



def estimate_cost(context, value, buying):
    """与工作台一致的费用假设，不把固定费率误称为券商真实税费。"""
    if value <= 0:
        return 0.
    return max(context.fees['min_cost'], value * context.fees['open_cost' if buying else 'close_cost'])


def industry_cap(context, industry):
    return context.unknown_industry_cap if industry == UNKNOWN_INDUSTRY else context.industry_cap


def floor_lots(shares):
    # Ignore tiny float noise in the Qlib adjusted-to-actual share conversion.
    return max(0, math.floor((shares + 1e-3) / 100) * 100)


def select_affordable(context, candidates, ranked, held):
    """先排除单股预算内买不起的候选，再排序、保留旧仓；不足部分由后续候选递补。"""
    equity = context.portfolio.total_value
    skipped, affordable = {}, []
    for code in ranked:
        item = candidates[code]
        minimum = max(1, math.ceil(context.min_trade_value / (item['price'] * 100) - 1e-10)) * 100 * item['price']
        budget = equity * min(context.risk, context.stock_cap, industry_cap(context, item['industry']))
        if code not in held and minimum * (1 + context.price_buffer) + estimate_cost(context, minimum, True) > budget:
            skipped[code] = '单股/行业预算买不起一手或最低建仓金额'
        elif code not in held and estimate_cost(context, minimum, True) / minimum > context.max_trade_cost_ratio:
            skipped[code] = '建仓预计费用占比过高'
        else:
            affordable.append(code)
    # Inverse-volatility/industry caps may shrink an otherwise affordable position below a lot.
    # Remove one infeasible NEW entrant at a time; recompute weights before trying the next name.
    while True:
        selected = select_with_buffer(affordable, held, context.topk, context.buffer)
        weights = allocate_weights(selected, candidates, context.risk, context.stock_cap,
                                   context.industry_cap, context.unknown_industry_cap)
        bad = []
        for code in selected:
            value = floor_lots(weights[code] * equity / (1 + context.price_buffer) / candidates[code]['price']) * candidates[code]['price']
            if code not in held and (value <= 0 or value < context.min_trade_value or
                    estimate_cost(context, value, True) / value > context.max_trade_cost_ratio):
                bad.append(code)
        if not bad:
            return selected, weights, skipped
        code = bad[-1]
        affordable.remove(code)
        skipped[code] = '分配权重后不足一手或最低建仓金额/费用要求'


def plan_rebalance(context, data, candidates, ranked, selected, weights, skipped):
    """先规划卖单，再按剩余现金和仓位上限规划买单；返回可审阅的手工调仓清单。"""
    equity = context.portfolio.total_value
    positions = context.portfolio.positions
    current = {code: float(p.total_amount) for code, p in positions.items() if p.total_amount > 0}
    codes = set(current) | set(selected) | set(skipped)
    prices, industries = {}, {}
    for code in codes:
        if code in candidates:
            prices[code], industries[code] = candidates[code]['price'], candidates[code]['industry']
        else:
            # Even a suspended/ineligible holding must have an explicit exit plan.
            bar = data.get(code)
            prices[code] = float(bar.close if bar is not None else positions[code].price)
            state = metadata_asof(context.metadata.get(code, []), context.previous_dt)
            industries[code] = state['industry'] if state else UNKNOWN_INDUSTRY
    targets = dict(current)
    reasons = dict(skipped)
    fees = {}
    cash = float(context.portfolio.available_cash)
    reserve = equity * (1 - context.risk)
    # Keep the continuous target here: rounding the TOTAL position would spuriously sell
    # a lot when a dividend turns 200 shares into 204 shares. Round DELTAS below instead.
    desired = {code: weights[code] * equity / (1 + context.price_buffer) / prices[code]
               for code in selected}

    def stock_value(code):
        return targets.get(code, 0.) * prices[code]

    def sector_value(industry):
        return sum(stock_value(code) for code in targets if industries[code] == industry)

    def total_value():
        return sum(stock_value(code) for code in targets)

    def small_trade(value, buying):
        return (value < context.min_trade_value - 1e-6 or value / equity < context.rebalance_band - 1e-10
                or estimate_cost(context, value, buying) / value > context.max_trade_cost_ratio)

    def sell(code, shares, reason):
        nonlocal cash
        targets[code] -= shares
        # Multiple planning passes still produce ONE target order and one minimum fee.
        combined = (current[code] - targets[code]) * prices[code]
        new_fee = estimate_cost(context, combined * (1 + context.price_buffer), False)
        cash += shares * prices[code] * (1 - context.price_buffer) - (new_fee - fees.get(code, 0.))
        fees[code] = new_fee
        reasons[code] = reason

    # Exit orders and reductions that restore a breached cap bypass the small-trade filter.
    for code in sorted(current):
        held = current[code]
        goal = desired.get(code, 0.)
        if goal < 100 - 1e-3:
            goal = 0.  # A position whose allocated budget cannot support a lot is exited.
        if goal >= held - 1e-3:
            continue
        shares = held if goal <= 0 else min(held, floor_lots(held - goal))
        if shares <= 0:
            reasons[code] = '保留旧仓：变化不足一手'
            continue
        value = shares * prices[code]
        hard = (code not in selected or goal <= 0 or stock_value(code) > equity * context.stock_cap + .01
                or sector_value(industries[code]) > equity * industry_cap(context, industries[code]) + .01
                or total_value() > equity * context.risk + .01)
        if not hard and small_trade(value, False):
            reasons[code] = '保留旧仓：调整金额、幅度或费用未达门槛'
            continue
        sell(code, shares, '退出信号/排名或目标不足一手' if shares == held else ('超限减仓' if hard else '月度减仓'))

    # Relative lot rounding can leave a small excess over a hard cap. Reduce the
    # lowest-ranked positions until all caps hold, even when the reduction is small.
    rank = {code: i + 1 for i, code in enumerate(ranked)}
    for code in sorted(current, key=lambda c: (rank.get(c, len(rank) + 1), c), reverse=True):
        excess = max(stock_value(code) - equity * context.stock_cap,
                     sector_value(industries[code]) - equity * industry_cap(context, industries[code]),
                     total_value() - equity * context.risk)
        if excess > .01 and targets[code] > 0:
            shares = min(targets[code], math.ceil(excess / prices[code] / 100) * 100)
            sell(code, shares, '超限减仓（整数手校正）')

    for code in selected:
        held = targets.get(code, 0.)
        if held < current.get(code, 0.) - 1e-3:
            continue  # Keep a reduction as one net sell; never plan a same-day buyback.
        wanted = floor_lots(desired[code] - held)
        if wanted == 0:
            reasons.setdefault(code, '保持现有持仓')
            continue
        # Caps account for retained positions, including small reductions we deliberately skipped.
        budgets = [wanted * prices[code],
                   equity * context.stock_cap / (1 + context.price_buffer) - stock_value(code),
                   equity * industry_cap(context, industries[code]) / (1 + context.price_buffer) - sector_value(industries[code]),
                   equity * context.risk / (1 + context.price_buffer) - total_value()]
        available = max(0., cash - reserve)
        budgets.extend([(available - context.fees['min_cost']) / (1 + context.price_buffer),
                        available / ((1 + context.price_buffer) * (1 + context.fees['open_cost']))])
        shares = floor_lots(max(0., min(budgets)) / prices[code])
        value = shares * prices[code]
        if not shares or small_trade(value, True):
            reasons[code] = '跳过买入：整数手、剩余预算或小额/费用门槛'
            continue
        targets[code] = held + shares
        fees[code] = estimate_cost(context, value * (1 + context.price_buffer), True)
        cash -= value * (1 + context.price_buffer) + fees[code]
        reasons[code] = '新建仓' if current.get(code, 0.) == 0 else '月度加仓'

    rows = []
    for code in sorted(codes, key=lambda c: (rank.get(c, len(rank) + 1), c)):
        before, after = current.get(code, 0.), targets.get(code, 0.)
        delta = after - before
        action = '买入' if delta > 1e-3 else ('卖出' if delta < -1e-3 else ('持有' if after else '跳过'))
        rows.append(dict(datetime=context.current_dt, signal_date=context.previous_dt, instrument=code,
            rank=rank.get(code), action=action, reference_price=prices[code], current_shares=before,
            trade_shares=delta, target_shares=after, target_value=after * prices[code],
            target_weight=after * prices[code] / equity, estimated_cost=fees.get(code, 0.),
            industry=industries[code], reason=reasons.get(code, '不调整')))
    return rows, targets, cash


def handle_data(context, data):
    equity = context.portfolio.total_value
    held = {code for code, position in context.portfolio.positions.items() if position.total_amount > 0}
    record(决策前资产=equity, 实际持股数=len(held),
           实际股票仓位=sum(p.value for p in context.portfolio.positions.values()) / equity if equity > 0 else 0,
           月度调仓=0)
    current, previous = pd.Timestamp(context.current_dt), context.previous_dt
    if previous is None or pd.Timestamp(previous).to_period('M') == current.to_period('M'):
        return
    month = str(current.to_period('M'))
    if context.last_rebalance == month:
        return
    prices = history(context.window, ['close', 'amount'])
    candidates, ranked, counts = rank_candidates(context, prices, data, pd.Timestamp(previous))
    selected, weights, skipped = select_affordable(context, candidates, ranked, held)
    rows, targets, estimated_cash = plan_rebalance(context, data, candidates, ranked, selected, weights, skipped)
    context.rebalance_plan = pd.DataFrame(rows)
    context.rebalance_plans.extend(rows)
    # Queue sells before buys; Qlib also enforces this ordering during execution.
    orders = sorted((row for row in rows if row['action'] in ('买入', '卖出')),
                    key=lambda row: (row['action'] != '卖出', row['instrument']))
    for row in orders:
        order_target_value(row['instrument'], row['target_value'])
        log.info('调仓计划 %s %s %.4f股，昨价%.4f，目标%.4f股/%.2f元，预估费用%.2f；%s',
                 row['instrument'], row['action'], abs(row['trade_shares']), row['reference_price'],
                 row['target_shares'], row['target_value'], row['estimated_cost'], row['reason'])
    context.last_rebalance = month
    target_value = sum(row['target_value'] for row in rows)
    record(月度调仓=1, 合格候选数=len(ranked), 可交易候选数=len(ranked) - len(skipped),
           买不起或成本剔除数=len(skipped), 目标持股数=sum(value > 0 for value in targets.values()),
           目标股票仓位=target_value / equity, 预计剩余现金=estimated_cash,
           计划买入金额=sum(r['trade_shares'] * r['reference_price'] for r in orders if r['action'] == '买入'),
           计划卖出金额=sum(-r['trade_shares'] * r['reference_price'] for r in orders if r['action'] == '卖出'),
           预计交易费=sum(r['estimated_cost'] for r in rows),
           小额调整跳过数=sum('门槛' in r['reason'] for r in rows),
           入选平均动量=sum(candidates[c]['momentum'] for c in selected) / len(selected) if selected else 0,
           未知行业目标仓位=sum(r['target_value'] for r in rows if r['industry'] == UNKNOWN_INDUSTRY) / equity,
           **counts)
    log.info('%s 月度调仓：合格%d只、资金/成本剔除%d只，计划持股%d只，目标仓位%.2f%%，预计剩余现金%.2f；诊断%s',
             month, len(ranked), len(skipped), sum(v > 0 for v in targets.values()), target_value / equity * 100,
             estimated_cash, counts)
    for code in selected:
        item = candidates[code]
        log.info('%s 排名%d 动量%.4f 年化波动%.4f 行业%s 理论权重%.4f',
                 code, ranked.index(code) + 1, item['momentum'], item['volatility'], item['industry'], weights[code])
    if counts['状态缺失数']:
        log.warning('%d只股票缺少截至%s可见的历史状态；未知行业上限%.0f%%不代表已分散行业',
                    counts['状态缺失数'], previous, context.unknown_industry_cap * 100)
