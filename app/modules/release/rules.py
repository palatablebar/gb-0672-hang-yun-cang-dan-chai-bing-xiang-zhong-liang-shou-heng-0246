"""跨航次分批出运放行规则。

主提单可跨多个航次分批出运。本模块只做纯计算，不触碰数据库：
- 每次放行从未出运余额中选节点，整节点出运，余额自底向上逐层重算；
- 父层与子层在同一航次只计一次（祖先在选则后代折叠）；
- 件数、净重、毛重以逐层核验后的节点值为准，未对平则阻断；
- 已出运证据保持原样，修订后按"已出运证据 + 未出运余额能否共同说明"裁决冻结或继续放行。
"""
from decimal import Decimal

FIELDS = ('pieces', 'net_kg', 'gross_kg')
ZERO = {f: Decimal('0') for f in FIELDS}


def _dec(value):
    return Decimal(str(value))


def node_map(state):
    return {r['id']: r for r in state['records']}


def effective_lines(selected, state):
    """父层和子层在同一航次只计一次：选中节点若有选中的祖先，则折叠掉。

    返回保留的节点 id 列表，按图中的稳定顺序（records 顺序）输出。
    """
    chosen = set(selected)
    parent_of = {r['id']: r['parent'].strip() for r in state['records']}

    def has_chosen_ancestor(node_id):
        parent = parent_of.get(node_id, '')
        seen = {node_id}  # 环回到起点时停止，不能把节点自身算作祖先
        while parent and parent not in seen:
            if parent in chosen:
                return True
            seen.add(parent)
            parent = parent_of.get(parent, '')
        return False

    return [r['id'] for r in state['records']
            if r['id'] in chosen and not has_chosen_ancestor(r['id'])]


def _subtree(node_id, children):
    """节点自身及其全部后代（含多层）。"""
    result = []
    stack = [node_id]
    while stack:
        current = stack.pop()
        result.append(current)
        stack.extend(children.get(current, []))
    return result


def shipped_coverage(confirmed_lines, state):
    """把历史有效出运行折算成每节点两类覆盖事实。

    - covered：节点落在某条出运行的子树内（整节点出运，余额归零）；
    - deductions：出运行节点的祖先（含主单）应从申报量中扣减的量。
    """
    children = state['graph']['children']
    parent_of = {r['id']: r['parent'].strip() for r in state['records']}
    covered = set()
    deductions = {r['id']: {f: Decimal('0') for f in FIELDS}
                  for r in state['records']}
    for line in confirmed_lines:
        subtree = set(_subtree(line['node_id'], children))
        covered.update(subtree)
        ancestor = parent_of.get(line['node_id'], '')
        seen = {line['node_id']}
        while ancestor:
            if ancestor in seen:
                break  # 环保护
            for field in FIELDS:
                deductions[ancestor][field] += _dec(line[field])
            seen.add(ancestor)
            ancestor = parent_of.get(ancestor, '')
    return {'covered': covered, 'deductions': deductions}


def compute_balances(state, confirmed_lines):
    """自底向上重算未出运余额。

    整节点出运：节点一旦落在某条出运行的子树内，自身与全部后代余额归零；
    未被整体覆盖的节点，余额 = 自身申报量 − 祖先出运行已出运的量。
    """
    coverage = shipped_coverage(confirmed_lines, state)
    covered = coverage['covered']
    deductions = coverage['deductions']
    balances = {}
    for row in state['records']:
        node_id = row['id']
        if node_id in covered:
            balances[node_id] = {field: Decimal('0') for field in FIELDS}
        else:
            balances[node_id] = {
                field: _dec(row[field]) - deductions[node_id][field]
                for field in FIELDS}
    return balances


def totals_for_lines(line_ids, state):
    """放行行合计：直接加保留行的节点值，不重复加总折叠掉的后代。"""
    records = node_map(state)
    totals = {f: Decimal('0') for f in FIELDS}
    for node_id in line_ids:
        for field in FIELDS:
            totals[field] += _dec(records[node_id][field])
    return totals


