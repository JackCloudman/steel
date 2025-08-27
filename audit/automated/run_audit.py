#!/usr/bin/env python3
"""
Highly-instrumented Automated Audit Runner for Steel LSP (audit kit)

Adds:
- Larger default timeouts (INIT_TIMEOUT=60, REQ_TIMEOUT=20)
- Extensive logging: outgoing JSON-RPC messages, incoming messages, server stderr
- Sets RUST_LOG=debug in server env to surface initialization logs
- Writes logs to audit/automated/logs/

Use: STEEL_LSP_CMD=/path/to/steel-language-server python3 run_audit.py
"""

import os
import sys
import json
import shutil
import subprocess
import threading
import time
import re
import select
from pathlib import Path
from typing import Union

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_DIR = REPO_ROOT / 'audit'
LOG_DIR = AUDIT_DIR / 'automated' / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
FFI_DIR = AUDIT_DIR / 'ffi'
MACROS_FILE = AUDIT_DIR / 'macros' / 'infix.stl'
FFI_MAIN = AUDIT_DIR / 'ffi' / 'main.stl'

# Config via env
LSP_ENV_CMD = os.environ.get('STEEL_LSP_CMD')
USE_CARGO_FALLBACK = os.environ.get('STEEL_AUDIT_USE_CARGO', '1') != '0'

INIT_TIMEOUT = int(os.environ.get('STEEL_AUDIT_INIT_TIMEOUT', 120))
REQ_TIMEOUT = int(os.environ.get('STEEL_AUDIT_REQ_TIMEOUT', 20))

# Expectations
EXPECT_HOVER_FOR_MACRO = False
EXPECT_MIN_DIAGNOSTICS = 1
EXPECT_HOVER_FOR_FFI = False
EXPECT_GOTO_FOR_FFI = False

OUTGOING_LOG = LOG_DIR / 'outgoing.jsonl'
INCOMING_LOG = LOG_DIR / 'incoming.jsonl'
STDERR_LOG = LOG_DIR / 'server.stderr.log'
GENERAL_LOG = LOG_DIR / 'run.log'

# Redirect stdout to the general log to avoid console noise in CI
class _StdoutLogger:
    def write(self, s):
        try:
            if not s:
                return
            with GENERAL_LOG.open('a', encoding='utf-8') as f:
                f.write(s)
        except Exception:
            pass
    def flush(self):
        try:
            pass
        except Exception:
            pass

sys.stdout = _StdoutLogger()

# Helpers to append logs
def _append_jsonl(path: Path, obj):
    try:
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(obj, ensure_ascii=False))
            f.write('\n')
    except Exception as e:
        _append_text(GENERAL_LOG, f'Failed to write jsonl {path}: {e}\n')

def _append_text(path: Path, text: str):
    try:
        with path.open('a', encoding='utf-8') as f:
            f.write(text)
    except Exception:
        pass

class JSONRPCError(Exception):
    pass

