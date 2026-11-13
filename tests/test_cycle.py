import importlib.util
import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

cycle, events = module('cycle'), module('events')
STATE = {'body_cycle_enabled': True, 'body_cycle_start_date': '2026-11-01',
         'body_cycle_length_days': 28, 'body_cycle_period_days': 5}
SETTINGS = {'ejaculate_ml': 3., 'vaginal_fraction': .8, 'uterine_fraction': .05,
            'vaginal_half_life_hours': 6., 'uterine_half_life_hours': 24.}
NOW = datetime(2026, 11, 13, 18, tzinfo=ZoneInfo('Asia/Shanghai'))


class CycleTests(unittest.TestCase):
    def snapshot(self, state=None, **kwargs):
        return cycle.make_snapshot(state or STATE, 'Asia/Shanghai', today=date(2026, 11, 13), **kwargs)

    def test_28_day_boundaries(self):
        x = self.snapshot()
        self.assertEqual((x['start'], x['end'], x['next_start']), ('2026-11-01','2026-11-28','2026-11-29'))
        self.assertEqual(x['ovulation_day'], 14)
        self.assertEqual([d['day'] for d in x['days'] if d['fertile']], list(range(9, 16)))
        self.assertEqual(x['days'][13]['phase'], 'ovulation')
        self.assertEqual(x['days'][14]['phase'], 'luteal')
        self.assertEqual(x['current']['day'], 13)

    def test_all_lengths_cover_every_day_and_allow_period_overlap(self):
        for length in range(20, 46):
            x = self.snapshot({**STATE, 'body_cycle_length_days': length, 'body_cycle_period_days': 10})
            self.assertEqual(len(x['days']), length)
            self.assertTrue(all(d['phase'] in cycle.PHASES for d in x['days']))
            self.assertEqual(sum(d['menstruation'] for d in x['days']), 10)
        x = self.snapshot({**STATE, 'body_cycle_length_days': 20, 'body_cycle_period_days': 10})
        self.assertTrue(x['overlap_note'])
        self.assertTrue(x['days'][0]['fertile'])

    def test_missing_disabled_invalid_and_demo(self):
        for extra, status in [({'body_cycle_enabled':False},'disabled'), ({'body_cycle_start_date':''},'missing_date'), ({'body_cycle_start_date':'2026-02-30'},'invalid_date')]:
            x = self.snapshot({**STATE, **extra})
            self.assertEqual(x['status'], status)
            self.assertNotIn('days', x)
        self.assertEqual(self.snapshot({**STATE, 'body_cycle_start_date':''}, demo=True)['status'], 'demo')

    def test_future_anchor_never_wraps_to_fake_current_day(self):
        x = self.snapshot({**STATE, 'body_cycle_start_date':'2026-12-01'})
        self.assertTrue(x['pending'])
        self.assertIsNone(x['current'])
        self.assertFalse(x['can_previous'])
        self.assertEqual(self.snapshot(cycle_offset=-1)['status'], 'before_anchor')

    def test_cycle_rollover_and_leap_date(self):
        x = cycle.make_snapshot(STATE, 'Asia/Shanghai', today=date(2026,11,29))
        self.assertEqual(x['current']['day'], 1)
        x = cycle.make_snapshot({**STATE,'body_cycle_start_date':'2028-02-20'},'Asia/Shanghai',today=date(2028,2,29))
        self.assertEqual(x['current']['day'], 10)

    def test_invalid_input(self):
        for kwargs in [{'cycle_offset':True},{'cycle_offset':13},{'demo':'true'}]:
            with self.assertRaises(ValueError): self.snapshot(**kwargs)
        for value in [0,46,28.5,True]:
            with self.assertRaises(ValueError): self.snapshot({**STATE,'body_cycle_length_days':value})
        with self.assertRaises(ValueError): cycle.make_snapshot(STATE,'invalid/timezone')