def _bad_weight_nodes(state):
    """逐层核验未对平的节点 id 集合。"""
    return {c['id'] for c in state.get('weight_checks', [])
            if c.get('code') != 'BALANCED'}


def _error_nodes(state):
    """拆并关系存在错误（环、缺父级）的节点 id 集合。"""
    return {e['id'] for e in state['graph'].get('errors', [])}


def adjudicate_revision(state, confirmed_lines):
    """修订裁决：按已出运证据与未出运余额能否共同说明，逐主单给结论。

    - 已出运证据节点在新舱单中缺失/身份变化/数量对不上/关系或重量出错：冻结该主单；
    - 修订只触及未出运明细：主单保持可放行，剩余余额按当前有效舱单重新核验。
    """
    records = node_map(state)
    children = state['graph']['children']
    bad_weight = _bad_weight_nodes(state)
    graph_errors = _error_nodes(state)
    root_of = {}
    for root in state['graph']['roots']:
        for node_id in _subtree(root, children):
            root_of[node_id] = root

    roots = {root: {'root': root, 'frozen': False, 'reasons': []}
             for root in state['graph']['roots']}
    for line in confirmed_lines:
        node_id = line['node_id']
        root = line.get('root') or root_of.get(node_id, '')
        verdict = roots.setdefault(root, {'root': root, 'frozen': False,
                                          'reasons': []})
        current = records.get(node_id)
        if current is None:
            verdict['frozen'] = True
            verdict['reasons'].append({
                'id': node_id, 'code': 'SHIPPED_NODE_MISSING',
                'detail': '已出运节点在修订舱单中缺失'})
            continue
        changed = [field for field in FIELDS
                   if _dec(current[field]) != _dec(line[field])]
        if changed:
            verdict['frozen'] = True
            verdict['reasons'].append({
                'id': node_id, 'code': 'SHIPPED_EVIDENCE_CHANGED',
                'detail': '已出运证据数值被修订改写：' + '、'.join(changed)})
        if node_id in graph_errors:
            verdict['frozen'] = True
            verdict['reasons'].append({
                'id': node_id, 'code': 'RELATION_CHANGED',
                'detail': '已出运节点的拆并关系存在环或缺失父级'})
        if node_id in bad_weight:
            verdict['frozen'] = True
            verdict['reasons'].append({
                'id': node_id, 'code': 'WEIGHT_NOT_BALANCED',
                'detail': '已出运节点净毛重逐层核验未对平'})

    # 未出运部分的问题只阻断放行，不冻结；让裁决可被追溯。
    open_blocks = []
    for row in state['records']:
        node_id = row['id']
        root = root_of.get(node_id, '')
        if node_id in graph_errors:
            open_blocks.append({'id': node_id, 'root': root,
                                'code': 'RELATION_CHANGED',
                                'detail': '未出运节点拆并关系错误'})
        if node_id in bad_weight:
            open_blocks.append({'id': node_id, 'root': root,
                                'code': 'WEIGHT_NOT_BALANCED',
                                'detail': '未出运节点净毛重未对平'})
    return {'roots': sorted(roots.values(), key=lambda item: item['root']),
            'frozen_roots': sorted(v['root'] for v in roots.values()
                                   if v['frozen']),
            'open_blocks': open_blocks}


