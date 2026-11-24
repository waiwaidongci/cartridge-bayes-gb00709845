import hashlib
import json
import time

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.constants.enums import State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import datasets, deliveries, merge_ledgers, runs, schedules
from src.services.data import parse_options
from src.services.merge import business_content, correction_records, merge_records, revisioned_content


def save_merged(connection, name, records, quarantined, owner, now):
    payload = json.dumps({'records': records, 'quarantined': quarantined}, sort_keys=True,
                         ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    existing = select(datasets.c.id).where(datasets.c.checksum == checksum)
    found = connection.scalar(existing)
    if found is not None:
        return found, False
    try:
        with connection.begin_nested():
            key = connection.execute(insert(datasets).values(name=name.strip(), owner=owner, records=records,
                                                             checksum=checksum, created=now)).inserted_primary_key[0]
            connection.execute(insert(merge_ledgers).values(dataset_id=key, meta={'quarantined': quarantined}))
    except IntegrityError:
        found = connection.scalar(existing)
        if found is None:
            raise
        return found, False
    return key, True


def merge_correction(body, actor):
    """把补传更正并入最新台账版本；有效记录变化时按上一分析参数排队重算。"""
    incoming = correction_records(body.csv)
    if not body.name.strip():
        raise ValueError('数据名称不能为空。')
    now = time.time()
    with engine.begin() as connection:
        base = connection.execute(select(datasets).order_by(datasets.c.id.desc()).limit(1)).mappings().one_or_none()
        if base is None:
            raise HTTPException(409, '请先导入台账数据，再合并更正。')
        ledger = connection.execute(select(merge_ledgers.c.meta)
                                    .where(merge_ledgers.c.dataset_id == base['id'])).scalar_one_or_none() or {}
        previous = ledger.get('quarantined') or {}
        merged, quarantined, events = merge_records(base['records'], previous, incoming, now)
        changed = business_content(base['records']) != business_content(merged)
        versioned = changed or quarantined != previous or revisioned_content(base['records']) != revisioned_content(merged)
        if not versioned:
            add_event(connection, actor, 'merge', base['id'], now)
            return {'id': base['id'], 'created': False, 'rows': len(merged), 'changed': False,
                    'run_id': None, 'events': events, 'quarantined': quarantined}
        key, created = save_merged(connection, body.name, merged, quarantined, actor, now)
        run_id = None
        if changed:
            last = connection.execute(select(runs.c.options).where(runs.c.dataset_id == base['id'])
                                      .order_by(runs.c.id.desc()).limit(1)).mappings().one_or_none()
            if last is not None:
                run_id = connection.execute(insert(runs).values(dataset_id=key, options=last['options'],
                                            state=State.queued.value, owner=actor, created=now)).inserted_primary_key[0]
                add_event(connection, actor, 'queue', run_id, now)
        add_event(connection, actor, 'merge', key, now)
    return {'id': key, 'created': created, 'rows': len(merged), 'changed': changed,
            'run_id': run_id, 'events': events, 'quarantined': quarantined}


def add_run(body, actor):
    options = parse_options(body.options)
    now = time.time()
    with engine.begin() as connection:
        if fetch_one(connection, datasets, body.dataset_id) is None:
            raise HTTPException(404, '数据版本不存在。')
        key = connection.execute(insert(runs).values(dataset_id=body.dataset_id, options=options,
                    state=State.queued.value, owner=actor, created=now)).inserted_primary_key[0]
        add_event(connection, actor, 'queue', key, now)
    return {'id': key, 'state': State.queued.value}


def claim_run():
    now = time.time()
    claimable = or_(runs.c.state == State.queued.value, and_(runs.c.state == State.running.value, runs.c.started < now - 120))
    with engine.begin() as connection:
        run = connection.execute(select(runs).where(claimable).order_by(runs.c.id).limit(1)).mappings().one_or_none()
        if run is None:
            return None
        key = run['id']
        claim = connection.execute(update(runs).where(runs.c.id == key, claimable)
                                   .values(state=State.running.value, started=now))
        if claim.rowcount != 1:
            return None
        data = fetch_one(connection, datasets, run['dataset_id'])
        return key, now, data['records'], run['options']


def finish_run(key, claim, result, error):
    now = time.time()
    state = State.failed.value if error else State.completed.value
    with engine.begin() as connection:
        saved = connection.execute(update(runs).where(runs.c.id == key, runs.c.started == claim,
                                   runs.c.state == State.running.value).values(state=state, result=result,
                                                  error=error, finished=now))
        if saved.rowcount:
            add_event(connection, 'worker', state, key, now)


def review_run(key, body, actor):
    now = time.time()
    note = body.note.strip()
    if len(note) < 2:
        raise ValueError('请输入至少两个字符的复核意见。')
    with engine.begin() as connection:
        run = fetch_one(connection, runs, key)
        if run is None:
            raise HTTPException(404, '分析任务不存在。')
        if run['state'] != State.completed.value:
            raise HTTPException(409, '分析完成后才能复核。')
        if body.decision == 'approved' and run['result'].get('can_publish') is False:
            raise HTTPException(409, '结果未达到本项目的发布门槛。')
        updated = connection.execute(update(runs).where(runs.c.id == key, runs.c.decision.is_(None))
                                     .values(decision=body.decision, reviewer=actor, review_note=note, reviewed=now))
        if updated.rowcount != 1:
            raise HTTPException(409, '已完成复核。请重新运行分析以提交新的复核。')
        add_event(connection, actor, body.decision, key, now)
    return {'decision': body.decision}


def schedule_report(key, body, actor):
    now = time.time()
    recipient = body.recipient.strip()
    if len(recipient) < 2:
        raise ValueError('请填写站内收件组。')
    if body.scheduled_at and body.scheduled_at.utcoffset() is None:
        raise ValueError('投递时间须含时区。')
    timestamp = body.scheduled_at.timestamp() if body.scheduled_at else now
    if body.scheduled_at and timestamp < now - 60:
        raise ValueError('投递时间已过，请调整时间。')
    with engine.begin() as connection:
        run = fetch_one(connection, runs, key)
        if run is None or run['decision'] != 'approved':
            raise HTTPException(409, '仅已批准的结果可分发。')
        required_recipient = run['result'].get('dispatch_recipient')
        if required_recipient and recipient != required_recipient:
            raise HTTPException(422, f'该报告应投递给{required_recipient}。')
        schedule_id = connection.execute(insert(schedules).values(run_id=key, recipient=recipient, cadence=body.cadence,
                                      next_at=timestamp, active=1, owner=actor)).inserted_primary_key[0]
        add_event(connection, actor, 'schedule', schedule_id, now)
    return {'id': schedule_id, 'next_at': timestamp}


def cancel_schedule(key, actor):
    with engine.begin() as connection:
        updated = connection.execute(update(schedules).where(schedules.c.id == key, schedules.c.active == 1)
                                     .values(active=0))
        if not updated.rowcount:
            raise HTTPException(409, '该计划已停止或已完成。')
        add_event(connection, actor, 'cancel', key, time.time())
    return {'id': key, 'active': False}


def publish_due():
    now = time.time()
    with engine.begin() as connection:
        pending = connection.execute(select(schedules).where(schedules.c.active == 1, schedules.c.next_at <= now)
                                     .order_by(schedules.c.next_at, schedules.c.id).limit(20)).mappings().all()
        for item in pending:
            previous = item['next_at']
            daily = item['cadence'] == 'daily'
            future = previous + (int((now - previous) / 86400) + 1) * 86400 if daily else previous
            changed = connection.execute(update(schedules).where(schedules.c.id == item['id'], schedules.c.active == 1,
                              schedules.c.next_at == previous).values(active=int(daily), next_at=future))
            if changed.rowcount:
                delivery_id = connection.execute(insert(deliveries).values(schedule_id=item['id'], run_id=item['run_id'],
                                               recipient=item['recipient'], scheduled=previous, delivered=now)).inserted_primary_key[0]
                add_event(connection, 'worker', 'delivered', delivery_id, now)
