"""Browser smoke check: pip install playwright, then python tests/browser_check.py.

Uses installed Chrome and a simulated desktop; recordings and screenshots go to temporary folders.
"""
import json
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from playwright.sync_api import sync_playwright, expect
from input_reply import core, server, actions, scripting, ai
from test_server import FakeBackend
from unittest.mock import patch

with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'data_dir', return_value=Path(tmp)):
    core.set_account(None)
    backend = FakeBackend()
    data = {'format': core.FORMAT, 'backend': 'windows', 'duration': .2,
            'target_window': {'id': '1', 'title': 'Test editor'},
            'events': [{'type': 'key_down', 'key': 'h', 't': .01}, {'type': 'key_up', 'key': 'h', 't': .02}]}
    core.write_recording('send-message.json', data)
    core.safe_path('send-message.json').with_suffix('.py').write_text(scripting.macro_to_python(data, 'send-message.json'))
    core.write_recording('ai-message.json', {'format': core.FORMAT_AGENT, 'steps': [{'type': 'type', 'text': 'Hello', 'param': 'message'}], 'parameters': [{'name': 'message'}]})
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    httpd.daemon_threads = True
    httpd.backend = backend
    httpd.state = actions.MacroState(backend)
    httpd.access_token = 'ui-test'
    httpd.assistant = ai.Assistant(backend, httpd.state)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=shutil.which('google-chrome') or shutil.which('chromium'), headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.add_init_script("sessionStorage.setItem('inputReplyToken','ui-test'); localStorage.setItem('inputReplyTheme','dark');")
            page.goto(f'http://127.0.0.1:{httpd.server_port}')
            page.locator('[role=radio]', has_text='send-message.json').click()
            page.get_by_label('Parameter name for Block 1').fill('message')
            page.get_by_role('button', name='Add', exact=True).click()
            page.get_by_label('Replacement text for message').fill('Hi Alex\nSecond line')
            assert page.get_by_label('Change message', exact=True).is_checked()
            page.get_by_role('button', name='Refresh', exact=True).click()
            assert page.get_by_label('Replacement text for message').input_value() == 'Hi Alex\nSecond line'
            page.locator('#replay-countdown').fill('0')
            page.get_by_role('button', name='Replay Macro', exact=True).click()
            page.locator('#job-phase-badge').filter(has_text='DONE').wait_for()
            assert backend.typed == ['Hi Alex\nSecond line'], backend.typed
            page.locator('#recordings-search').fill('no match')
            assert page.get_by_text('No matches. Try another name or window.').is_visible()
            page.locator('#recordings-search').fill('')
            assert page.get_by_label('Replacement text for message').input_value() == 'Hi Alex\nSecond line'
            page.screenshot(path=str(Path(tmp) / 'desktop.png'), full_page=True)
            page.get_by_label('Switch color theme').click()
            page.screenshot(path=str(Path(tmp) / 'light.png'), full_page=True)
            page.locator('[role=radio]', has_text='ai-message.json').click()
            page.get_by_label('Replacement text for message').wait_for()
            page.locator('[role=radio]', has_text='send-message.json').click()
            page.get_by_role('button', name='Python Logic', exact=True).click()
            expect(page.get_by_label('Python macro code')).to_have_value(re.compile(r'context\.play_recording'))
            page.keyboard.press('Escape')
            page.locator('#python-modal').wait_for(state='hidden')
            assert not page.locator('#app-view').evaluate('(el) => el.inert')
            page.get_by_role('button', name='Settings', exact=True).click()
            page.locator('#set-close').focus()
            page.keyboard.press('Tab')
            assert page.evaluate('() => document.activeElement.id') == 'set-autostart'
            page.keyboard.press('Escape')
            page.locator('#settings-modal').wait_for(state='hidden')
            for width in [375, 760, 1024]:
                page.set_viewport_size({'width': width, 'height': 900})
                assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'), f'Overflow at {width}'
                if width == 375: page.screenshot(path=str(Path(tmp) / 'mobile.png'), full_page=True)
            assert not errors, errors
            browser.close()
            print('PASS: parameters, multiline replay, refresh, search, AI parameters, editor, themes and responsive layouts')
    finally:
        httpd.shutdown()
        httpd.server_close()
