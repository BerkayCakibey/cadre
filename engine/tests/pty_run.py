#!/usr/bin/env python3
"""pty_run.py — runs a command in a REAL pseudo-terminal, sends timed keys, prints the output.
   pty_run.py <seconds> "<t1>:<key1>,<t2>:<key2>" -- <command...>
Why: TTY-dependent tools like the watcher/menu (termios, scroll region, key reading) cannot be tested through a pipe."""
import os, pty, sys, time, select, signal

duration = float(sys.argv[1]); keys = []
if sys.argv[2]:
    for tk in sys.argv[2].split(","):
        t, k = tk.split(":", 1); keys.append((float(t), k))
cmd = sys.argv[sys.argv.index("--") + 1:]
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"; os.environ["COLUMNS"] = "120"; os.environ["LINES"] = "40"
    os.execvp(cmd[0], cmd)
# window size
try:
    import fcntl, termios, struct
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
except Exception:
    pass
out = b""; start = time.time(); sent = set()
while True:
    r, _, _ = select.select([fd], [], [], 0.1)
    if r:
        try:
            d = os.read(fd, 65536)
            if not d:
                break
            out += d
        except OSError:
            break
    passing = time.time() - start
    for i, (t, k) in enumerate(keys):
        if i not in sent and passing >= t:
            os.write(fd, k.encode()); sent.add(i)
    if passing > duration:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        break
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            # take the remaining output
            while True:
                r, _, _ = select.select([fd], [], [], 0.2)
                if not r: break
                try:
                    d = os.read(fd, 65536)
                    if not d: break
                    out += d
                except OSError: break
            break
    except ChildProcessError:
        break
sys.stdout.write(out.decode(errors="replace").replace("\r", ""))
