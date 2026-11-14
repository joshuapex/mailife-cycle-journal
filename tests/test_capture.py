"""Integration with the real SDK types; model responses are offline fixtures."""
import asyncio
from collections import OrderedDict, deque
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
# Reuse the package name installed by the existing SDK adapter tests.
spec=importlib.util.spec_from_file_location('journal_capture_tests',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
pkg=importlib.util.module_from_spec(spec);sys.modules[spec.name]=pkg;spec.loader.exec_module(pkg)
adapter=importlib.import_module('journal_capture_tests.plugin')


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=adapter.EventStore(Path(self.tmp.name)/'events.db')
        adapter.journal.initialize(self.store);adapter.pregnancy.initialize(self.store)
        self.calls=[]
        self.response={'status':'completed','event_id':'new','kind':'unspecified',
                       'event_date':datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),'user_evidence':[0]}
        async def generate(**kwargs):
            self.calls.append(kwargs)
            return {'success':True,'response':json.dumps(self.response)}
        async def read(key):
            return {'state':{'body_cycle_enabled':True,'body_cycle_start_date':'2026-11-01','body_cycle_length_days':28,'body_cycle_period_days':5},
                    'environment.timezone':'Asia/Shanghai','users.profiles':[{'user_id':'u','role':'owner','enabled':True}]}[key]
        logger=SimpleNamespace(info=lambda *a:None,warning=lambda *a:None)
        self.p=SimpleNamespace(store=self.store,config=adapter.Settings(),_private_sessions={},_contexts=OrderedDict(),
            _journal_queue=asyncio.Queue(maxsize=2),_journal_notices=deque(maxlen=30),_read=read,_journal_epoch=0,
            ctx=SimpleNamespace(llm=SimpleNamespace(generate=generate),logger=logger))
        for name in ['_remember_private_session','_queue_context_message','_analyze_context','_settings','_allowed_actor','_journal_loop']:
            setattr(self.p,name,MethodType(getattr(adapter.MaiLifeCycle,name),self.p))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def message(self,ident='1',text='刚才我们结束了性行为',user='u',session='s',**extra):
        return {'session_id':session,'platform':'qq','message_id':ident,'processed_plain_text':text,
                'message_info':{'user_info':{'user_id':user},'group_info':None},**extra}

    async def receive(self,**kwargs):
        await adapter.MaiLifeCycle.capture(self.p,self.message(**kwargs))

    async def test_receive_only_enqueues_and_sent_bot_only_supplies_context(self):
        await self.receive()
        self.assertFalse(self.calls)
        job=self.p._journal_queue.get_nowait()
        await adapter.MaiLifeCycle.capture_sent_context(self.p,self.message('b','成功发送的回复','bot'),sent=False)
        self.assertEqual(len(next(iter(self.p._contexts.values()))),1)
        await adapter.MaiLifeCycle.capture_sent_context(self.p,self.message('b','成功发送的回复','bot'),sent=True)
        self.assertTrue(self.p._journal_queue.empty())
        self.assertEqual(next(iter(self.p._contexts.values()))[-1]['role'],'assistant')
        await self.p._analyze_context(job)
        self.assertEqual(len(self.store.records()),1)
        self.assertEqual(self.calls[0]['task_name'],'planner')

    async def test_whitelist_group_quotes_notify_and_truncation(self):
        self.p.config.simulation.allowed_user_ids=['owner']
        await self.receive()
        self.assertTrue(self.p._journal_queue.empty())
        self.p.config.simulation.allowed_user_ids=[]
        for extra in [{'reply_to':'quoted'},{'is_notify':True},{'processed_plain_text':'x'*1001}]:
            m=self.message(**{})
            m.update(extra)
            await adapter.MaiLifeCycle.capture(self.p,m)
        m=self.message();m['message_info']['group_info']={'group_id':'g'}
        await adapter.MaiLifeCycle.capture(self.p,m)
        self.assertTrue(self.p._journal_queue.empty())

    async def test_model_failure_bad_json_and_epoch_change_do_not_write(self):
        await self.receive()
        job=self.p._journal_queue.get_nowait()
        async def bad(**kw):return {'success':False}
        self.p.ctx.llm.generate=bad
        await self.p._analyze_context(job)
        self.assertEqual(adapter.journal.diagnostics(self.store)[0]['reason'],'model_error')
        await self.receive(ident='2')
        job=self.p._journal_queue.get_nowait()
        async def malformed(**kw):return {'success':True,'response':'invalid json'}
        self.p.ctx.llm.generate=malformed
        await self.p._analyze_context(job)
        self.assertEqual(adapter.journal.diagnostics(self.store)[0]['reason'],'invalid_response')
        await self.receive(ident='3')
        job=self.p._journal_queue.get_nowait()
        async def changing(**kw):
            self.p._journal_epoch+=1
            return {'success':True,'response':json.dumps(self.response)}
        self.p.ctx.llm.generate=changing
        await self.p._analyze_context(job)
        self.assertEqual(self.store.records(),[])

    async def test_queue_overflow_and_worker_cancellation(self):
        for ident in ['1','2','3']:await self.receive(ident=ident)
        self.assertEqual(self.p._journal_notices[-1]['reason'],'queue_full')
        entered=asyncio.Event()
        async def wait(**kw):
            entered.set();await asyncio.Event().wait()
        self.p.ctx.llm.generate=wait
        worker=asyncio.create_task(self.p._journal_loop())
        await asyncio.wait_for(entered.wait(),1)
        worker.cancel()
        await asyncio.gather(worker,return_exceptions=True)
        self.assertEqual(self.store.records(),[])

    async def test_completed_internal_replay_draws_only_once(self):
        self.response['kind']='internal'
        await self.receive()
        job=self.p._journal_queue.get_nowait()
        await self.p._analyze_context(job)
        await self.p._analyze_context(job)
        self.assertEqual(len(self.store.records()),1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM pregnancy_checks').fetchone()[0],1)


if __name__=='__main__':unittest.main()