class LSPProcess:
    def __init__(self, cmd: Union[str, list]):
        self.cmd = cmd
        _append_text(GENERAL_LOG, f'Launching LSP with command: {cmd}\n')

        # Prepare environment for the server: enable debug logging to capture initialization details
        env = os.environ.copy()
        if 'RUST_LOG' not in env:
            env['RUST_LOG'] = 'debug'

        # Launch
        if isinstance(cmd, list):
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        else:
            # string -> run through shell
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, env=env)

        if self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError('Failed to start LSP process with stdio pipes')

        _append_text(GENERAL_LOG, f'LSP PID: {self.proc.pid}\n')

        self._id = 0
        self._responses = {}
        self._notifications = []
        self._lock = threading.Lock()
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()
        # give the reader thread a moment to start and bind file descriptors
        time.sleep(0.05)
        # capture stderr
        self._stderr_thread = threading.Thread(target=self._stderr_reader, daemon=True)
        self._stderr_thread.start()

    def _stderr_reader(self):
        try:
            # Read lines from stderr and append to file
            while True:
                if self.proc.stderr is None:
                    break
                line = self.proc.stderr.readline()
                if not line:
                    break
                try:
                    s = line.decode('utf-8', errors='replace')
                except Exception:
                    s = str(line)
                _append_text(STDERR_LOG, s)
        except Exception as e:
            _append_text(GENERAL_LOG, f'stderr reader error: {e}\n')

    def _reader(self):
        _append_text(GENERAL_LOG, '[reader] started\n')
        buf = b''
        # try to use file descriptor for more reliable select/os.read
        fd = None
        try:
            fd = self.proc.stdout.fileno()
        except Exception:
            fd = None
        while True:
            try:
                if self.proc.stdout is None:
                    _append_text(GENERAL_LOG, '[reader] proc.stdout is None, exiting\n')
                    break
                if fd is not None:
                    # wait up to 0.1s for data
                    rlist, _, _ = select.select([fd], [], [], 0.1)
                    if not rlist:
                        if self.proc.poll() is not None:
                            _append_text(GENERAL_LOG, '[reader] process exited while waiting\n')
                            break
                        continue
                    try:
                        chunk = os.read(fd, 4096)
                    except Exception:
                        chunk = self.proc.stdout.read(4096)
                else:
                    # fallback to selecting on the file object
                    try:
                        rlist, _, _ = select.select([self.proc.stdout], [], [], 0.1)
                    except Exception:
                        rlist = [self.proc.stdout]
                    if not rlist:
                        if self.proc.poll() is not None:
                            _append_text(GENERAL_LOG, '[reader] process exited while waiting (fallback)\n')
                            break
                        continue
                    chunk = self.proc.stdout.read(4096)
            except Exception as e:
                _append_text(GENERAL_LOG, f'reader exception: {e}\n')
                break
            if not chunk:
                if self.proc.poll() is not None:
                    _append_text(GENERAL_LOG, '[reader] EOF and process exited\n')
                    break
                time.sleep(0.01)
                continue
            try:
                _append_text(GENERAL_LOG, f'reader got {len(chunk)} bytes\n')
            except Exception:
                pass
            buf += chunk
            while True:
                header_end = buf.find(b"\r\n\r\n")
                if header_end == -1:
                    break
                header = buf[:header_end].decode('utf-8', errors='replace')
                m = re.search(r'Content-Length:\s*(\d+)', header, flags=re.IGNORECASE)
                if not m:
                    buf = buf[header_end+4:]
                    continue
                content_len = int(m.group(1))
                total_needed = header_end + 4 + content_len
                if len(buf) < total_needed:
                    break
                content = buf[header_end+4:total_needed]
                buf = buf[total_needed:]
                try:
                    msg = json.loads(content.decode('utf-8'))
                except Exception as e:
                    _append_text(GENERAL_LOG, f'Failed to parse JSON from LSP: {e}\n')
                    continue
                # log incoming
                _append_jsonl(INCOMING_LOG, msg)
                # dispatch
                if isinstance(msg, dict) and 'id' in msg and ('result' in msg or 'error' in msg):
                    with self._lock:
                        self._responses[msg['id']] = msg
                else:
                    with self._lock:
                        self._notifications.append(msg)
        _append_text(GENERAL_LOG, 'reader exiting\n')

    def send(self, method, params=None, id_=None):
        if id_ is None:
            with self._lock:
                self._id += 1
                id_ = self._id
        req = {'jsonrpc': '2.0', 'id': id_, 'method': method}
        if params is not None:
            req['params'] = params
        s = json.dumps(req, ensure_ascii=False)
        payload = f"Content-Length: {len(s.encode('utf-8'))}\r\n\r\n" + s
        # log outgoing
        try:
            _append_text(OUTGOING_LOG, payload + '\n')
        except Exception:
            pass
        if self.proc.stdin is None:
            raise RuntimeError('LSP process stdin not available')
        try:
            self.proc.stdin.write(payload.encode('utf-8'))
            self.proc.stdin.flush()
        except BrokenPipeError:
            raise RuntimeError('Broken pipe writing to LSP stdin')
        return id_

    def send_notification(self, method, params=None):
        req = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            req['params'] = params
        s = json.dumps(req, ensure_ascii=False)
        payload = f"Content-Length: {len(s.encode('utf-8'))}\r\n\r\n" + s
        try:
            _append_text(OUTGOING_LOG, payload + '\n')
        except Exception:
            pass
        if self.proc.stdin is None:
            raise RuntimeError('LSP process stdin not available')
        try:
            self.proc.stdin.write(payload.encode('utf-8'))
            self.proc.stdin.flush()
        except BrokenPipeError:
            raise RuntimeError('Broken pipe writing to LSP stdin')

    def wait_response(self, id_, timeout=5):
        waited = 0.0
        while waited < timeout:
            with self._lock:
                if id_ in self._responses:
                    return self._responses.pop(id_)
            time.sleep(0.05)
            waited += 0.05
        raise JSONRPCError(f"Timeout waiting for response {id_}")

    def collect_notifications(self):
        with self._lock:
            notifications = list(self._notifications)
        return notifications

    def shutdown(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def build_ffi():
    _append_text(GENERAL_LOG, 'Building FFI crate...\n')
    r = subprocess.run(['cargo', 'build', '--release'], cwd=FFI_DIR)
    if r.returncode != 0:
        raise RuntimeError('FFI build failed')
    _append_text(GENERAL_LOG, 'FFI build complete\n')


def choose_lsp_cmd():
    if LSP_ENV_CMD:
        path = Path(LSP_ENV_CMD).expanduser()
        if not path.exists():
            raise RuntimeError(f'STEEL_LSP_CMD set but binary not found: {path}')
        return [str(path)]
    found = shutil.which('steel-language-server')
    if found:
        return [found]
    if USE_CARGO_FALLBACK:
        return ['cargo', 'run', '-p', 'steel-language-server', '--quiet', '--']
    raise RuntimeError('No steel-language-server binary found and cargo fallback disabled')


def wait_for_notification(lsp: LSPProcess, predicate, timeout=5.0):
    waited = 0.0
    while waited < timeout:
        notifs = lsp.collect_notifications()
        for n in notifs:
            try:
                if predicate(n):
                    return n
            except Exception:
                pass
        time.sleep(0.05)
        waited += 0.05
    return None


def run_tests():
    build_ffi()

    cmd = choose_lsp_cmd()
    lsp = LSPProcess(cmd)
    try:
        # Create a minimal workspace containing only the kit files to reduce server work
        min_ws = LOG_DIR / 'min_workspace'
        if min_ws.exists():
            # clean up previous
            try:
                for p in min_ws.iterdir():
                    if p.is_file():
                        p.unlink()
            except Exception:
                pass
        else:
            min_ws.mkdir(parents=True, exist_ok=True)
        # copy the key files into the minimal workspace
        try:
            shutil.copy2(MACROS_FILE, min_ws / MACROS_FILE.name)
            shutil.copy2(FFI_MAIN, min_ws / FFI_MAIN.name)
        except Exception as e:
            _append_text(GENERAL_LOG, f'Failed to prepare minimal workspace: {e}\n')
        root_uri = f'file://{min_ws.resolve()}'
        # Initialize without rootUri to avoid workspace-wide indexing
        init_params = {
            'processId': None,
            'capabilities': {},
        }
        id0 = lsp.send('initialize', init_params)
        _append_text(GENERAL_LOG, f'sent initialize id={id0}\n')
        try:
            res = lsp.wait_response(id0, timeout=INIT_TIMEOUT)
            _append_jsonl(INCOMING_LOG, {'initialize_result': res})
        except Exception as e:
            _append_text(GENERAL_LOG, f'Initialize failed: {e}\n')
            # Dump partial notifications for debugging
            notifs = lsp.collect_notifications()
            _append_text(GENERAL_LOG, f'Partial notifications count: {len(notifs)}\n')
            raise

        lsp.send_notification('initialized', {})

        # Open macros file
        text = MACROS_FILE.read_text()
        uri = f'file://{MACROS_FILE}'
        lsp.send_notification('textDocument/didOpen', {
            'textDocument': {
                'uri': uri,
                'languageId': 'scheme',
                'version': 1,
                'text': text,
            }
        })

        # Wait for diagnostics
        diag = wait_for_notification(lsp, lambda n: isinstance(n, dict) and n.get('method') == 'textDocument/publishDiagnostics' and n.get('params', {}).get('uri') == uri, timeout=5.0)
        if diag:
            _append_jsonl(INCOMING_LOG, {'diagnostics': diag})
        # Hover over the '*' occurrence
        target = '(infix 5 * 10)'
        idx = text.find(target)
        hover_res = None
        if idx != -1:
            before = text[:idx]
            line = before.count('\n')
            col = len(before.split('\n')[-1]) + target.index('*')
            pos = {'line': line, 'character': col}
            hover_id = lsp.send('textDocument/hover', {
                'textDocument': {'uri': uri},
                'position': pos
            })
            try:
                hover_res = lsp.wait_response(hover_id, timeout=REQ_TIMEOUT)
            except Exception as e:
                _append_text(GENERAL_LOG, f'Hover request error: {e}\n')

        # Collect diagnostics count
        time.sleep(0.2)
        notifs = lsp.collect_notifications()
        diag_notifications = [n for n in notifs if n.get('method') == 'textDocument/publishDiagnostics']
        _append_text(GENERAL_LOG, f'Diagnostics notifications count (total seen): {len(diag_notifications)}\n')

        # Open ffi main
        text2 = FFI_MAIN.read_text()
        uri2 = f'file://{FFI_MAIN}'
        lsp.send_notification('textDocument/didOpen', {
            'textDocument': {
                'uri': uri2,
                'languageId': 'scheme',
                'version': 1,
                'text': text2,
            }
        })
        time.sleep(1.0)

        hover_res2 = None
        goto_res = None
        idx2 = text2.find('procesar_struct')
        if idx2 != -1:
            before2 = text2[:idx2]
            line2 = before2.count('\n')
            col2 = len(before2.split('\n')[-1])
            pos2 = {'line': line2, 'character': col2}
            hover_id2 = lsp.send('textDocument/hover', {'textDocument': {'uri': uri2}, 'position': pos2})
            try:
                hover_res2 = lsp.wait_response(hover_id2, timeout=REQ_TIMEOUT)
            except Exception as e:
                _append_text(GENERAL_LOG, f'Hover2 error: {e}\n')

            goto_id = lsp.send('textDocument/definition', {'textDocument': {'uri': uri2}, 'position': pos2})
            try:
                goto_res = lsp.wait_response(goto_id, timeout=REQ_TIMEOUT)
            except Exception as e:
                _append_text(GENERAL_LOG, f'Goto error: {e}\n')

        # Evaluate expectations
        passed = True
        if EXPECT_HOVER_FOR_MACRO:
            if not hover_res or not hover_res.get('result'):
                _append_text(GENERAL_LOG, 'FAIL: Expected hover content for macro symbol but got none\n')
                passed = False
        else:
            if hover_res and hover_res.get('result'):
                _append_text(GENERAL_LOG, 'WARN: Hover returned content for macro symbol (unexpected)\n')

        if len(diag_notifications) < EXPECT_MIN_DIAGNOSTICS:
            _append_text(GENERAL_LOG, f'FAIL: Expected at least {EXPECT_MIN_DIAGNOSTICS} diagnostics notifications, saw {len(diag_notifications)}\n')
            passed = False
        else:
            _append_text(GENERAL_LOG, f'OK: Diagnostics notifications >= {EXPECT_MIN_DIAGNOSTICS}\n')

        if EXPECT_HOVER_FOR_FFI:
            if not hover_res2 or not hover_res2.get('result'):
                _append_text(GENERAL_LOG, 'FAIL: Expected hover for FFI symbol but got none\n')
                passed = False
        else:
            if hover_res2 and hover_res2.get('result'):
                _append_text(GENERAL_LOG, 'WARN: Hover returned content for FFI symbol (unexpected)\n')

        if EXPECT_GOTO_FOR_FFI:
            if not goto_res or not goto_res.get('result'):
                _append_text(GENERAL_LOG, 'FAIL: Expected gotoDefinition for FFI symbol but got none\n')
                passed = False
        else:
            if goto_res and goto_res.get('result'):
                _append_text(GENERAL_LOG, 'WARN: gotoDefinition returned a result for FFI symbol (unexpected)\n')

        if passed:
            _append_text(GENERAL_LOG, '\n=== AUDIT RESULT: PASS ===\n')
        else:
            _append_text(GENERAL_LOG, '\n=== AUDIT RESULT: FAIL ===\n')

    finally:
        lsp.shutdown()


if __name__ == '__main__':
    try:
        run_tests()
    except Exception as e:
        _append_text(GENERAL_LOG, f'run_tests exception: {e}\n')
        sys.exit(1)
    _append_text(GENERAL_LOG, 'Audit run complete\n')