def plan_release(state, selected, confirmed_lines):
    """对一次出运申请生成放行清单与阻断结论（不写库）。

    返回 status=放行/阻断；阻断时 lines 为空并在 blocks 写明每个原因。
    """
    if not isinstance(selected, list) or not selected or \
            not all(isinstance(node_id, str) and node_id.strip()
                    for node_id in selected):
        return {'status': '阻断', 'lines': [], 'totals': dict(ZERO),
                'blocks': [{'id': '', 'code': 'EMPTY_SELECTION',
                            'detail': '出运申请未选择任何货物'}]}

    records = node_map(state)
    chosen = [node_id.strip() for node_id in selected]
    blocks = []

    unknown = sorted({node_id for node_id in chosen
                      if node_id not in records})
    for node_id in unknown:
        blocks.append({'id': node_id, 'code': 'UNKNOWN_REFERENCE',
                       'detail': '选中节点不在当前有效舱单中'})

    line_ids = effective_lines(chosen, state)
    folded = sorted(set(chosen) - set(line_ids))
    for node_id in folded:
        blocks.append({'id': node_id, 'code': 'LINE_COLLAPSED',
                       'detail': '父层已在同一航次出运，子层只计一次'})

    balances = compute_balances(state, confirmed_lines)
    adjudication = adjudicate_revision(state, confirmed_lines)
    frozen_roots = set(adjudication['frozen_roots'])
    children = state['graph']['children']
    root_of = {}
    for root in state['graph']['roots']:
        for node_id in _subtree(root, children):
            root_of[node_id] = root
    bad_weight = _bad_weight_nodes(state)
    graph_errors = _error_nodes(state)

    lines = []
    for node_id in line_ids:
        if node_id in unknown:
            continue
        root = root_of.get(node_id, '')
        if root in frozen_roots:
            blocks.append({'id': node_id, 'root': root,
                           'code': 'MASTER_FROZEN',
                           'detail': f'主单 {root} 已被修订冻结，停止交接'})
            continue
        short = [field for field in FIELDS
                 if balances[node_id][field] == 0]
        if short:
            blocks.append({'id': node_id, 'root': root,
                           'code': 'INSUFFICIENT_BALANCE',
                           'detail': '未出运余额不足：' + '、'.join(short)})
            continue
        if node_id in graph_errors:
            blocks.append({'id': node_id, 'root': root,
                           'code': 'RELATION_CHANGED',
                           'detail': '节点拆并关系存在环或缺失父级'})
            continue
        if node_id in bad_weight:
            blocks.append({'id': node_id, 'root': root,
                           'code': 'WEIGHT_NOT_BALANCED',
                           'detail': '件数或净毛重与逐层核验结果不一致'})
            continue
        row = records[node_id]
        lines.append({
            'node_id': node_id, 'root': root,
            'pieces': int(row['pieces']),
            'net_kg': str(row['net_kg']),
            'gross_kg': str(row['gross_kg']),
            'balance_after': {field: str(balances[node_id][field]
                                         - _dec(row[field]))
                              for field in FIELDS},
        })

    totals = totals_for_lines([line['node_id'] for line in lines], state)
    status = '阻断' if blocks else '放行'
    return {
        'status': status,
        'lines': lines if not blocks else [],
        'totals': {field: str(value) for field, value in totals.items()},
        'blocks': blocks,
        'balances': {node_id: {field: str(value) for field, value in vals.items()}
                     for node_id, vals in balances.items()},
        'adjudication': adjudication,
    }


def build_view(state, confirmed_lines, history):
    """组装放行页视图：每主单裁决、每节点余额、历史航次证据。"""
    balances = compute_balances(state, confirmed_lines)
    adjudication = adjudicate_revision(state, confirmed_lines)
    root_of = {}
    for root in state['graph']['roots']:
        for node_id in _subtree(root, state['graph']['children']):
            root_of[node_id] = root
    balance_rows = []
    for row in state['records']:
        node_id = row['id']
        balance_rows.append({
            'id': node_id, 'root': root_of.get(node_id, ''),
            'pieces': row['pieces'], 'net_kg': row['net_kg'],
            'gross_kg': row['gross_kg'],
            'remain_pieces': balances[node_id]['pieces'],
            'remain_net_kg': str(balances[node_id]['net_kg']),
            'remain_gross_kg': str(balances[node_id]['gross_kg']),
        })
    metrics = {
        '主单': len(state['graph']['roots']),
        '冻结主单': len(adjudication['frozen_roots']),
        '有效出运行': len(confirmed_lines),
        '历史申请': len(history),
    }
    return {'title': '放行清单', 'rows': balance_rows,
            'metrics': metrics, 'adjudication': adjudication,
            'history': history}


def apply(state):
    """引擎预览入口：尚无历史出运时给出初始余额视图。"""
    state['release_view'] = build_view(state, [], [])
    state['views']['release'] = state['release_view']
