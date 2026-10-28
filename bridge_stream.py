"""Bounded, cancellable subprocess streaming with concurrent stderr draining."""
from contextlib import contextmanager
import os
import selectors
import signal
import subprocess
import time
import re


class BridgeCancelled(RuntimeError):
    pass


# Kill only the credentialed bridge carrying this invocation's random tag.
# Terminating docker-exec alone does not terminate its process in the container.
KILL_TAGGED = """const fs=require('fs');const tag=process.argv[1];for(const pid of fs.readdirSync('/proc')){if(!/^\\d+$/.test(pid)||Number(pid)===process.pid)continue;try{const args=fs.readFileSync('/proc/'+pid+'/cmdline','utf8').split('\\0');if(args.includes(tag)&&args.includes('-'))process.kill(Number(pid),'SIGKILL');}catch{}}"""


def stop_process(process):
    tag = getattr(process, 'album_tag', None)
    if tag and process.poll() != 0:
        try:
            subprocess.run(['docker', 'exec', process.album_container, 'node', '-e', KILL_TAGGED, '--', tag],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass  # The guest also has its own hard deadline if Docker is unavailable.
    if process.poll() is None:
        try:
            if getattr(process, 'album_new_session', False):
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                if getattr(process, 'album_new_session', False):
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
    process.wait(timeout=2)


def _chunks(process, *, total, stall, cancel):
    selector = selectors.DefaultSelector()
    error = bytearray()
    started = progress = time.monotonic()
    try:
        for stream, kind in ((process.stdout, 'out'), (process.stderr, 'err')):
            if stream is not None:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if cancel is not None and cancel.is_set():
                raise BridgeCancelled('PicPeak request cancelled. You can try again.')
            if now - started >= total:
                raise TimeoutError('PicPeak request exceeded its time limit. You can try again.')
            if now - progress >= stall:
                raise TimeoutError('PicPeak stopped responding. You can try again.')
            for key, _ in selector.select(min(.2, total - (now-started), stall - (now-progress))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == 'err':
                    error.extend(chunk)
                    del error[:-8192]
                else:
                    progress = time.monotonic()
                    yield chunk
            if not selector.get_map() and process.poll() is None:
                time.sleep(.02)
        code = process.wait(timeout=1)
        if code:
            message = re.sub(r'https?://\S+', '[storage endpoint]', error.decode(errors='replace'))[-300:].strip()
            raise RuntimeError(message or 'PicPeak processor stopped. Please try again.')
    finally:
        selector.close()


@contextmanager
def stream(process, *, total=1800, stall=60, cancel=None):
    iterator = _chunks(process, total=total, stall=stall, cancel=cancel)
    try:
        yield iterator
    finally:
        iterator.close()
        stop_process(process)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def json_rows(chunks, limit=16*1024*1024):
    import json
    pending = bytearray()
    for chunk in chunks:
        pending.extend(chunk)
        while b'\n' in pending:
            line, _, remaining = pending.partition(b'\n')
            pending = bytearray(remaining)
            if len(line) > limit:
                raise ValueError('PicPeak preview response is too large')
            if line.strip():
                yield json.loads(line)
        if len(pending) > limit:
            raise ValueError('PicPeak preview response is too large')
    if pending.strip():
        yield json.loads(pending)
