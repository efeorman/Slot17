"""Process-scoped lock. Released by the OS even after a crash."""
import os
from pathlib import Path


def receiver_processes(proc_root='/proc'):
    """Best-effort Linux guard for legacy tools that do not use watch.lock.

    Return only PID/tool names, never command arguments or credentials. The
    runtime lock is the race-safe guard for cooperative detector/preflight use;
    a process inventory cannot prevent an unrelated tool opening the SDR later.
    """
    names = {b'watch.py', b'supervise.py', b'capture.py', b'stream.py', b'antenna.py',
             b'diagnostics.py', b'survey.py', b'calibrate.py', b'stability.py',
             b'drop_probe.py', b'airspy_rx', b'airspy_info',
             # The RTL-SDR tools hold a stick too (found in review, 26 November 2026).
             b'rtl_test', b'rtl_sdr', b'rtl_tcp', b'rtl_eeprom', b'rtl_fm', b'rtl_power'}
    found = []
    for path in Path(proc_root).glob('[0-9]*/cmdline'):
        if path.parent.name == str(os.getpid()):
            continue
        try:
            args = path.read_bytes().split(b'\0')
        except OSError:
            continue
        if b'--selftest' in args:
            continue
        for arg in args:
            name = arg.rsplit(b'/', 1)[-1]
            if name in names:
                found.append({'pid': int(path.parent.name), 'tool': name.decode()})
                break
    return found


def acquire_idle(root):
    """Reserve the detector lock before offline tests and reject known receivers."""
    owner = acquire(Path(root)/'runtime/watch.lock')
    try:
        busy = receiver_processes()
        if busy:
            raise RuntimeError('receiver/supervisor is already running: '+
                               ', '.join(f"{r['tool']} (PID {r['pid']})" for r in busy))
        return owner
    except Exception:
        owner.close()
        raise


def acquire(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise RuntimeError("another detector already owns this runtime lock") from None
    return fh
