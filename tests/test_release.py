"""M8 放行清单：跨航次分批出运、余额重算、修订裁决与申请幂等。"""
from copy import deepcopy
from decimal import Decimal

from fastapi.testclient import TestClient

from app import main, store
from app.config import ROOT
from app.engine import evaluate
from app.modules.release import rules


# ---------- 纯规则层 ----------

def test_parent_child_counted_once_same_voyage(clean):
    """父层和子层在同一航次只计一次：折叠后代，清单只保留父层。"""
    plan = rules.plan_release(clean, ['M001-P1', 'M001-P1-1'], [])
    assert plan['status'] == '阻断'  # 折叠本身写明原因，停止交接
    codes = {b['code'] for b in plan['blocks']}
    assert 'LINE_COLLAPSED' in codes
    assert next(b for b in plan['blocks']
                if b['id'] == 'M001-P1-1')['code'] == 'LINE_COLLAPSED'

    # 只选父层时正常放行，件净重毛与节点核验值一致。
    plan = rules.plan_release(clean, ['M001-P1'], [])
    assert plan['status'] == '放行'
    assert [line['node_id'] for line in plan['lines']] == ['M001-P1']
    assert plan['totals'] == {'pieces': '20', 'net_kg': '200',
                              'gross_kg': '220'}


def test_balances_recomputed_across_voyages(clean):
    """第二次出运只从未出运余额选货，余额自底向上重算。"""
    first = rules.plan_release(clean, ['M001-P1'], [])
    assert first['status'] == '放行'
    confirmed = [dict(line, voyage='V1', request_id='r1')
                 for line in first['lines']]

    # M001-P1 整棵子树已被覆盖：自身与叶子余额归零，M001 主单仍有一半。
    balances = rules.compute_balances(clean, confirmed)
    assert balances['M001-P1']['pieces'] == Decimal('0')
    assert balances['M001-P1-1']['pieces'] == Decimal('0')
    assert balances['M001']['pieces'] == Decimal('20')
    second = rules.plan_release(clean, ['M001-P2'], confirmed)
    assert second['status'] == '放行'
    assert second['lines'][0]['balance_after']['pieces'] == '0'
    after = rules.compute_balances(
        clean, confirmed + [dict(second['lines'][0], voyage='V2',
                                 request_id='r2')])
    assert after['M001']['pieces'] == Decimal('0')


def test_insufficient_balance_blocks(clean):
    """余额不足时写明阻断原因，不产生放行行。"""
    first = rules.plan_release(clean, ['M001-P1'], [])
    confirmed = [dict(line, voyage='V1', request_id='r1')
                 for line in first['lines']]
    retry = rules.plan_release(clean, ['M001-P1'], confirmed)
    assert retry['status'] == '阻断'
    assert retry['lines'] == []
    block = next(b for b in retry['blocks'] if b['id'] == 'M001-P1')
    assert block['code'] == 'INSUFFICIENT_BALANCE'


def test_weight_mismatch_blocks(sample):
    """重量未对平时清单阻断并停止交接（现场脏批 M001 毛重差 1kg）。"""
    plan = rules.plan_release(sample, ['M001'], [])
    assert plan['status'] == '阻断'
    codes = {b['code'] for b in plan['blocks']}
    assert 'WEIGHT_NOT_BALANCED' in codes


def test_relation_error_blocks(sample):
    """拆并关系变化（环、缺父级）的节点不得放行。"""
    plan = rules.plan_release(sample, ['LOOP-A'], [])
    assert plan['status'] == '阻断'
    assert any(b['code'] == 'RELATION_CHANGED' for b in plan['blocks'])


