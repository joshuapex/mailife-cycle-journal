"""Private self-report records and explicitly fictional compartment simulation."""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

SIMULATION_NOTICE = "数值依据配置比例、衰减参数和事件记录计算，不是身体测量。分配比例、半衰期及排卵数量的生成参数不代表生理指标或受孕概率。"


def detect_report(message: dict, now: datetime, allowed_users: list[str]) -> dict | None:
    """Conservative rule matching; never use quoted, hypothetical or group text."""
    if not isinstance(message, dict):
        return None
    info = message.get("message_info")
    if not isinstance(info, dict) or info.get("group_info") is not None:
        return None
    user = info.get("user_info") or {}
    user_id = str(user.get("user_id") or "")
    if not user_id or (allowed_users and user_id not in allowed_users):
        return None
    if message.get("reply_to") or message.get("is_notify") or info.get("is_notify"):
        return None
    text = str(message.get("processed_plain_text") or "").strip()
    if not text or len(text) > 240:
        return None
    # Deliberately prefer missed reports to fabricating an intimate event.
    if re.search(r'[?？“”「」『』"<>]|如果|假如|假设|要是|可能|也许|打算|准备|明天|后天|将会|会不会|能不能|没有|并未|未发生|未曾|没发生|没做|没内射|未内射|还没|不曾|不是|别记|不要记|朋友|别人|他说|她说|告诉我|听说|看到|看见|刷到|听到|有人|问|了没|好像|想象|希望|是否|吗|小说|剧情|举例|例如|测试|撤回|取消|其实', text):
        return None
    if not re.search(r'我|我们|咱们|和你|跟你', text):
        return None
    if not re.search(r'刚才|刚刚|今天|昨天|前天|已经|发生了|做了|进行了|射精了|内射了|\d{4}-\d{2}-\d{2}', text):
        return None
    internal = bool(re.search(r'内射(?:了|过)?|射在(?:了)?(?:阴道|里面|体内)|体内射精', text))
    unprotected = bool(re.search(r'无保护性行为|未采取避孕措施|无套|没戴套|未戴套|未使用避孕套', text))
    protected = not unprotected and bool(re.search(r'戴(?:了)?(?:避孕)?套|使用(?:了)?避孕套|全程避孕套|采取了避孕措施', text))
    # "内射" with an explicit condom is ambiguous about placement: never deposit.
    if protected:
        internal = False
    if unprotected and not internal and not re.search(r'性行为|性交|做爱|发生(?:了)?关系|做了', text):
        return None
    sexual_action = bool(re.search(r'性行为|性交|做爱|同房|发生(?:了)?关系', text))
    completed_action = bool(re.search(r'发生了|进行了|有过|做了|做过|同房了|性交了', text))
    if not internal and not unprotected and not (sexual_action and completed_action):
        return None
    # Unsupported historical/clock times require manual entry, not a guessed time.
    if re.search(r'上周|上个月|去年|\d+天前|\d+小时前|\d{1,2}[:：]\d{2}|\d+点', text):
        return None
    found = re.search(r'\d{4}-\d{2}-\d{2}', text)
    try:
        day = date.fromisoformat(found.group()) if found else now.date() - timedelta(days=2 if '前天' in text else 1 if '昨天' in text else 0)
    except ValueError:
        return None
    if day > now.date():
        return None
    message_id = str(message.get("message_id") or info.get("message_id") or "")
    if not message_id:
        return None
    platform = str(message.get("platform") or info.get("platform") or "unknown")
    identity = platform + ':' + user_id
    source = hashlib.sha256((identity + ':' + message_id).encode()).hexdigest()
    return {"source_key": source, "person": hashlib.sha256(identity.encode()).hexdigest()[:10],
            "event_date": day.isoformat(), "kind": "internal" if internal else "unprotected" if unprotected else "protected" if protected else "unspecified",
            "precision": "reported_date", "origin": "private_self_report"}


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, source_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS cycles(key TEXT PRIMARY KEY, eggs INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('seed', ?)", (secrets.token_hex(32),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, report: dict, settings: dict, now: datetime) -> bool:
        item = {**report, "recorded_at": now.isoformat(), "volume_ml": settings['ejaculate_ml'],
                "vaginal_fraction": settings['vaginal_fraction'], "uterine_fraction": settings['uterine_fraction'],
                "vaginal_half_life_hours": settings['vaginal_half_life_hours'],
                "uterine_half_life_hours": settings['uterine_half_life_hours']}
        if item['precision'] == 'exact':
            timestamp = datetime.fromisoformat(item['occurred_at'])
        elif item['event_date'] == now.date().isoformat():
            # Start the toy simulation at record time, not an invented act time.
            timestamp = now
        else:
            timestamp = datetime.combine(date.fromisoformat(item['event_date']), datetime.min.time(), tzinfo=now.tzinfo)
        item['simulation_started_at'] = timestamp.isoformat()
        with self.connect() as db:
            result = db.execute("INSERT OR IGNORE INTO events(source_key,payload) VALUES (?,?)", (item['source_key'], json.dumps(item)))
            return result.rowcount == 1

    def records(self):
        with self.connect() as db:
            return [{**json.loads(payload), 'id': ident} for ident, payload in db.execute("SELECT id,payload FROM events ORDER BY id DESC")]

    def history(self, *, page=1, page_size=20, start_date='', end_date='', kind='', origin=''):
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100000:
            raise ValueError('页码必须是1到100000的整数')
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ValueError('每页条数必须是1到100的整数')
        for value in (start_date, end_date):
            if not isinstance(value, str) or (value and (len(value) != 10 or date.fromisoformat(value).isoformat() != value)):
                raise ValueError('日期筛选须为YYYY-MM-DD')
        if start_date and end_date and start_date > end_date:
            raise ValueError('开始日期不能晚于结束日期')
        if kind not in {'', 'internal', 'unprotected', 'protected', 'unspecified'}:
            raise ValueError('事件类型筛选无效')
        if origin not in {'', 'private_self_report', 'manual_self_report'}:
            raise ValueError('事件来源筛选无效')
        clauses, values = [], []
        for field, op, value in [('event_date','>=',start_date),('event_date','<=',end_date),('kind','=',kind),('origin','=',origin)]:
            if value:
                clauses.append(f"json_extract(payload,'$.{field}') {op} ?")
                values.append(value)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            matched = db.execute('SELECT COUNT(*) FROM events' + where, values).fetchone()[0]
            pages = max(1, (matched + page_size - 1) // page_size)
            page = min(page, pages)
            rows = db.execute("SELECT id,payload FROM events" + where +
                              " ORDER BY json_extract(payload,'$.event_date') DESC, "
                              "COALESCE(json_extract(payload,'$.occurred_at'),json_extract(payload,'$.simulation_started_at')) DESC,id DESC LIMIT ? OFFSET ?",
                              [*values, page_size, (page - 1) * page_size]).fetchall()
        return {'items': [{**json.loads(payload), 'id': ident} for ident, payload in rows],
                'page': page, 'page_size': page_size, 'pages': pages,
                'matched': matched, 'total': total, 'has_previous': page > 1, 'has_next': page < pages}

    def delete(self, event_id: int) -> bool:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
            raise ValueError('事件编号无效')
        with self.connect() as db:
            return db.execute('DELETE FROM events WHERE id=?', (event_id,)).rowcount == 1

    def eggs(self, key: str, double_probability: float) -> int:
        with self.connect() as db:
            saved = db.execute('SELECT eggs FROM cycles WHERE key=?', (key,)).fetchone()
            if saved:
                return saved[0]
            seed = db.execute("SELECT value FROM metadata WHERE key='seed'").fetchone()[0]
            roll = int.from_bytes(hashlib.sha256((seed + key).encode()).digest()[:8], 'big') / 2**64
            eggs = 2 if roll < double_probability else 1
            db.execute('INSERT OR IGNORE INTO cycles VALUES (?,?)', (key, eggs))
            return db.execute('SELECT eggs FROM cycles WHERE key=?', (key,)).fetchone()[0]


def simulate(records: list[dict], now: datetime) -> dict:
    vaginal = uterine = volume = 0.0
    internal_count = 0
    for item in records:
        volume += item['volume_ml']
        if item['kind'] != 'internal':
            continue
        internal_count += 1
        started = datetime.fromisoformat(item['simulation_started_at'])
        hours = (now - started).total_seconds() / 3600
        if hours < 0:
            continue
        vaginal += item['volume_ml'] * item['vaginal_fraction'] * math.exp2(-hours / item['vaginal_half_life_hours'])
        uterine += item['volume_ml'] * item['uterine_fraction'] * math.exp2(-hours / item['uterine_half_life_hours'])
    return {'vaginal_ml': round(vaginal, 3), 'uterine_ml': round(uterine, 3),
            'configured_volume_total_ml': round(volume, 3), 'event_count': len(records),
            'internal_count': internal_count, 'notice': SIMULATION_NOTICE,
            'actual_retained_volume': '无法从聊天或配置估算',
            'time_note': '明确时间使用该时刻；仅自述日期时，当天从记录时刻启动模型计算，往日从该日00:00启动。这是游戏计算约定。'}
