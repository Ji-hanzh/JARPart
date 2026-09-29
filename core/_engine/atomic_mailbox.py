"""Atomic mailbox."""
import contextlib
import json
import os
import signal
import time
from pathlib import Path

@contextlib.contextmanager
def candidate_commit():
    previous=signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGALRM})
    try:yield
    finally:signal.pthread_sigmask(signal.SIG_SETMASK,previous)

class Mailbox:
    def __init__(self,folder):
        self.folder=Path(folder);self.sequence=0
    def prepare(self,message):
        self.sequence+=1
        target=self.folder/f'{self.sequence:09d}.json'
        temporary=target.with_suffix('.pending')
        # JSON encoding / full candidate copy happen before the commit section.
        temporary.write_text(json.dumps(message,separators=(',',':')),encoding='utf-8')
        def commit():os.replace(temporary,target)
        return commit
    def __call__(self,message):self.prepare(message)()
    def receive(self):
        for p in sorted(self.folder.glob('*.json')):
            message=json.loads(p.read_text(encoding='utf-8'))
            p.unlink()
            yield message

def supervise(worker,args):
    import multiprocessing
    import tempfile
    from runtime_common import PKG
    folder=PKG/'outputs/supervision';folder.mkdir(parents=True,exist_ok=True)
    ctx=multiprocessing.get_context('fork')
    with tempfile.TemporaryDirectory(prefix='module-',dir=folder) as td:
        mailbox=Mailbox(td)
        proc=ctx.Process(target=worker,args=(td,args));proc.start()
        state=deadline=started=done=failure=stopped=None
        def collect():
            nonlocal state,deadline,started,done,failure
            for message in mailbox.receive():
                kind=message['kind']
                if kind=='ready':
                    state=message['result'];deadline=message['deadline'];started=message['started']
                elif kind=='progress':
                    if state is not None and message['emitted_at']<=deadline:state.update(message['fields'])
                elif kind=='done':
                    # A complete worker result is usable only before supervisor stop.
                    if stopped is None:done=message['result']
                    elif message['result'].get('state')=='PROCESS_FAILED':failure=message['result'].get('error') or 'worker technical failure'
                elif kind=='error':failure=message['error']
        try:
            while True:
                now=time.monotonic()
                if deadline is not None and now>=deadline:
                    stopped=now
                    if proc.is_alive():proc.kill()
                    proc.join(5)
                    # Consume every complete pre-deadline event, never .pending files.
                    collect();break
                collect()
                if done is not None or failure is not None:break
                if not proc.is_alive():collect();break
                delay=.01 if deadline is None else max(0,min(.01,deadline-time.monotonic()))
                time.sleep(delay)
        finally:
            if proc.is_alive():proc.kill()
            proc.join(5)
        return state,deadline,started,done,failure,stopped,proc.exitcode