def test_revision_unshipped_change_keeps_master_open(clean):
    """修订只改变未出运明细：主单不冻结，剩余余额按新舱单重新核验。"""
    first = rules.plan_release(clean, ['M001-P1'], [])
    confirmed = [dict(line, voyage='V1', request_id='r1')
                 for line in first['lines']]

    revised_csv = (ROOT / 'sample_data' / 'clean.csv').read_text().replace(
        'M001-P2-1,M001-P2,1,pallet,0.1,0.11,t',
        'M001-P2-1,M001-P2,1,pallet,0.12,0.13,t')
    revised = evaluate({'dataset': 'clean', 'csv_text': revised_csv})
    verdict = rules.adjudicate_revision(revised, confirmed)
    assert 'M001' not in verdict['frozen_roots']
    # 已出运证据保持原样，未出运叶子带着新值参与余额重算。
    balances = rules.compute_balances(revised, confirmed)
    assert balances['M001-P2-1']['net_kg'] == Decimal('120')
    # 修订后的未出运叶子仍可继续放行（父节点因子值变化核验不平，改选叶子）。
    plan = rules.plan_release(revised, ['M001-P2-1'], confirmed)
    assert plan['status'] == '放行'


def test_revision_shipped_evidence_change_freezes_master(clean):
    """修订改写已出运证据：证据与余额无法共同说明，冻结整张主单。"""
    first = rules.plan_release(clean, ['M001-P1'], [])
    confirmed = [dict(line, voyage='V1', request_id='r1')
                 for line in first['lines']]

    revised_csv = (ROOT / 'sample_data' / 'clean.csv').read_text().replace(
        'M001-P1,M001,20,carton,200,220,kg',
        'M001-P1,M001,20,carton,205,220,kg')
    revised = evaluate({'dataset': 'clean', 'csv_text': revised_csv})
    verdict = rules.adjudicate_revision(revised, confirmed)
    assert 'M001' in verdict['frozen_roots']
    reason = verdict['roots'][0]['reasons'][0]
    assert reason['code'] == 'SHIPPED_EVIDENCE_CHANGED'

    # 冻结主单上的任何后续选货都被阻断。
    plan = rules.plan_release(revised, ['M001-P2'], confirmed)
    assert plan['status'] == '阻断'
    assert any(b['code'] == 'MASTER_FROZEN' for b in plan['blocks'])


def test_revision_shipped_node_missing_freezes(clean):
    """已出运节点在修订舱单中缺失：冻结主单并保留节点编号证据。"""
    first = rules.plan_release(clean, ['M001-P1'], [])
    confirmed = [dict(line, voyage='V1', request_id='r1')
                 for line in first['lines']]
    lines = [ln for ln in (ROOT / 'sample_data' / 'clean.csv')
             .read_text().splitlines() if 'M001-P1,' not in ln]
    revised = evaluate({'dataset': 'clean',
                        'csv_text': '\n'.join(lines)})
    verdict = rules.adjudicate_revision(revised, confirmed)
    assert 'M001' in verdict['frozen_roots']
    assert any(r['code'] == 'SHIPPED_NODE_MISSING'
               for r in verdict['roots'][0]['reasons'])


def test_leaf_level_shipments_accumulate_on_parent(clean):
    """叶子逐航次出运：父/主单余额逐次扣减，全部出完后整树归零。"""
    plan1 = rules.plan_release(clean, ['M001-P1-1'], [])
    assert plan1['status'] == '放行'
    confirmed = [dict(plan1['lines'][0], voyage='V1', request_id='r1')]
    balances = rules.compute_balances(clean, confirmed)
    # 叶子自身归零，其父与主单各扣 10 件。
    assert balances['M001-P1-1']['pieces'] == Decimal('0')
    assert balances['M001-P1']['pieces'] == Decimal('10')
    assert balances['M001']['pieces'] == Decimal('30')

    plan2 = rules.plan_release(clean, ['M001-P1-2'], confirmed)
    assert plan2['status'] == '放行'
    confirmed += [dict(plan2['lines'][0], voyage='V2', request_id='r2')]
    balances = rules.compute_balances(clean, confirmed)
    assert balances['M001-P1']['pieces'] == Decimal('0')
    assert balances['M001']['pieces'] == Decimal('20')
    # M001-P1 已无余额，再次选它必须阻断。
    again = rules.plan_release(clean, ['M001-P1'], confirmed)
    assert again['status'] == '阻断'
    assert again['blocks'][0]['code'] == 'INSUFFICIENT_BALANCE'


