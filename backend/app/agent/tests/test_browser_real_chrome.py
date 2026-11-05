"""Main browser workflow against real Chrome with a disposable isolated profile."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen
import pytest
import websockets
from app.skills.browser_use.extension.session import ToolError
from app.skills.browser_use.extension.toolset import BrowserTools

HTML = b'''<!doctype html><meta charset=utf-8><title>Isolated workflow</title>
<style>button,input{display:block;margin:18px;padding:12px}</style>
<input id=email aria-label=Email><button id=save onclick="window.sent=(window.sent||0)+1;document.querySelector('#status').innerHTML='<p id=success>Saved</p>';history.pushState({},'', '/done')">Save draft</button>
<button id=duplicate>Duplicate</button><button>Duplicate</button><div id=status></div>
<button id=noeffect>No effect</button><button id=dialog onclick="alert('Review')">Show dialog</button>
<form action=/form-target method=post><input id=first name=first aria-label="First field"><input id=second name=second aria-label="Second field">
<button type=submit formaction=/override>Submit locally</button></form>
<iframe title="Accessible frame" src=/frame></iframe>
<iframe title="Inherited srcdoc" srcdoc="<button onclick='parent.document.body.setAttribute(&quot;data-srcdoc&quot;,&quot;clicked&quot;)'>Srcdoc action</button>"></iframe>
<iframe title="Inherited blank" src="about:blank" onload="this.contentDocument.body.innerHTML='<button>Blank action</button>'"></iframe>
<iframe title="Opaque sandbox" sandbox srcdoc="<button>Opaque action</button>"></iframe>'''

class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type','text/html')
        self.end_headers()
        self.wfile.write(b'<button onclick="parent.document.querySelector(\'#status\').innerHTML=\'<p id=frameclicked>Frame clicked</p>\'">Frame action</button>' if self.path == '/frame' else HTML)
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length',0)))
        self.send_response(200)
        self.send_header('Content-Type','text/html')
        self.end_headers()
        self.wfile.write(b'<p id=submitted>Submission received</p>')
    def log_message(self,*args):
        pass

@contextmanager
def isolated_chrome(tmp_path):
    executable = next((p for p in [shutil.which('google-chrome'),shutil.which('chromium'),
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'] if p and Path(p).is_file()),None)
    if not executable:
        pytest.skip('Chrome/Chromium required for isolated real-browser integration.')
    http = ThreadingHTTPServer(('127.0.0.1',0),_Page)
    thread = threading.Thread(target=http.serve_forever,daemon=True)
    thread.start()
    profile = tmp_path/'isolated-chrome-profile'
    profile.mkdir()
    process = subprocess.Popen([executable,'--headless=new','--no-first-run','--no-default-browser-check',
        '--disable-background-networking','--remote-debugging-port=0','--remote-allow-origins=http://localhost',
        '--user-data-dir='+str(profile),'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        active = profile/'DevToolsActivePort'
        deadline = time.monotonic()+15
        port=None
        while process.poll() is None and time.monotonic()<deadline:
            try:
                port=int(active.read_text().splitlines()[0])
                break
            except (OSError,ValueError,IndexError):
                pass  # Windows can briefly hold the file while Chrome writes it.
            time.sleep(.05)
        assert port,'Isolated Chrome did not expose DevTools endpoint.'
        with urlopen(f'http://127.0.0.1:{port}/json/list',timeout=5) as response:
            page = next(t for t in json.load(response) if t['type']=='page')
        yield page['webSocketDebuggerUrl'],f'http://127.0.0.1:{http.server_port}/'
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        http.shutdown()
        http.server_close()
        thread.join(timeout=3)

class RealCDPBridge:
    connected = True
    def __init__(self,socket):
        self.socket,self.pending,self.listeners,self.counter,self.url,self.calls=socket,{},[],0,'about:blank',[]
        self.reader=asyncio.create_task(self._read())
    def add_listener(self,callback):
        self.listeners.append(callback)
    async def _read(self):
        async for raw in self.socket:
            message=json.loads(raw)
            if 'id' in message:
                future=self.pending.pop(message['id'],None)
                if future and not future.done():
                    future.set_result(message)
            elif 'method' in message:
                params=message.get('params',{})
                if message['method']=='Page.frameNavigated' and not params.get('frame',{}).get('parentId'):
                    self.url=params['frame'].get('url',self.url)
                if message['method']=='Page.navigatedWithinDocument':
                    self.url=params.get('url',self.url)
                for callback in self.listeners:
                    callback({'event':'cdp','tabId':7,'method':message['method'],'params':params})
    async def request(self,method,params=None,*,timeout=30):
        params=params or {}
        if method=='tabs.list':
            return [{'id':7,'url':self.url,'title':'Fixture','active':True,'focusedWindow':True}]
        if method=='tabs.activate':
            return await self.cdp('Page.bringToFront',{},timeout)
        assert method=='cdp',method
        return await self.cdp(params['method'],params['params'],timeout)
    async def cdp(self,method,params=None,timeout=30):
        self.calls.append((method,params or {}))
        self.counter+=1
        request_id=self.counter
        future=asyncio.get_running_loop().create_future()
        self.pending[request_id]=future
        await self.socket.send(json.dumps({'id':request_id,'method':method,'params':params or {}}))
        response=await asyncio.wait_for(future,timeout)
        if 'error' in response:
            raise ToolError(response['error']['message'])
        return response.get('result',{})
    async def evaluate(self,expression):
        return (await self.cdp('Runtime.evaluate',{'expression':expression,'returnByValue':True}))['result'].get('value')

async def _workflow(socket_url,url):
    async with websockets.connect(socket_url,origin='http://localhost') as socket:
        bridge=RealCDPBridge(socket)
        tools=BrowserTools(bridge)
        await tools.call('navigate_page',{'url':url,'includeSnapshot':False})
        snapshot=await tools.call('take_snapshot',{})
        session=tools.pages.session(7)
        uid=lambda name:next(n.uid for n in session.nodes.values() if n.name==name)
        assert any(n.name=='Frame action' for n in session.nodes.values())
        assert any(n.name=='Srcdoc action' for n in session.nodes.values()), (session.frames, snapshot.lines[0])
        assert any(n.name=='Blank action' for n in session.nodes.values())
        assert not any(n.name=='Opaque action' for n in session.nodes.values())
        assert 'inaccessible or policy-restricted frame was omitted' in snapshot.lines[0]
        context=(await tools.call('__monaw_action_context',{'tool_name':'inspect_element','arguments':{'selector':'#email'}})).structured_content['browser_action_context']
        assert context['frame_origins'] and all(origin.startswith('http://127.0.0.1:') for origin in context['frame_origins'])
        assert context['unsupported_frames']
        result=await tools.call('click',{'uid':uid('Srcdoc action'),'expected_effect':{'type':'element_appeared','selector':'body[data-srcdoc="clicked"]'}})
        assert result.action_outcome['status']=='verified'
        result=await tools.call('click',{'uid':uid('Frame action'),'expected_effect':{'type':'element_appeared','selector':'#frameclicked'}})
        assert result.action_outcome['status']=='verified'
        with pytest.raises(ToolError,match='multiple'):
            await tools.call('inspect_element',{'selector':'button'})
        stale=uid('Duplicate')
        await bridge.evaluate("document.querySelector('#duplicate').outerHTML='<button id=duplicate>Different target</button>'")
        presses=lambda:sum(m=='Input.dispatchMouseEvent' and p['type']=='mousePressed' for m,p in bridge.calls)
        count=presses()
        with pytest.raises(ToolError,match='stale_element|no longer exists|replaced|node'):
            await tools.call('click',{'uid':stale})
        assert presses()==count
        await tools.call('take_snapshot',{})
        result=await tools.call('fill',{'uid':uid('Email'),'value':'fixture@example.test',
            'expected_effect':{'type':'value_equals','selector':'#email','value':'fixture@example.test'}})
        assert result.action_outcome['status']=='verified'
        result=await tools.call('click',{'uid':uid('Save draft'),'expected_effect':{'type':'url_equals','value':url+'done'}})
        assert result.action_outcome['status']=='verified'
        assert await bridge.evaluate('window.sent')==1 and session.same_document>0
        result=await tools.call('click',{'uid':uid('No effect'),'expected_effect':{'type':'element_appeared','selector':'#never','timeout':0}})
        assert result.action_outcome['status']=='failed' and result.action_outcome['action_executed']
        assert result.action_outcome['recovery']['retry_allowed'] is False
        screenshot=await tools.call('take_screenshot',{})
        binding=screenshot.structured_content['visual_binding']
        assert binding['image_width']>0 and binding['viewport_width']>0
        point=await bridge.evaluate("(()=>{const r=document.querySelector('#noeffect').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
        visual={'screenshot_id':binding['screenshot_id'],**point}
        await bridge.evaluate("document.querySelector('#noeffect').setAttribute('data-revision','new')")
        with pytest.raises(ToolError,match='unsafe_visual_fallback'):
            await tools.call('click',{'visual_target':visual})
        screenshot=await tools.call('take_screenshot',{})
        visual['screenshot_id']=screenshot.structured_content['visual_binding']['screenshot_id']
        result=await tools.call('click',{'visual_target':visual,'includeSnapshot':False})
        assert result.action_outcome['status']=='executed'
        with pytest.raises(ToolError,match='unsafe_visual_fallback'):
            await tools.call('click',{'visual_target':visual})
        result=await tools.call('click',{'uid':uid('Show dialog'),'expected_effect':{'type':'dialog_opened'}})
        assert result.action_outcome['status']=='verified'
        await tools.call('handle_dialog',{'action':'dismiss','includeSnapshot':False})
        tools.require_authorization=True
        args={'uid':uid('No effect'),'includeSnapshot':False}
        with pytest.raises(ToolError,match='authorization'):
            await tools.call('click',args)
        context=(await tools.call('__monaw_action_context',{'tool_name':'click','arguments':args})).structured_content['browser_action_context']
        result=await tools.call('click',{**args,'__monaw_authorization':{**context,'action_id':'real-browser-action'}})
        assert result.action_outcome['action_id']=='real-browser-action'
        with pytest.raises(ToolError,match='already consumed'):
            await tools.call('click',{**args,'__monaw_authorization':context})
        context=(await tools.call('__monaw_action_context',{'tool_name':'click','arguments':args})).structured_content['browser_action_context']
        await bridge.evaluate("document.querySelector('#noeffect').setAttribute('data-destination','changed')")
        with pytest.raises(ToolError,match='context changed'):
            await tools.call('click',{**args,'__monaw_authorization':context})
        tools.require_authorization=False
        await tools.call('navigate_page',{'url':url,'includeSnapshot':False})
        await tools.call('take_snapshot',{})
        tools.require_authorization=True
        args={'elements':[{'uid':uid('First field'),'value':'one'},{'uid':uid('Second field'),'value':'two'}],
              'includeSnapshot':False,'expected_effect':{'type':'value_equals','selector':'#second','value':'two'}}
        context=(await tools.call('__monaw_action_context',{'tool_name':'fill_form','arguments':args})).structured_content['browser_action_context']
        result=await tools.call('fill_form',{**args,'__monaw_authorization':context})
        assert result.action_outcome['status']=='verified'
        assert await bridge.evaluate('document.querySelector("#first").value')=='one'
        # Main-world getters can lie while native form submission still uses
        # real DOM attributes. Trusted fixed queries must use isolated wrappers.
        await bridge.evaluate("""(() => {
          const button=document.querySelector('form button'), form=button.form;
          for (const name of ['action','method','elements']) {
            const field=document.createElement('input'); field.type='hidden'; field.name=name; field.value='clobber'; form.appendChild(field);
          }
          window.hostileFactsRead=0;
          const lie=value => ({configurable:true,get(){window.hostileFactsRead++;return value;}});
          Object.defineProperty(button,'type',lie('button'));
          Object.defineProperty(button,'formAction',lie('https://false-destination.invalid/'));
          Object.defineProperty(button,'formMethod',lie('get'));
          Object.defineProperty(form,'action',lie('https://false-destination.invalid/'));
          Object.defineProperty(form,'method',lie('get'));
        })()""")
        click_args={'uid':uid('Submit locally'),'includeSnapshot':False}
        guarded=(await tools.call('__monaw_action_context',{'tool_name':'click','arguments':click_args})).structured_content['browser_action_context']
        assert guarded['side_effect']=='external_mutation' and guarded['destination']==url+'override'
        assert guarded['target']['type']=='submit' and guarded['target']['form_method']=='post'
        assert await bridge.evaluate('window.hostileFactsRead')==0
        text_uid=next(n.uid for n in session.nodes.values() if n.role=='StaticText' and n.name=='Submit locally')
        nested=(await tools.call('__monaw_action_context',{'tool_name':'click','arguments':{'uid':text_uid}})).structured_content['browser_action_context']
        assert nested['side_effect']=='external_mutation' and nested['destination']==url+'override'
        await bridge.evaluate('document.querySelector("#first").focus()')
        args={'key':'Enter','includeSnapshot':False,'expected_effect':{'type':'url_equals','value':url+'override'}}
        context=(await tools.call('__monaw_action_context',{'tool_name':'press_key','arguments':args})).structured_content['browser_action_context']
        assert context['destination']==url+'override' and context['side_effect']=='external_mutation'
        assert await bridge.evaluate('window.hostileFactsRead')==0
        result=await tools.call('press_key',{**args,'__monaw_authorization':context})
        assert result.action_outcome['status']=='verified',result.action_outcome
        assert await bridge.evaluate('document.querySelector("#submitted").textContent')=='Submission received'
        bridge.reader.cancel()
        await asyncio.gather(bridge.reader,return_exceptions=True)

def test_real_chrome_main_workflow_and_exact_runtime_binding(tmp_path):
    with isolated_chrome(tmp_path) as (socket_url,url):
        asyncio.run(_workflow(socket_url,url))
