import sqlite3, json
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS ship_requests (request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, voyage TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)")
    con.execute('CREATE INDEX IF NOT EXISTS idx_ship_run ON ship_requests(run_id)')
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def _row_to_request(row):
    payload=json.loads(row['payload'])
    return {'request_id':row['request_id'],'run_id':row['run_id'],
            'attempt_no':row['attempt_no'],'voyage':row['voyage'],
            'created_at':row['created_at'],**payload}

def ship_history(path, run_id):
    """同一主批的出运申请时间线，按申请次序返回（含失败申请，保留原清单）。"""
    with connect(path) as con:
        rows=con.execute('SELECT * FROM ship_requests WHERE run_id=? ORDER BY attempt_no',(run_id,)).fetchall()
    return [_row_to_request(row) for row in rows]

def confirmed_lines(path, run_id):
    """已生效的出运行：只取放行成功的申请，已出运结果保留原样不被改写。"""
    lines=[]
    for request in ship_history(path, run_id):
        if request.get('status')=='放行':
            for line in request['lines']:
                lines.append({**line,'voyage':request['voyage'],
                              'request_id':request['request_id']})
    return lines

def get_request(path, request_id):
    with connect(path) as con:
        row=con.execute('SELECT * FROM ship_requests WHERE request_id=?',(request_id,)).fetchone()
    return _row_to_request(row) if row else None

def next_attempt(path, run_id):
    with connect(path) as con:
        row=con.execute('SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM ship_requests WHERE run_id=?',(run_id,)).fetchone()
    return row['n']

def record_ship_request(path, run_id, request_id, voyage, payload):
    """幂等落盘：同一出运申请（request_id）重试只生效一次。

    首次申请在单事务内插入；规则阻断时插入失败记录但不产生任何生效行，
    旧版出运结果与原清单保持原样。重试同一 request_id 直接回显首次结论。
    """
    existing=get_request(path, request_id)
    if existing is not None:
        if existing['run_id']!=run_id:
            raise BusinessError('REQUEST_REPLAY','申请编号已用于其他批次',409)
        return existing, False
    record={'request_id':request_id,'run_id':run_id,
            'attempt_no':next_attempt(path, run_id),'voyage':voyage,
            'payload':json.dumps(payload,ensure_ascii=False),
            'created_at':datetime.now(timezone.utc).isoformat()}
    with connect(path) as con:
        try:
            con.execute('INSERT INTO ship_requests(request_id,run_id,attempt_no,voyage,payload,created_at) VALUES(:request_id,:run_id,:attempt_no,:voyage,:payload,:created_at)',record)
        except sqlite3.IntegrityError:
            con.rollback()
            existing=get_request(path, request_id)
            if existing is not None:
                return existing, False
            raise
    return get_request(path, request_id), True