def test_empty_and_unknown_selection(clean):
    assert rules.plan_release(clean, [], [])['status'] == '阻断'
    plan = rules.plan_release(clean, ['NO-SUCH-NODE'], [])
    assert plan['status'] == '阻断'
    assert plan['blocks'][0]['code'] == 'UNKNOWN_REFERENCE'


# ---------- 持久层与 API ----------

def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'release.sqlite'))
    return TestClient(main.app)


def _clean_run(c):
    return c.post('/api/runs', json={'dataset': 'clean'}).json()


def test_ship_idempotent_and_balance_progress(tmp_path, monkeypatch):
    """同一申请重试只生效一次；跨航次余额逐次扣减。"""
    c = _client(tmp_path, monkeypatch)
    run = _clean_run(c)
    rid = run['manifest']['run_id']

    first = c.post('/api/release/ship', json={
        'run_id': rid, 'voyage': 'V-001', 'request_id': 'REQ-1',
        'selected': ['M001-P1']}).json()
    assert first['status'] == '放行'
    assert first['replayed'] is False

    # 同一 request_id 重试：回显首次结论，attempt 不增加、不重复出运。
    replay = c.post('/api/release/ship', json={
        'run_id': rid, 'voyage': 'V-001', 'request_id': 'REQ-1',
        'selected': ['M001-P1']}).json()
    assert replay['replayed'] is True
    assert replay['lines'] == first['lines']
    history = c.get(f'/api/release/{rid}/history').json()
    assert len(history['requests']) == 1
    assert len(history['confirmed_lines']) == 1

    # 第二航次放行剩余部分。
    second = c.post('/api/release/ship', json={
        'run_id': rid, 'voyage': 'V-002', 'request_id': 'REQ-2',
        'selected': ['M001-P2']}).json()
    assert second['status'] == '放行'
    history = c.get(f'/api/release/{rid}/history').json()
    assert [line['voyage'] for line in history['confirmed_lines']] == \
        ['V-001', 'V-002']


def test_failed_confirmation_keeps_old_result_and_list(tmp_path, monkeypatch):
    """确认失败：旧版出运结果保留原样，原清单随失败记录可追溯。"""
    c = _client(tmp_path, monkeypatch)
    rid = _clean_run(c)['manifest']['run_id']
    ok = c.post('/api/release/ship', json={
        'run_id': rid, 'voyage': 'V-001', 'request_id': 'REQ-OK',
        'selected': ['M001-P1']}).json()
    assert ok['status'] == '放行'

    # 超出未出运余额的申请确认失败。
    bad = c.post('/api/release/ship', json={
        'run_id': rid, 'voyage': 'V-002', 'request_id': 'REQ-BAD',
        'selected': ['M001-P1']}).json()
    assert bad['status'] == '阻断'
    assert any(b['code'] == 'INSUFFICIENT_BALANCE' for b in bad['blocks'])

    history = c.get(f'/api/release/{rid}/history').json()
    # 失败申请留痕，但有效出运行仍是旧版那一条。
    assert len(history['requests']) == 2
    assert len(history['confirmed_lines']) == 1
    assert history['confirmed_lines'][0]['request_id'] == 'REQ-OK'

    # 失败申请重试仍只回显同一条失败记录，不新增。
    replay = c.post('/api/release/ship', json={
        'run_id': rid, 'voyage': 'V-002', 'request_id': 'REQ-BAD',
        'selected': ['M001-P1']}).json()
    assert replay['status'] == '阻断' and replay['replayed'] is True
    assert len(c.get(f'/api/release/{rid}/history').json()['requests']) == 2


