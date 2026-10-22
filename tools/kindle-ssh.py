import os, pty, select, time, sys

remote = sys.argv[1] if len(sys.argv) > 1 else "id; uname -a; echo __DONE__"
host = sys.argv[2] if len(sys.argv) > 2 else ""
port = sys.argv[3] if len(sys.argv) > 3 else "2222"
if not host:
    sys.exit("uso: python3 kindle-ssh.py \"comando\" IP-DEL-KINDLE [puerto]")
user = "root"

cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
       "-o", "NumberOfPasswordPrompts=1", "-p", port, f"{user}@{host}", remote]

pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)
else:
    out = b""; sent = False; start = time.time()
    while True:
        if time.time() - start > 60:
            break
        r, _, _ = select.select([fd], [], [], 0.5)
        if fd in r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            out += data
            sys.stdout.write(data.decode(errors="replace")); sys.stdout.flush()
            low = out.lower()
            if (not sent) and b"password" in low:
                os.write(fd, b"\n"); sent = True
            if b"__DONE__" in out:
                time.sleep(0.5)
                break
    try:
        os.waitpid(pid, 0)
    except Exception:
        pass