class EventTests(unittest.TestCase):
    def message(self, text, **extra):
        return {'message_id':'msg-1', 'platform':'qq', 'message_info':{'group_info':None,'user_info':{'user_id':'100'}},
                'processed_plain_text':text, **extra}

    def test_accepts_only_explicit_self_report(self):
        for text, kind in [('我昨天发生了无保护性行为','unprotected'),('我们刚才发生了内射','internal'),('我今天没戴套进行了性行为','unprotected'),
                           ('我昨天戴了套发生了性行为','protected'),('我昨天发生了性行为','unspecified')]:
            r = events.detect_report(self.message(text), NOW, [])
            self.assertEqual(r['kind'], kind)
            self.assertNotIn('text', r)
        r = events.detect_report(self.message('我昨天发生了无保护性行为'), NOW, [])
        self.assertEqual(r['event_date'], '2026-11-12')

    def test_rejects_questions_negation_quotes_future_and_other_people(self):
        for text in ['如果我今天内射了会怎样','我昨天没有发生内射','我昨天发生了内射吗','我明天准备内射',
                     '他说我今天发生了内射','“我昨天内射了”','我上周发生了内射','我昨天未发生内射',
                     '我今天发生了内射，这只是测试','我昨天13点发生了内射','我今天无套但没有内射',
                     '我今天手机没戴套','我刚才看到有人内射了','我刚才问她内射了没']:
            self.assertIsNone(events.detect_report(self.message(text), NOW, []), text)

    def test_private_and_user_scope(self):
        m = self.message('我今天发生了内射')
        m['message_info']['group_info'] = {'group_id':'g'}
        self.assertIsNone(events.detect_report(m, NOW, []))
        self.assertIsNone(events.detect_report(self.message('我今天发生了内射'), NOW, ['200']))
        self.assertIsNone(events.detect_report(self.message('我今天发生了内射', reply_to='quoted'), NOW, []))
        self.assertIsNone(events.detect_report({'processed_plain_text':'我今天发生了内射'}, NOW, []))

    def test_storage_dedup_snapshot_delete_and_decay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = events.EventStore(Path(tmp)/'events.db')
            report = events.detect_report(self.message('我今天发生了内射'), NOW, [])
            self.assertTrue(store.add(report, SETTINGS, NOW))
            self.assertFalse(store.add(report, SETTINGS, NOW))
            rows = store.records()
            self.assertEqual(len(rows), 1)
            s = events.simulate(rows, NOW)
            self.assertEqual((s['vaginal_ml'],s['uterine_ml']), (2.4,.15))
            s = events.simulate(rows, NOW+timedelta(hours=6))
            self.assertEqual(s['vaginal_ml'], 1.2)
            self.assertLess(s['vaginal_ml']+s['uterine_ml'],3)
            self.assertEqual(store.records()[0]['volume_ml'],3)
            self.assertTrue(store.delete(rows[0]['id']))
            self.assertEqual(events.simulate(store.records(), NOW)['vaginal_ml'],0)

    def test_unprotected_without_internal_has_no_simulated_deposit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=events.EventStore(Path(tmp)/'events.db')
            store.add(events.detect_report(self.message('我今天发生了无保护性行为'), NOW, []), SETTINGS, NOW)
            s=events.simulate(store.records(), NOW)
            self.assertEqual((s['vaginal_ml'],s['uterine_ml']), (0,0))

    def test_history_paging_is_not_limited_to_latest_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=events.EventStore(Path(tmp)/'events.db')
            for n in range(205):
                day=(date(2025,1,1)+timedelta(days=n)).isoformat()
                report={'source_key':str(n),'person':'sample','event_date':day,'kind':'internal' if n%2 else 'protected',
                        'precision':'reported_date','origin':'private_self_report' if n%3 else 'manual_self_report'}
                store.add(report,SETTINGS,NOW)
            last=store.history(page=11)
            self.assertEqual((last['total'],last['matched'],last['pages'],len(last['items'])),(205,205,11,5))
            self.assertEqual(last['items'][-1]['event_date'],'2025-01-01')
            self.assertTrue(last['has_previous'])
            self.assertFalse(last['has_next'])
            first=store.history()
            self.assertEqual(first['items'][0]['event_date'],(date(2025,1,1)+timedelta(days=204)).isoformat())
            restarted=events.EventStore(Path(tmp)/'events.db')
            self.assertEqual(restarted.history()['total'],205)

    def test_history_filters_dates_type_source_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=events.EventStore(Path(tmp)/'events.db')
            for n,kind in enumerate(['internal','protected','unspecified']):
                store.add({'source_key':str(n),'person':'sample','event_date':f'2026-11-{10+n}', 'kind':kind,
                           'precision':'reported_date','origin':'manual_self_report' if n==0 else 'private_self_report'},SETTINGS,NOW)
            self.assertEqual(store.history(start_date='2026-11-10',end_date='2026-11-10')['matched'],1)
            self.assertEqual(store.history(kind='protected',origin='private_self_report')['matched'],1)
            self.assertEqual(store.history(kind='protected',origin='manual_self_report')['matched'],0)
            self.assertEqual(store.history(page=100)['page'],1)
            for args in [{'start_date':'2026-02-30'},{'start_date':'2026-11-12','end_date':'2026-11-01'},
                         {'kind':'unknown'},{'origin':'unknown'},{'page':True},{'page':0},{'page_size':101}]:
                with self.assertRaises(ValueError):store.history(**args)

    def test_late_backfill_is_sorted_by_event_date_not_insert_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=events.EventStore(Path(tmp)/'events.db')
            for n,day in enumerate(['2026-11-12','2026-11-01']):
                store.add({'source_key':str(n),'person':'sample','event_date':day,'kind':'unspecified',
                           'precision':'reported_date','origin':'manual_self_report'},SETTINGS,NOW)
            rows=store.history()['items']
            self.assertEqual(rows[0]['event_date'],'2026-11-12')
            self.assertTrue(store.delete(rows[0]['id']))
            self.assertEqual(store.history()['total'],1)

    def test_eggs_are_persistent_per_cycle_and_not_rerolled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.db';store=events.EventStore(path)
            self.assertEqual(store.eggs('cycle-a',0),1)
            self.assertEqual(store.eggs('cycle-b',1),2)
            self.assertEqual(events.EventStore(path).eggs('cycle-a',1),1)
            self.assertEqual(store.eggs('cycle-b',0),2)


if __name__ == '__main__': unittest.main()