def test_revision_flow_baseline_freeze_and_continue(tmp_path, monkeypatch):
    """修订舱单通过基线批次继承已出运证据，分别裁决继续放行与冻结。"""
    c = _client(tmp_path, monkeypatch)
    base = _clean_run(c)
    base_id = base['manifest']['run_id']
    c.post('/api/release/ship', json={
        'run_id': base_id, 'voyage': 'V-001', 'request_id': 'REQ-1',
        'selected': ['M001-P1', 'M002-P1']}).raise_for_status()

    original = (ROOT / 'sample_data' / 'clean.csv').read_text()

    # 修订只动 M001 的未出运叶子：M001 继续放行，M002 不受影响。
    open_csv = original.replace(
        'M001-P2-1,M001-P2,1,pallet,0.1,0.11,t',
        'M001-P2-1,M001-P2,1,pallet,0.12,0.13,t')
    open_run = c.post('/api/runs',
                      json={'dataset': 'clean', 'csv_text': open_csv}).json()
    open_id = open_run['manifest']['run_id']
    plan = c.post('/api/release/preview', json={
        'run_id': open_id, 'baseline_run_id': base_id,
        'selected': ['M001-P2-1']}).json()
    assert plan['status'] == '放行'

    # 修订改写 M002 的已出运证据：M002 冻结，M001 仍可放行。
    frozen_csv = original.replace(
        'M002-P1,M002,20,carton,200,220,kg',
        'M002-P1,M002,20,carton,210,220,kg')
    frozen_run = c.post('/api/runs',
                        json={'dataset': 'clean', 'csv_text': frozen_csv}).json()
    frozen_id = frozen_run['manifest']['run_id']
    blocked = c.post('/api/release/preview', json={
        'run_id': frozen_id, 'baseline_run_id': base_id,
        'selected': ['M002-P2']}).json()
    assert blocked['status'] == '阻断'
    assert any(b['code'] == 'MASTER_FROZEN' for b in blocked['blocks'])
    safe = c.post('/api/release/preview', json={
        'run_id': frozen_id, 'baseline_run_id': base_id,
        'selected': ['M001-P2']}).json()
    assert safe['status'] == '放行'


def test_release_page_and_navigation(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = _clean_run(c)['manifest']['run_id']
    html = c.get(f'/release?run_id={rid}').text
    assert '未出运余额' in html and rid in html
    # 导航栏包含第八个模块。
    assert '/release?run_id=' in html


def test_bad_requests_rejected(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = _clean_run(c)['manifest']['run_id']
    assert c.post('/api/release/ship',
                  json={'run_id': rid, 'selected': ['M001-P1']}
                  ).status_code == 422  # 缺航次
    assert c.post('/api/release/ship',
                  json={'voyage': 'V1', 'selected': []}).status_code == 422


def test_export_bundle_contains_release_evidence(tmp_path, monkeypatch):
    import io
    import json
    import zipfile

    from app.archive import serialize
    c = _client(tmp_path, monkeypatch)
    rid = _clean_run(c)['manifest']['run_id']
    c.post('/api/release/ship', json={
        'run_id': rid, 'voyage': 'V-001', 'request_id': 'REQ-Z',
        'selected': ['M001-P1']}).raise_for_status()
    result = c.get(f'/api/runs/{rid}').json()
    result['release_evidence'] = {
        'confirmed_lines': store.confirmed_lines(
            main.DB_PATH, rid),
        'requests': store.ship_history(main.DB_PATH, rid)}
    bundle = zipfile.ZipFile(io.BytesIO(serialize(result)))
    release = json.loads(bundle.read('release.json'))
    assert release['confirmed_lines'][0]['voyage'] == 'V-001'
    assert release['requests'][0]['request_id'] == 'REQ-Z'
