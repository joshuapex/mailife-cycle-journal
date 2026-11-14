import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


events, journal = load('events'), load('journal')
NOW = datetime(2026,11,15,1,tzinfo=timezone(timedelta(hours=8)))
SETTINGS = {'ejaculate_ml':3.,'vaginal_fraction':.8,'uterine_fraction':.05,
            'vaginal_half_life_hours':6.,'uterine_half_life_hours':24.}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = events.EventStore(Path(self.tmp.name)/'events.db')
        journal.initialize(self.store)
        self.actor = journal.actor_key('qq','user','session')

    def tearDown(self):
        self.tmp.cleanup()

    def decision(self, **extra):
        return {'status':'completed','event_id':'new','kind':'unspecified',
                'event_date':'2026-11-15',**extra}

    def apply(self, source='one', existing=None, **extra):
        return journal.apply(self.store,source,self.actor,self.decision(**extra),SETTINGS,NOW,existing or [])

    def test_pending_then_completion_then_aftercare_is_one_event(self):
        self.apply(status='pending')
        self.assertEqual(self.store.records(),[])
        self.apply('two')
        existing = journal.recent_events(self.store,self.actor,NOW,30)
        result = self.apply('three',existing,event_id=existing[0]['id'],kind='internal')
        self.assertEqual(result['reason'],'updated')
        self.assertEqual(len(self.store.records()),1)
        self.assertEqual(self.store.records()[0]['kind'],'internal')
        self.assertEqual(self.apply('four',existing,event_id=existing[0]['id'],kind='internal')['reason'],'duplicate')
        self.assertEqual(len(self.store.records()),1)
        self.assertEqual(journal.diagnostics(self.store)[-1]['reason'],'pending')

    def test_new_round_requires_explicit_evidence(self):
        self.apply()
        existing = journal.recent_events(self.store,self.actor,NOW,30)
        self.assertEqual(self.apply('two',existing)['reason'],'ambiguous')
        self.apply('three',existing,explicit_new_round=True)
        self.assertEqual(len(self.store.records()),2)

    def test_replay_and_concurrent_commit_are_idempotent(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:self.apply(),range(8)))
        self.assertEqual(sum(r['reason']=='recorded' for r in results),1)
        self.assertEqual(len(self.store.records()),1)
        restarted = events.EventStore(self.store.path)
        self.assertEqual(journal.apply(restarted,'one',self.actor,self.decision(),SETTINGS,NOW,[])['reason'],'duplicate')

    def test_deleted_event_does_not_resurrect_or_reuse_id(self):
        self.apply()
        ident = self.store.records()[0]['id']
        self.store.delete(ident)
        existing = journal.recent_events(self.store,self.actor,NOW,30)
        self.assertTrue(existing[0]['deleted'])
        self.assertEqual(self.apply('two',existing,event_id=ident)['reason'],'deleted')
        self.apply('three',existing,explicit_new_round=True)
        self.assertGreater(self.store.records()[0]['id'],ident)
        self.assertEqual(self.apply('four',existing,event_id=ident)['reason'],'deleted')

    def test_manual_add_never_reuses_deleted_journal_id(self):
        self.apply()
        ident = self.store.records()[0]['id']
        self.store.delete(ident)
        report={'source_key':'manual','person':'manual','kind':'protected','origin':'manual_self_report',
                'event_date':'2026-11-15','precision':'reported_date'}
        self.store.add(report,SETTINGS,NOW)
        self.assertGreater(self.store.records()[0]['id'],ident)

    def test_isolation_and_expiration(self):
        self.apply()
        self.assertEqual(journal.recent_events(self.store,'different',NOW,30),[])
        self.assertEqual(journal.recent_events(self.store,self.actor,NOW+timedelta(hours=1),30),[])
        ident = self.store.records()[0]['id']
        with self.assertRaises(ValueError):
            journal.apply(self.store,'two','different',self.decision(event_id=ident),SETTINGS,NOW,[{'id':ident}])

    def test_bot_only_old_or_invented_evidence_is_rejected(self):
        messages=[{'role':'user','text':'之前的陈述'},{'role':'assistant','text':'描述'},
                  {'role':'user','text':'结束了，刚才那次'}]
        for evidence in [[],[1],[0],[-1],[True],[9],['2']]:
            with self.subTest(evidence=evidence),self.assertRaises(ValueError):
                journal.validate({**self.decision(),'user_evidence':evidence},messages,[],NOW)
        good=journal.validate({**self.decision(),'user_evidence':[2]},messages,[],NOW)
        self.assertEqual(good['status'],'completed')
        with self.assertRaises(ValueError):
            journal.validate({**self.decision(event_id=99),'user_evidence':[2]},messages,[],NOW)

    def test_future_questions_and_malformed_model_output_fail_closed(self):
        for text in ['我明天准备同房','如果刚才发生了关系','内射了吗？','他说刚才发生了关系','这是测试']:
            parsed=journal.validate({**self.decision(),'user_evidence':[0]},[{'role':'user','text':text}],[],NOW)
            self.assertNotEqual(parsed['status'],'completed')
        for raw in ['not json',[],{},self.decision(event_date='2026-11-16',user_evidence=[0]),self.decision(kind='invented',user_evidence=[0])]:
            with self.subTest(raw=raw),self.assertRaises((ValueError,TypeError)):
                journal.validate(raw,[{'role':'user','text':'刚才结束了'}],[],NOW)

    def test_type_correction_preserves_snapshot_and_source(self):
        self.apply(kind='internal')
        original=self.store.records()[0]
        existing=journal.recent_events(self.store,self.actor,NOW,30)
        self.apply('two',existing,event_id=original['id'],kind='protected')
        updated=self.store.records()[0]
        self.assertEqual(updated['source_key'],original['source_key'])
        self.assertEqual(updated['volume_ml'],original['volume_ml'])
        self.assertEqual(events.simulate([updated],NOW)['internal_count'],0)
        self.assertEqual(self.store.history(origin='chat_inferred')['matched'],1)

    def test_diagnostics_are_bounded_and_text_free(self):
        for i in range(510):
            journal.diagnose(self.store,str(i),self.actor,'model_error',NOW)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM journal_decisions').fetchone()[0],500)
        rows=journal.diagnostics(self.store)
        self.assertEqual(len(rows),30)
        self.assertEqual(set(rows[0]),{'at','reason','label'})
        self.assertNotIn(self.actor,json.dumps(rows))

    def test_manual_correction_is_protected_from_model_and_validated(self):
        self.apply(kind='internal')
        row=self.store.records()[0]
        self.store.correct(row['id'],'protected','2026-11-14',NOW)
        corrected=self.store.records()[0]
        self.assertEqual(corrected['event_date'],'2026-11-14')
        existing=journal.recent_events(self.store,self.actor,NOW,30)
        self.apply('two',existing,event_id=row['id'],kind='internal',event_date='2026-11-14')
        self.assertEqual(self.store.records()[0]['kind'],'protected')
        for ident,kind,day in [(True,'internal','2026-11-15'),(row['id'],'wrong','2026-11-15'),(row['id'],'internal','2026-11-16')]:
            with self.assertRaises(ValueError):self.store.correct(ident,kind,day,NOW)


if __name__=='__main__':
    unittest.main()
