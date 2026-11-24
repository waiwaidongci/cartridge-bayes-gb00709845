"""合并校点补传的耗材更正记录。

按实物编号（cartridge_id）与校点修订号（revision）合并乱序记录：
- 低修订号不得覆盖高修订号；
- 同一修订号内容不一致时只隔离该件，其余记录照常合并；
- 更高修订号到达后解除隔离，隔离件不参与重算。
"""

import json

from src.services.fields import PROFILE, read_rows

BUSINESS_KEYS = tuple(field['key'] for field in PROFILE['columns'])
REVISION_KEY = PROFILE['correction']['column']['key']
MAX_QUARANTINE_CONTENTS = 4
EVENT_NAMES = ('applied', 'stale', 'duplicates', 'held', 'quarantined', 'released')


def correction_records(text):
    """解析补传更正 CSV：八个台账列之外必须附校点修订号列。"""
    return read_rows(text, [*PROFILE['columns'], PROFILE['correction']['column']])


def fingerprint(row):
    return json.dumps({key: row[key] for key in BUSINESS_KEYS}, sort_keys=True,
                      ensure_ascii=False, allow_nan=False)


def business_content(records):
    """有效记录的业务内容指纹，用于判断合并是否改变了台账。"""
    return sorted(fingerprint(row) for row in records)


def revisioned_content(records):
    """含修订号的完整指纹，用于判断是否需要保存新的数据版本。"""
    return sorted(json.dumps({**row, REVISION_KEY: int(row.get(REVISION_KEY, 0))},
                             sort_keys=True, ensure_ascii=False, allow_nan=False) for row in records)


def _snapshot(row):
    return {**{key: row[key] for key in BUSINESS_KEYS}, REVISION_KEY: row[REVISION_KEY]}


def _conflict(revision, rows, now):
    contents, seen = [], set()
    for row in rows:
        mark = fingerprint(row)
        if mark not in seen:
            seen.add(mark)
            contents.append(_snapshot(row))
    return {'revision': revision, 'at': now, 'contents': contents[:MAX_QUARANTINE_CONTENTS]}


def merge_records(base_records, quarantined, incoming, now):
    """把更正记录并入现行台账，返回（有效记录，隔离清单，合并事件）。

    base_records：现行数据版本的记录（可缺省 revision，视为 0）。
    quarantined：现行隔离清单 {耗材编号: {revision, at, contents}}。
    incoming：correction_records 解析出的更正记录，允许乱序与重复。
    """
    effective = {}
    for row in base_records:
        normalized = {**row, REVISION_KEY: int(row.get(REVISION_KEY, 0))}
        effective[normalized['cartridge_id']] = normalized
    quarantine = {key: dict(value) for key, value in (quarantined or {}).items()}
    events = {name: [] for name in EVENT_NAMES}

    batches = {}
    for row in incoming:
        normalized = {**row, REVISION_KEY: int(row.get(REVISION_KEY, 0))}
        batches.setdefault(normalized['cartridge_id'], []).append(normalized)

    for cartridge in sorted(batches):
        rows = batches[cartridge]
        top_revision = max(row[REVISION_KEY] for row in rows)
        tops = [row for row in rows if row[REVISION_KEY] == top_revision]
        distinct = {fingerprint(row) for row in tops}
        current = effective.get(cartridge)
        current_revision = current[REVISION_KEY] if current is not None else None
        held = quarantine.get(cartridge)
        held_revision = held['revision'] if held is not None else None

        if current_revision is not None and top_revision < current_revision:
            events['stale'].append(cartridge)  # 旧版不得覆盖新版
            continue
        if held_revision is not None and top_revision < held_revision:
            events['stale'].append(cartridge)
            continue
        if current_revision is not None and top_revision == current_revision:
            if len(distinct) == 1 and fingerprint(current) == next(iter(distinct)):
                events['duplicates'].append(cartridge)  # 同版同内容，幂等跳过
            else:  # 同版冲突：只隔离该件
                del effective[cartridge]
                quarantine[cartridge] = _conflict(top_revision, [current, *tops], now)
                events['quarantined'].append(cartridge)
            continue
        if held_revision is not None and top_revision == held_revision:
            known = {fingerprint(item) for item in held['contents']}
            extras = [row for row in tops if fingerprint(row) not in known]
            if extras:  # 同版再来不解除隔离，仅补充冲突内容备查
                held['contents'] = (held['contents'] + [_snapshot(row) for row in extras])[:MAX_QUARANTINE_CONTENTS]
                held['at'] = now
            events['held'].append(cartridge)
            continue
        if len(distinct) > 1:  # 更高修订自身冲突：隔离该件，等待再次修订
            if current is not None:
                del effective[cartridge]
            quarantine[cartridge] = _conflict(top_revision, tops, now)
            events['quarantined'].append(cartridge)
            continue
        effective[cartridge] = dict(tops[0])  # 更高修订：采用；若曾隔离则解除
        if held is not None:
            del quarantine[cartridge]
            events['released'].append(cartridge)
        events['applied'].append(cartridge)

    merged = [effective[cartridge] for cartridge in sorted(effective)]
    return merged, quarantine, events
