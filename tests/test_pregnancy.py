import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
events,pregnancy=load('events'),load('pregnancy')
NOW=datetime(2026,11,13,12,tzinfo=ZoneInfo('Asia/Shanghai'))
SETTINGS={'auto_trigger_enabled':True,'trigger_probability':1.,'max_event_age_days':7}
CYCLE={'status':'ready','start':'2026-11-01','current':{'fertile':True}}


class PregnancyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=events.EventStore(Path(self.tmp.name)/'events.db');pregnancy.initialize(self.store)
    def tearDown(self):self.tmp.cleanup()
    def report(self,key='a',kind='internal',day='2026-11-13'):
        return {'source_key':key,'kind':kind,'event_date':day}

    def test_trigger_and_persistence_and_no_repeat(self):
        a=pregnancy.attempt(self.store,self.report(),CYCLE,SETTINGS,NOW)
        self.assertTrue(a['triggered'])
        b=pregnancy.attempt(self.store,self.report(),CYCLE,{**SETTINGS,'trigger_probability':0},NOW)
        self.assertEqual(a,b)
        new_store=events.EventStore(self.store.path);pregnancy.initialize(new_store)
        self.assertEqual(len(pregnancy.snapshot(new_store,NOW)['history']),1)
        c=pregnancy.attempt(self.store,self.report('b'),CYCLE,SETTINGS,NOW)
        self.assertEqual(c['outcome'],'already_ongoing')

    def test_failed_draw_does_not_reroll_after_probability_change(self):
        a=pregnancy.attempt(self.store,self.report(),CYCLE,{**SETTINGS,'trigger_probability':0},NOW)
        self.assertEqual(a['outcome'],'not_triggered')
        b=pregnancy.attempt(self.store,self.report(),CYCLE,SETTINGS,NOW)
        self.assertFalse(b['triggered'])
        self.assertIsNone(pregnancy.snapshot(self.store,NOW)['current'])

    def test_eligibility_private_record_type_window_old_and_disabled(self):
        cases=[(self.report('a','protected'),CYCLE,SETTINGS,'not_internal'),
               (self.report('b','unprotected'),CYCLE,SETTINGS,'not_internal'),
               (self.report('c'),{**CYCLE,'current':{'fertile':False}},SETTINGS,'outside_window_or_unconfigured'),
               (self.report('d'),{'status':'missing_date'},SETTINGS,'outside_window_or_unconfigured'),
               (self.report('e'),CYCLE,{**SETTINGS,'auto_trigger_enabled':False},'auto_disabled'),
               (self.report('f',day='2025-11-13'),CYCLE,SETTINGS,'historical_backfill_or_future'),
               ({**self.report('g'),'eligible_actor':False},CYCLE,SETTINGS,'unauthorized_actor')]
        for report,cycle,settings,outcome in cases:
            self.assertEqual(pregnancy.attempt(self.store,report,cycle,settings,NOW)['outcome'],outcome)
        self.assertIsNone(pregnancy.snapshot(self.store,NOW)['current'])

    def test_stage_boundaries_due_date_and_no_automatic_delivery(self):
        anchor='2026-01-01'
        for days,phase in [(97,'孕早期'),(98,'孕中期'),(195,'孕中期'),(196,'孕晚期'),(280,'待登记分娩')]:
            now=datetime(2026,1,1,12,tzinfo=NOW.tzinfo)+timedelta(days=days)
            pregnancy.set_manual(self.store,anchor,2,'自定义角色状态',now)
            d=pregnancy.snapshot(self.store,now)
            self.assertEqual(d['current']['phase'],phase)
            self.assertEqual(d['current']['gestational_days'],days)
            self.assertEqual(d['current']['due_date'],'2026-10-08')
            self.assertEqual(d['current']['status'],'active')
            self.assertTrue(d['calendar_paused'])

    def test_override_delivery_postpartum_and_end_are_archived(self):
        pregnancy.set_manual(self.store,'2026-11-01',1,'',NOW)
        pregnancy.set_manual(self.store,'2026-10-01',2,'只作角色备注',NOW)
        self.assertEqual(len(pregnancy.snapshot(self.store,NOW)['history']),2)
        pregnancy.end(self.store,'delivered',NOW)
        d=pregnancy.snapshot(self.store,NOW+timedelta(days=4))
        self.assertEqual(d['current']['postpartum_days'],4)
        self.assertTrue(d['calendar_paused'])
        pregnancy.end(self.store,'ended',NOW+timedelta(days=4))
        d=pregnancy.snapshot(self.store,NOW+timedelta(days=4))
        self.assertIsNone(d['current']);self.assertFalse(d['calendar_paused'])
        self.assertIn('已结束',pregnancy.context_text(d))

    def test_invalid_manual_inputs_and_context_is_character_data(self):
        for args in [('2026-11-14',1,''),('2026-02-30',1,''),('2026-11-01',3,''),('2026-11-01',True,''),('2026-11-01',1,'x'*301)]:
            with self.assertRaises(ValueError):pregnancy.set_manual(self.store,*args,NOW)
        pregnancy.set_manual(self.store,'2026-11-01',1,'更喜欢安静的日常',NOW)
        text=pregnancy.context_text(pregnancy.snapshot(self.store,NOW))
        self.assertIn('角色扮演背景',text)
        self.assertIn('不是用户的身体状态',text)
        self.assertNotIn('source_key',text)
        self.assertNotIn('mL',text)


if __name__=='__main__':unittest.main()
