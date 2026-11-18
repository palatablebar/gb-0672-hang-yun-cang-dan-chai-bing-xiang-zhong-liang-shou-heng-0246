import uuid
from fastapi import APIRouter, Request, Body
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app.errors import BusinessError
from app import store
from app.modules.release import rules

router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def _load_state(request, run_id):
    from app.main import DB_PATH
    return store.get(DB_PATH, run_id)


def _baseline_run_id(state, payload):
    """修订场景：新舱单批次通过 baseline_run_id 指向旧批的已出运证据。"""
    baseline = payload.get('baseline_run_id')
    if baseline is None:
        return state['manifest']['run_id']
    if not isinstance(baseline, str) or not baseline.strip():
        raise BusinessError('BAD_BASELINE', '基线批次编号无效')
    return baseline.strip()


def _confirmed_lines(baseline_id):
    from app.main import DB_PATH
    return store.confirmed_lines(DB_PATH, baseline_id)


def _history(run_id, baseline_id):
    from app.main import DB_PATH
    history = store.ship_history(DB_PATH, run_id)
    if baseline_id != run_id:
        history = store.ship_history(DB_PATH, baseline_id) + history
    return history


@router.get('/release')
def page(request: Request):
    from app.main import DB_PATH
    run_id = request.query_params.get('run_id')
    if run_id:
        state = store.get(DB_PATH, run_id)
    else:
        state = store.save(DB_PATH, evaluate())
        run_id = state['manifest']['run_id']
    baseline_id = request.query_params.get('baseline_run_id') or run_id
    confirmed = store.confirmed_lines(DB_PATH, baseline_id)
    view = rules.build_view(state, confirmed, _history(run_id, baseline_id))
    state['views']['release'] = view
    return templates.TemplateResponse(
        request=request, name='release.html',
        context={'meta': META, 'key': 'release', 'state': state,
                 'view': view, 'baseline_run_id': baseline_id})


@router.post('/api/release')
def calculate(payload: dict = Body(default={})):
    """模块演示入口：对当前批次按零历史出运计算初始放行视图。"""
    run_id = payload.get('run_id')
    if isinstance(run_id, str) and run_id.strip():
        state = _load_state(None, run_id.strip())
    else:
        from app.engine import evaluate
        state = evaluate(payload)
    rules.apply(state)
    return state


@router.post('/api/release/preview')
def preview(payload: dict = Body(default={})):
    """纯规则预览：给出放行清单、阻断原因与重算余额，不产生任何持久效果。"""
    run_id = payload.get('run_id')
    if not isinstance(run_id, str) or not run_id.strip():
        raise BusinessError('BAD_RUN', '缺少批次编号')
    state = _load_state(None, run_id.strip())
    baseline_id = _baseline_run_id(state, payload)
    selected = payload.get('selected', [])
    plan = rules.plan_release(
        state, selected, _confirmed_lines(baseline_id))
    return {'run_id': run_id, 'baseline_run_id': baseline_id, **plan}


@router.post('/api/release/ship')
def ship(payload: dict = Body(...)):
    """确认出运：同一 request_id 重试只生效一次；阻断时失败留痕并恢复旧版结果。"""
    run_id = payload.get('run_id')
    if not isinstance(run_id, str) or not run_id.strip():
        raise BusinessError('BAD_RUN', '缺少批次编号')
    run_id = run_id.strip()
    state = _load_state(None, run_id)
    baseline_id = _baseline_run_id(state, payload)
    request_id = str(payload.get('request_id') or '').strip() or \
        uuid.uuid4().hex
    voyage = str(payload.get('voyage') or '').strip()
    if not voyage:
        raise BusinessError('BAD_VOYAGE', '航次编号不能为空')

    # 先看是否同一申请重试：重放直接回显首次结论，不重新裁决、不重复生效。
    from app.main import DB_PATH
    existing = store.get_request(DB_PATH, request_id)
    if existing is not None:
        if existing['run_id'] != run_id:
            raise BusinessError('REQUEST_REPLAY', '申请编号已用于其他批次', 409)
        return {'replayed': True, **existing}

    confirmed_before = _confirmed_lines(baseline_id)
    plan = rules.plan_release(state, payload.get('selected', []),
                              confirmed_before)
    record_payload = {
        'status': plan['status'], 'voyage': voyage,
        'baseline_run_id': baseline_id,
        'selected': payload.get('selected', []),
        'lines': plan['lines'], 'totals': plan['totals'],
        'blocks': plan['blocks'],
    }
    saved, created = store.record_ship_request(
        DB_PATH, run_id, request_id, voyage, record_payload)
    # 阻断：无新生效行，confirmed_lines 仍是旧版，原清单随失败记录保留。
    saved['replayed'] = not created
    saved['balances'] = plan['balances']
    saved['adjudication'] = plan['adjudication']
    return saved


@router.get('/api/release/{run_id}/history')
def history(run_id: str, baseline_run_id: str | None = None):
    from app.main import DB_PATH
    baseline_id = baseline_run_id or run_id
    return {'run_id': run_id, 'baseline_run_id': baseline_id,
            'confirmed_lines': store.confirmed_lines(DB_PATH, baseline_id),
            'requests': _history(run_id, baseline_id)}
