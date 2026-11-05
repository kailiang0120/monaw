"""Main verification/recovery contract, including lost replies and batch scope changes."""
import asyncio
import json

import mcp.types as types
import pytest

from app.skills.browser_use.extension.bridge import ExtensionError
from app.skills.browser_use.extension.session import ToolError
from app.skills.browser_use.extension.toolset import BrowserTools, _TARGET_FACTS
from app.skills.browser_use.extension.__main__ import build_server
from app.agent.tests.test_browser_extension_tools import FakeChrome, _ax


def test_dispatch_proof_is_independent_of_business_result_and_lost_response():
    class LostReply(FakeChrome):
        lose_reply = False
        async def request(self, method, params=None, **kwargs):
            result = await super().request(method, params, **kwargs)
            if self.lose_reply and method == 'cdp' and params['method'] == 'Input.dispatchMouseEvent' and params['params']['type'] == 'mouseReleased':
                raise ExtensionError('extension_disconnected', 'The extension disconnected after dispatch.')
            return result

    async def scenario():
        chrome=LostReply()
        tools=BrowserTools(chrome)
        await tools.call('take_snapshot',{})
        result=await tools.call('click',{'uid':'1_3','includeSnapshot':False})
        assert result.action_outcome['status']=='executed' and result.action_outcome['verification']['passed'] is None
        chrome.lose_reply=True
        chrome.calls.clear()
        result=await tools.call('click',{'uid':'1_3','includeSnapshot':False})
        assert result.action_outcome['status']=='uncertain' and result.action_outcome['action_executed']
        assert result.action_outcome['recovery']['retry_allowed'] is False
        assert sum(m=='Input.dispatchMouseEvent' and p['type']=='mousePressed' for m,p in chrome.calls)==1
        # A successful read-only reconciliation can prove the expected effect
        # even though the input reply was lost. It does not send another click.
        chrome.calls.clear()
        result=await tools.call('click',{'uid':'1_3','expected_effect':{'type':'element_appeared','selector':'#success','timeout':0}})
        assert result.action_outcome['status']=='verified'
        assert sum(m=='Input.dispatchMouseEvent' and p['type']=='mousePressed' for m,p in chrome.calls)==1
        chrome.element_states=[{'attached':False,'visible':False}]
        result=await tools.call('click',{'uid':'1_3','expected_effect':{'type':'element_appeared','selector':'#missing','timeout':0}})
        # Readiness rejects this target; nothing is repeated after uncertainty.
    with pytest.raises(ToolError,match='replaced or removed'):
        asyncio.run(scenario())


def test_multifield_batch_stops_on_changed_destination_after_first_input():
    class DynamicForm(FakeChrome):
        def __init__(self):
            super().__init__()
            self.tree[1]['childIds'].append('8')
            self.tree.append(_ax(8,'textbox','Second',backend=15,parent=2))
            self.changed=False
        async def request(self,method,params=None,**kwargs):
            result=await super().request(method,params,**kwargs)
            if method=='cdp' and params['method']=='Runtime.callFunctionOn' and params['params']['functionDeclaration']==_TARGET_FACTS:
                object_id=params['params']['objectId']
                destination='https://evil.example/changed' if self.changed and object_id=='obj-15' else 'https://example.com/submit'
                return {'result':{'value':{'tag':'input','type':'text','id':object_id,'attached':True,'frame_url':'https://example.com/',
                    'form_action':destination,'form_method':'post','form_id':'same','control_index':0 if object_id=='obj-13' else 1}}}
            if method=='cdp' and params['method']=='Input.insertText':
                self.changed=True
            return result
    async def scenario():
        chrome=DynamicForm()
        tools=BrowserTools(chrome)
        await tools.call('take_snapshot',{})
        second=next(n.uid for n in tools.pages.session(7).nodes.values() if n.name=='Second')
        tools.require_authorization=True
        arguments={'elements':[{'uid':'1_5','value':'first'},{'uid':second,'value':'second'}],'includeSnapshot':False}
        context=(await tools.call('__monaw_action_context',{'tool_name':'fill_form','arguments':arguments})).structured_content['browser_action_context']
        result=await tools.call('fill_form',{**arguments,'__monaw_authorization':context})
        assert result.is_error and result.action_outcome['status']=='uncertain'
        assert 'Completed uids: 1_5' in result.lines[1]
        assert [p for m,p in chrome.calls if m=='Input.insertText']==[{'text':'first'}]
    asyncio.run(scenario())


