"""Read-only dependency adapter. Never imports or edits MaiLife source files."""
from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pydantic import model_validator
from maibot_sdk import API, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from .cycle import make_snapshot
from .events import EventStore, detect_report, simulate
from . import pregnancy

MAILIFE_ID = "maibot-community.mai-life"


class PluginSettings(PluginConfigBase):
    enabled: bool = Field(default=True, description="启用周期日历")
    config_version: str = "1.0.0"


class SimulationSettings(PluginConfigBase):
    __ui_label__ = "身体状态与事件"
    capture_private_reports: bool = Field(default=True, description="识别私聊中第一人称、已发生的明确自述；忽略疑问、否定、假设、引用和群聊。不保存原始聊天文本。")
    allowed_user_ids: list[str] = Field(default_factory=list, description="可记录的私聊用户ID；空列表允许所有私聊用户。建议填写参与该角色状态的用户ID。")
    ejaculate_ml: float = Field(default=3.0, ge=0, le=20, description="每次射出量（mL，配置值），不是实际测量。修改只影响新事件。")
    vaginal_fraction: float = Field(default=0.8, ge=0, le=1, description="阴道内留存量初始分配比例，无生理学依据。与子宫内留存量比例之和不得超过1。")
    uterine_fraction: float = Field(default=0.05, ge=0, le=1, description="子宫内留存量初始分配比例，无生理学依据，不代表真实精液进入子宫。")
    vaginal_half_life_hours: float = Field(default=6, ge=0.1, le=168, description="阴道内留存量衰减半衰期（小时），为数值衰减参数，不是精子存活时间。")
    uterine_half_life_hours: float = Field(default=24, ge=0.1, le=168, description="子宫内留存量衰减半衰期（小时），为数值衰减参数，不代表生理过程。")
    double_ovulation_probability: float = Field(default=0.05, ge=0, le=1, description="双卵生成概率，默认5%为人为设定参数，不是人群发生率；设为0固定单卵。每周期首次查看抽取并持久保存。")

    @model_validator(mode='after')
    def validate_fractions(self):
        if self.vaginal_fraction + self.uterine_fraction > 1:
            raise ValueError('两个留存模型的分配比例之和不能大于1')
        return self


class Settings(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)
    pregnancy: 'PregnancySettings' = Field(default_factory=lambda: PregnancySettings())


class PregnancySettings(PluginConfigBase):
    __ui_label__ = '妊娠状态'
    auto_trigger_enabled: bool = Field(default=True, description='新记录的明确内射事件位于估算易孕窗口时触发一次角色状态抽样，不回溯既有事件。')
    trigger_probability: float = Field(default=0.2, ge=0, le=1, description='每条符合条件事件的妊娠登记概率，默认20%是配置参数，不是真实受孕率；修改不重抽历史事件。')
    max_event_age_days: int = Field(default=7, ge=0, le=30, description='自动抽样只处理最近多少天内发生的新记录，默认7天；更早的补记仅保存历史，避免旧事件意外改变当前剧情。')
    trigger_user_ids: list[str] = Field(default_factory=list, description='可通过新私聊自述触发妊娠抽样的QQ用户；空列表时仅麦生活已启用的owner。管理员手动补记不受此列表限制。')
    context_enabled: bool = Field(default=True, description='向授权QQ私聊的Planner和Replyer注入简短的角色妊娠状态，不向群聊注入，不含性行为记录或模型计算留存量。')
    context_user_ids: list[str] = Field(default_factory=list, description='允许接收角色妊娠上下文的QQ用户ID；空列表时仅麦生活中已启用的owner用户。')


Settings.model_rebuild()


