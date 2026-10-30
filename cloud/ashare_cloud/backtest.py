"""Offline frozen-plan replay. No reconstructed signals, network, AI or live writes."""
from __future__ import annotations
import copy
from pathlib import Path
from .common import Calendar, digest, file_hash, now, read_json, write_json
from .engine import simulate


def replay(root, calendar=None):
    root = Path(root)
    account = read_json(root / 'account.json')
    from .cli import check_account
    check_account(account)
    days = sorted(account['settlements'])
    inputs = {'account_sha256': file_hash(root / 'account.json'), 'snapshots': {}}
    result = {'schema': 1, 'kind': 'frozen_plan_replay', 'title': '冻结计划与归档行情重放',
        'generated_at': now().isoformat(), 'status': 'blocked', 'auto_apply': False,
        'start': days[0] if days else None, 'end': days[-1] if days else None,
        'limitations': ['仅重放已归档模拟账户；不代表全市场历史策略回测或样本外验证。',
                         '沿用原成交假设和费用，不推算缺失竞价、不补造历史选股计划。'],
        'days': [], 'reason': '', 'inputs': inputs}
    try:
        if not days:
            raise ValueError('没有归档结算，尚不能回测')
        initial = account.get('provenance', {}).get('original_seed_cash')
        if not isinstance(initial, (int, float)) or initial <= 0:
            raise ValueError('缺少已验证的初始空仓资金记录')
        calendar = calendar or Calendar()
        state = {'cash': initial, 'equity': initial, 'positions': [], 'closed_trades': [],
                 'last_settled': None, 'settlements': {}}
        peak, drawdown, buys, sells = initial, 0, 0, 0
        for i, day in enumerate(days):
            plan = account['plans'].get(day)
            if i and plan is None:
                raise ValueError(day + ' 缺少盘前冻结计划')
            path = root / 'snapshots' / (day + '.json')
            if not path.exists():
                raise ValueError(day + ' 缺少归档行情')
            inputs['snapshots'][day] = file_hash(path)
            market = read_json(path)
            state, settlement = simulate(state, copy.deepcopy(plan), market, day, calendar)
            if digest(settlement) != digest(account['settlements'][day]):
                raise ValueError(day + ' 重放结算与原记录不一致')
            peak = max(peak, state['equity'])
            drawdown = max(drawdown, (peak - state['equity']) / peak * 100)
            buys += len(settlement['buys']); sells += len(settlement['sells'])
            result['days'].append({'date': day, 'equity': state['equity'], 'day_pnl': settlement['day_pnl']})
        for key in ('cash', 'equity', 'positions', 'closed_trades', 'last_settled'):
            if state[key] != account[key]:
                raise ValueError('重放末态与账本不一致：' + key)
        result.update(status='ok', reason='逐日结算与原账本一致', initial_equity=initial,
                      final_equity=state['equity'], return_pct=round((state['equity']/initial-1)*100, 6),
                      max_drawdown_pct=round(drawdown, 6), buys=buys, sells=sells, sessions=len(days))
    except ValueError as exc:
        result['reason'] = str(exc)
    result['id'] = digest(inputs)[:24]
    write_json(root / 'backtests' / (result['id'] + '.json'), result)
    return result