def test_reconnect_invalidates_uid_and_opaque_binding_without_replay():
    async def scenario():
        chrome=FakeChrome()
        tools=BrowserTools(chrome)
        await tools.call('take_snapshot',{})
        arguments={'uid':'1_3','includeSnapshot':False}
        context=(await tools.call('__monaw_action_context',{'tool_name':'click','arguments':arguments})).structured_content['browser_action_context']
        tools.pages._on_bridge_event({'event':'disconnected'})
        with pytest.raises(ToolError,match='predates reconnection'):
            await tools.call('click',{**arguments,'__monaw_authorization':context})
        assert not any(m.startswith('Input.') for m,_ in chrome.calls)
    asyncio.run(scenario())


def test_stdio_validation_accepts_private_binding_but_does_not_expose_it():
    async def scenario():
        chrome=FakeChrome()
        tools=BrowserTools(chrome)
        await tools.call('take_snapshot',{})
        tools.require_authorization=True
        async def listening():
            return None
        server=build_server(tools,listening)
        call=server.request_handlers[types.CallToolRequest]
        arguments={'uid':'1_3','includeSnapshot':False}
        preflight=await call(types.CallToolRequest(params=types.CallToolRequestParams(name='__monaw_action_context',arguments={'tool_name':'click','arguments':arguments})))
        context=preflight.root.structuredContent['browser_action_context']
        response=await call(types.CallToolRequest(params=types.CallToolRequestParams(name='click',arguments={**arguments,'__monaw_authorization':{**context,'action_id':'adapter-action'}})))
        assert not response.root.isError
        assert response.root.structuredContent['action_outcome']['action_id']=='adapter-action'
        assert len([1 for m,p in chrome.calls if m=='Input.dispatchMouseEvent' and p['type']=='mouseReleased'])==1
        invalid=await call(types.CallToolRequest(params=types.CallToolRequestParams(name='click',arguments={'uid':'1_3','unexpected':'never accepted'})))
        assert invalid.root.isError
    asyncio.run(scenario())

@pytest.mark.parametrize(('field_type','autocomplete','consequential'), [
    ('text','',False), ('password','',True), ('file','',True), ('text','cc-number',True),
])
def test_native_get_sensitive_controls_require_consequential_policy(field_type,autocomplete,consequential):
    class GetForm(FakeChrome):
        async def request(self,method,params=None,**kwargs):
            result=await super().request(method,params,**kwargs)
            if method=='cdp' and params['method']=='Runtime.callFunctionOn' and params['params']['functionDeclaration']==_TARGET_FACTS:
                return {'result':{'value':{'tag':'button','type':'submit','attached':True,'frame_url':'https://example.com/',
                    'form_action':'https://example.com/submit','form_method':'get','form_controls':[
                        {'id':'field','name':'field','type':field_type,'autocomplete':autocomplete,'value':'never exposed'}]}}}
            return result
    async def scenario():
        tools=BrowserTools(GetForm())
        await tools.call('take_snapshot',{})
        context=(await tools.call('__monaw_action_context',{'tool_name':'click','arguments':{'uid':'1_3'}})).structured_content['browser_action_context']
        assert (context['side_effect']=='external_mutation') is consequential
        assert context['runtime_facts']['sensitive_data_submission'] is consequential
        assert ('never exposed' in json.dumps(context)) is False
    asyncio.run(scenario())
