import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from src.config.settings import IS_MYSQL, cache, engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, evidence, maintenance, runs
from src.services import domain
from src.utils.encoding import canonical
from src.utils.time import utc_time

MAINTENANCE_ID = 1


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def maintenance_row(conn, lock=False):
    stmt = select(maintenance).where(maintenance.c.id == MAINTENANCE_ID)
    if lock:
        stmt = stmt.with_for_update()
    row = conn.execute(stmt).mappings().first()
    if row is None:
        values = {'id': MAINTENANCE_ID, 'stopped': False, 'epoch': uuid.uuid4().hex}
        if IS_MYSQL:
            conn.execute(mysql_insert(maintenance).values(**values).on_duplicate_key_update(id=maintenance.c.id))
        else:
            conn.execute(postgres_insert(maintenance).values(**values).on_conflict_do_nothing(index_elements=['id']))
        row = conn.execute(stmt).mappings().first()
    return dict(row)


def maintenance_status():
    with engine.begin() as conn:
        state = maintenance_row(conn)
        running = conn.execute(select(func.count()).select_from(runs)
                               .where(runs.c.status == Status.RUNNING)).scalar_one()
        queued = conn.execute(select(func.count()).select_from(runs)
                              .where(runs.c.status == Status.QUEUED)).scalar_one()
        stored = select(evidence.c.id).where(evidence.c.run_id == runs.c.id)
        pending_evidence = conn.execute(select(func.count()).select_from(runs)
                                        .where(runs.c.status == Status.COMPLETE, ~stored.exists())).scalar_one()
    return {
        'stopped': state['stopped'],
        'epoch': state['epoch'],
        'stopped_by': state['stopped_by'],
        'stopped_at': state['stopped_at'],
        'resumed_by': state['resumed_by'],
        'resumed_at': state['resumed_at'],
        'running': running,
        'queued': queued,
        'pending_evidence': pending_evidence,
        # 所有已领单办结、成功凭证也入库后才报排空。
        'drained': bool(state['stopped'] and not running and not pending_evidence),
    }


def maintenance_stop(actor):
    with engine.begin() as conn:
        state = maintenance_row(conn, lock=True)
        if not state['stopped']:
            conn.execute(maintenance.update().where(maintenance.c.id == MAINTENANCE_ID).values(
                stopped=True, epoch=uuid.uuid4().hex, stopped_by=actor, stopped_at=utc_time(),
                resumed_by=None, resumed_at=None))
            log(conn, actor, 'maintenance_stop', 'maintenance')
    return maintenance_status()


def maintenance_resume(epoch, actor):
    with engine.begin() as conn:
        state = maintenance_row(conn, lock=True)
        # 已恢复时重试直接返回现状，不重复生效；恢复指令只对本次维护的 epoch 有效。
        if state['stopped']:
            if epoch != state['epoch']:
                raise HTTPException(409, '恢复指令与本次维护不匹配，请查询最新维护状态')
            conn.execute(maintenance.update().where(maintenance.c.id == MAINTENANCE_ID).values(
                stopped=False, resumed_by=actor, resumed_at=utc_time()))
            log(conn, actor, 'maintenance_resume', 'maintenance')
    return maintenance_status()


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        found(conn, cases, case_id)
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'enqueue', run_id)
        result = found(conn, runs, run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    stale = and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600))
    with engine.begin() as conn:
        # 与停领在同一行上串行：停领生效前已领的单继续执行，生效后不再领新单。
        state = maintenance_row(conn, lock=True)
        # 停领期间执行中断的陈旧单仍允许接手，保证已领单能够办结。
        ready = stale if state['stopped'] else or_(runs.c.status == Status.QUEUED, stale)
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        return run.id, token, found(conn, cases, run.case_id)['payload']


def finish(run_id, token, result=None, error=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        if result is not None:
            conn.execute(evidence.insert().values(run_id=run_id, report=result, digest=digest))
        log(conn, 'worker', status.value, run_id)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        found(conn, evidence, identity)
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)
