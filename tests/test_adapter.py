import importlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import tempfile
import time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

from maibot_sdk.context import PluginContext

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('cycle_adapter_tests',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
pkg=importlib.util.module_from_spec(spec);sys.modules[spec.name]=pkg;spec.loader.exec_module(pkg)
adapter=importlib.import_module('cycle_adapter_tests.plugin')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_sdk_normalized_config_and_timezone(self):
        state={'body_cycle_enabled':True,'body_cycle_start_date':'','body_cycle_length_days':28,'body_cycle_period_days':5}
        class Context:
            async def call_capability(self,capability,**args):
                raw={'success':True,'value':state if args['key']=='state' else 'Asia/Shanghai'}
                return PluginContext._normalize_capability_result(capability,raw)
        instance=SimpleNamespace(ctx=Context())
        self.assertEqual(await adapter.MaiLifeCycle._read(instance,'state'),state)
        self.assertEqual(await adapter.MaiLifeCycle._read(instance,'environment.timezone'),'Asia/Shanghai')

    async def test_unavailable_dependency_stays_an_error(self):
        class Context:
            async def call_capability(self,*args,**kwargs):return {'success':False,'error':'not loaded'}
        with self.assertRaises(RuntimeError):await adapter.MaiLifeCycle._read(SimpleNamespace(ctx=Context()),'state')

    def test_configuration_invariants(self):
        with self.assertRaises(ValueError):adapter.SimulationSettings(vaginal_fraction=.9,uterine_fraction=.3)
        with self.assertRaises(ValueError):adapter.SimulationSettings(ejaculate_ml=-1)
        self.assertEqual(adapter.Settings().plugin.config_version,'1.0.0')


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=adapter.EventStore(Path(self.tmp.name)/'events.db');adapter.pregnancy.initialize(self.store)
        now=datetime.now(ZoneInfo('Asia/Shanghai'))
        adapter.pregnancy.set_manual(self.store,(now.date()-timedelta(days=70)).isoformat(),1,'安静的角色日常',now)
        async def read(key):
            return [{'user_id':'owner','enabled':True,'role':'owner'}] if key=='users.profiles' else 'Asia/Shanghai'
        self.instance=SimpleNamespace(config=adapter.Settings(),store=self.store,_read=read,_private_sessions={})
        async def allowed(user_id,platform,ids):return await adapter.MaiLifeCycle._allowed_actor(self.instance,user_id,platform,ids)
        self.instance._allowed_actor=allowed
        async def context(session):return await adapter.MaiLifeCycle._pregnancy_context(self.instance,session)
        self.instance._pregnancy_context=context
    async def asyncTearDown(self):self.tmp.cleanup()

    def remember(self,user='owner',group=None,session='s'):
        adapter.MaiLifeCycle._remember_private_session(self.instance,{'session_id':session,'platform':'qq','message_info':{'group_info':group,'user_info':{'user_id':user}}})

    async def test_only_authorized_private_session_gets_character_state(self):
        self.remember();text=await self.instance._pregnancy_context('s');self.assertIn('孕周10周',text)
        self.assertNotIn('mL',text)
        self.remember(group={'group_id':'g'});self.assertEqual(await self.instance._pregnancy_context('s'),'')
        self.remember(user='other');self.assertEqual(await self.instance._pregnancy_context('s'),'')
        self.instance.config.pregnancy.context_user_ids=['other'];self.assertIn('孕周',await self.instance._pregnancy_context('s'))
        self.instance.config.pregnancy.context_enabled=False;self.assertEqual(await self.instance._pregnancy_context('s'),'')

    async def test_both_context_hooks_preserve_existing_content_and_deduplicate(self):
        self.remember()
        original=[{'item_type':'SystemMessageItem','meta':{'item_id':'original'},'parts':[{'type':'text','text':'original'}]}]
        args={'items':original,'item_schema_version':1,'tool_definitions':[{'name':'reply'}],'session_id':'s'}
        result=await adapter.MaiLifeCycle.pregnancy_planner(self.instance,**args)
        self.assertEqual(len(original),1)
        self.assertEqual(len(result['modified_kwargs']['items']),2)
        again=await adapter.MaiLifeCycle.pregnancy_planner(self.instance,**result['modified_kwargs'])
        self.assertEqual(len(again['modified_kwargs']['items']),2)
        skipped=await adapter.MaiLifeCycle.pregnancy_planner(self.instance,**{**args,'tool_definitions':[]})
        self.assertNotIn('modified_kwargs',skipped)
        reply=await adapter.MaiLifeCycle.pregnancy_replyer(self.instance,session_id='s',extra_prompt='existing life state')
        self.assertTrue(reply['modified_kwargs']['extra_prompt'].startswith('existing life state'))
        reply2=await adapter.MaiLifeCycle.pregnancy_replyer(self.instance,**reply['modified_kwargs'])
        self.assertEqual(reply2['modified_kwargs']['extra_prompt'].count('[local.mailife-cycle.pregnancy]'),1)

    async def test_ending_clears_pregnancy_in_future_context(self):
        self.remember();adapter.pregnancy.end(self.store,'ended',datetime.now(ZoneInfo('Asia/Shanghai')))
        self.assertIn('当前不在妊娠',await self.instance._pregnancy_context('s'))