class MaiLifeCycle(MaiBotPlugin):
    config_model = Settings

    async def on_load(self):
        self.store = EventStore(self.ctx.paths.data_dir / "cycle_events.sqlite3")
        await asyncio.to_thread(pregnancy.initialize, self.store)
        self._private_sessions = {}
        self.ctx.logger.info("麦生活周期日历已加载：周期只读，事件与数值模型独立存储")

    async def on_unload(self):
        self._private_sessions.clear()

    async def on_config_update(self, scope, config_data, version):
        pass

    def _remember_private_session(self, message):
        if not isinstance(message,dict):
            return
        session = str(message.get('session_id') or '')
        info = message.get('message_info')
        if not session or not isinstance(info,dict):
            return
        if info.get('group_info') is not None:
            self._private_sessions.pop(session,None)
            return
        user = info.get('user_info') or {}
        ident = str(user.get('user_id') or '')
        if not ident:
            return
        self._private_sessions[session] = {'user_id':ident,'platform':str(message.get('platform') or info.get('platform') or ''),'seen':time.monotonic()}
        if len(self._private_sessions)>512:
            self._private_sessions.pop(next(iter(self._private_sessions)))

    async def _record(self, report, settings, now):
        await asyncio.to_thread(self.store.add, report, settings, now)
        config = self.config.pregnancy.model_dump()
        state = await self._read('state')
        cycle = make_snapshot(state, str(now.tzinfo), today=datetime.fromisoformat(report['event_date']).date())
        return await asyncio.to_thread(pregnancy.attempt,self.store,report,cycle,config,now)

    async def _allowed_actor(self, user_id, platform, allowed):
        if platform!='qq':
            return False
        if allowed:
            return user_id in allowed
        profiles = await self._read('users.profiles')
        return isinstance(profiles,list) and any(str(p.get('user_id'))==user_id and p.get('enabled',True) and p.get('role')=='owner' for p in profiles if isinstance(p,dict))

    async def _pregnancy_context(self, session):
        if not self.config.plugin.enabled or not self.config.pregnancy.context_enabled:
            return ''
        actor = self._private_sessions.get(session)
        if not actor or actor['platform']!='qq' or time.monotonic()-actor['seen']>86400:
            return ''
        if not await self._allowed_actor(actor['user_id'],actor['platform'],self.config.pregnancy.context_user_ids):
            return ''
        timezone = await self._read('environment.timezone')
        current = await asyncio.to_thread(pregnancy.snapshot,self.store,datetime.now(ZoneInfo(timezone)))
        return pregnancy.context_text(current)

    @HookHandler('maisaka.planner.before_request',mode=HookMode.BLOCKING,order=HookOrder.LATE,error_policy=ErrorPolicy.SKIP)
    async def pregnancy_planner(self, **kwargs):
        if kwargs.get('item_schema_version')!=1 or not isinstance(kwargs.get('items'),list):
            return {'action':'continue'}
        if isinstance(kwargs.get('tool_definitions'),list) and not kwargs['tool_definitions']:
            return {'action':'continue'}
        text = await self._pregnancy_context(str(kwargs.get('session_id') or ''))
        if not text:
            return {'action':'continue'}
        marker = 'local.mailife-cycle.pregnancy'
        items = [item for item in kwargs['items'] if not isinstance(item,dict) or (item.get('meta') or {}).get('item_id')!=marker]
        items.append({'item_type':'SystemMessageItem','meta':{'item_id':marker,'logical_turn_id':None,'timestamp':datetime.now().isoformat()},'parts':[{'type':'text','text':text}]})
        return {'action':'continue','modified_kwargs':{**kwargs,'items':items}}

    @HookHandler('maisaka.replyer.before_request',mode=HookMode.BLOCKING,order=HookOrder.LATE,error_policy=ErrorPolicy.SKIP)
    async def pregnancy_replyer(self, **kwargs):
        text = await self._pregnancy_context(str(kwargs.get('session_id') or ''))
        if not text:
            return {'action':'continue'}
        marker = '\n[local.mailife-cycle.pregnancy]\n'
        base = str(kwargs.get('extra_prompt') or '').split(marker)[0]
        return {'action':'continue','modified_kwargs':{**kwargs,'extra_prompt':base+marker+text}}

    def _settings(self):
        settings = self.config.simulation.model_dump()
        if settings['vaginal_fraction'] + settings['uterine_fraction'] > 1:
            raise ValueError("两个留存模型的分配比例之和不能大于1，请修改扩展的角色状态设置")
        return settings

    async def _read(self, key):
        result = await self.ctx.call_capability("config.get_plugin", plugin_name=MAILIFE_ID, key=key)
        # SDK 2.19 unwraps Host's {success, value} envelope automatically.
        # Also accept an unnormalized envelope for host/SDK compatibility.
        if result is None or (isinstance(result, dict) and result.get('success') is False):
            raise RuntimeError("未能读取麦生活配置，请确认麦生活已启用且依赖版本满足要求")
        if isinstance(result, dict) and result.get('success') is True and 'value' in result:
            return result['value']
        return result

    @staticmethod
    def _event_view(rows, state, timezone, now):
        view = [{k: v for k, v in r.items() if k != 'source_key'} for r in rows]
        for row in view:
            row['phase_at_event'] = '未配置周期'
            if state.get('body_cycle_start_date'):
                event = make_snapshot(state, timezone, today=datetime.fromisoformat(row['event_date']).date())
                if event.get('current'):
                    row['phase_at_event'] = event['current']['phase_label'] + (' · 经期' if event['current']['menstruation'] else '')
            row['simulation_now'] = simulate([row], now)
        return view

    @API(name='event_history', description='性行为自述历史：筛选、分页与详情', version='1', public=False)
    async def event_history(self, page: int = 1, start_date: str = '', end_date: str = '', kind: str = '', origin: str = ''):
        try:
            result = await asyncio.to_thread(self.store.history, page=page, start_date=start_date, end_date=end_date, kind=kind, origin=origin)
            state, timezone = await asyncio.gather(self._read('state'), self._read('environment.timezone'))
            now = datetime.now(ZoneInfo(timezone))
            result['items'] = self._event_view(result['items'], state, timezone, now)
            result['timezone'] = timezone
            return {'success': True, 'data': result}
        except (ValueError, RuntimeError) as exc:
            return {'success': False, 'error': str(exc)}

    @API(name="cycle_snapshot", description="周期日历只读快照", version="1", public=False)
    async def cycle_snapshot(self, cycle_offset: int = 0, demo: bool = False):
        try:
            state, timezone = await asyncio.gather(self._read("state"), self._read("environment.timezone"))
            data = make_snapshot(state, timezone, cycle_offset, demo)
            now = datetime.now(ZoneInfo(timezone))
            settings = self._settings()
            records = await asyncio.to_thread(self.store.records)
            summary = simulate(records, now)
            pregnant = await asyncio.to_thread(pregnancy.snapshot,self.store,now)
            if data['status'] == 'ready' and not pregnant['calendar_paused']:
                key = f"{data['start']}:{data['length']}"
                eggs = await asyncio.to_thread(self.store.eggs, key, settings['double_ovulation_probability'])
                data['eggs'] = {'cycle_total': eggs, 'released_today': eggs if (data.get('current') or {}).get('phase') == 'ovulation' else 0,
                                'label': '本周期排卵数（设定）；不代表实际卵子数。每日不重抽，不推断卵巢储备。'}
            # Never expose message IDs or original text in the page response.
            view = self._event_view(records[:200], state, timezone, now)
            counts = {}
            for record in records:
                if data['status'] != 'ready' or not data['start'] <= record['event_date'] <= data['end']:
                    continue
                item = counts.setdefault(record['event_date'], {'count': 0, 'internal': 0})
                item['count'] += 1
                item['internal'] += int(record['kind'] == 'internal')
            data.update({'simulation': summary, 'events': view, 'simulation_settings': settings,
                         'events_truncated': len(records) > 200, 'calendar_counts': counts,
                         'pregnancy':pregnant,'pregnancy_settings':self.config.pregnancy.model_dump()})
            return {"success": True, "data": data}
        except (ValueError, RuntimeError) as exc:
            return {"success": False, "error": str(exc)}
        except Exception:
            self.ctx.logger.exception("周期日历读取失败")
            return {"success": False, "error": "周期数据暂不可用，请检查麦生活运行状态后重试"}

    @HookHandler("chat.receive.after_process", mode=HookMode.BLOCKING, error_policy=ErrorPolicy.SKIP)
    async def capture(self, message=None, **kwargs):
        self._remember_private_session(message)
        if not self.config.plugin.enabled or not self.config.simulation.capture_private_reports:
            return {"action": "continue"}
        if not isinstance(message, dict) or not any(word in str(message.get('processed_plain_text') or '') for word in
                ('内射', '射精', '射在', '无保护', '避孕', '无套', '戴套', '性行为', '性交', '同房', '做爱', '发生关系', '发生了关系')):
            return {"action": "continue"}
        try:
            settings = self._settings()
            timezone = await self._read('environment.timezone')
            now = datetime.now(ZoneInfo(timezone))
            report = detect_report(message, now, settings['allowed_user_ids'])
            if report:
                actor=(message.get('message_info') or {}).get('user_info') or {}
                report['eligible_actor'] = await self._allowed_actor(str(actor.get('user_id') or ''),str(message.get('platform') or ''),self.config.pregnancy.trigger_user_ids)
                await self._record(report,settings,now)
        except Exception:
            # Avoid logging the message, user identity, or intimate event content.
            self.ctx.logger.warning("周期事件未记录，请检查扩展设置或存储状态")
        return {"action": "continue"}

    @API(name="record_event", description="管理员手动补记自述事件", version="1", public=False)
    async def record_event(self, occurred_at: str, kind: str):
        try:
            if kind not in {'internal', 'unprotected', 'protected', 'unspecified'}:
                raise ValueError('事件类型无效')
            if not isinstance(occurred_at, str) or len(occurred_at) > 32:
                raise ValueError('事件时间格式无效')
            timezone = await self._read('environment.timezone')
            now = datetime.now(ZoneInfo(timezone))
            at = datetime.fromisoformat(occurred_at)
            if at.tzinfo is None:
                at = at.replace(tzinfo=now.tzinfo)
            at = at.astimezone(now.tzinfo)
            if at > now:
                raise ValueError('不能记录尚未发生的事件')
            report = {'source_key': secrets.token_hex(24), 'person': '管理员补记', 'event_date': at.date().isoformat(),
                      'occurred_at': at.isoformat(), 'kind': kind, 'precision': 'exact', 'origin': 'manual_self_report'}
            await self._record(report,self._settings(),now)
            return {'success': True}
        except (ValueError, RuntimeError) as exc:
            return {'success': False, 'error': str(exc)}

    @API(name="delete_event", description="删除错误或不需要的事件", version="1", public=False)
    async def delete_event(self, event_id: int):
        return {'success': await asyncio.to_thread(self.store.delete, event_id)}

    @API(name='pregnancy_set',description='手动开始或覆盖妊娠状态',version='1',public=False)
    async def pregnancy_set(self, anchor_date: str, fetuses: int = 1, note: str = ''):
        try:
            timezone = await self._read('environment.timezone')
            ident = await asyncio.to_thread(pregnancy.set_manual,self.store,anchor_date,fetuses,note,datetime.now(ZoneInfo(timezone)))
            return {'success':True,'id':ident}
        except ValueError as exc:
            return {'success':False,'error':str(exc)}

    @API(name='pregnancy_end',description='登记分娩或结束妊娠状态/产后阶段',version='1',public=False)
    async def pregnancy_end(self, outcome: str):
        try:
            timezone = await self._read('environment.timezone')
            await asyncio.to_thread(pregnancy.end,self.store,outcome,datetime.now(ZoneInfo(timezone)))
            return {'success':True}
        except ValueError as exc:
            return {'success':False,'error':str(exc)}


def create_plugin():
    return MaiLifeCycle()
