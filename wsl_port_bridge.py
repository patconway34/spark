#!/usr/bin/env python3
"""Loopback bridge for ttyd ports when WSL's own port relay is wedged.

WHY THIS EXISTS
    ttyd runs inside WSL and binds 0.0.0.0. Windows normally reaches it on
    127.0.0.1:<port> because WSL runs a relay (wslrelay.exe / the WSL svchost)
    that forwards localhost into the VM over vsock.

    When that vsock channel breaks -- the symptom is
        WSL (NNNN) ERROR: UtilAcceptVsock:271: accept4 failed 110
    and `wsl.exe -e true` timing out with Wsl/Service/0x8007274c -- the relay
    still ACCEPTS the TCP connection on Windows but never delivers it to the
    VM. ttyd is fine; the tab just renders blank. See gotchas/wsl-relay-wedge.md.

    ttyd is still reachable directly at the VM's IP, so this script stands in
    for the dead relay: listen on 127.0.0.1:<port>, forward to <vm-ip>:<port>.

USAGE
    python wsl_port_bridge.py <vm-ip> <port> [<port> ...]

    Run it only for the ports whose relay died -- a port the real relay still
    owns cannot be bound and will be reported as skipped.

THIS IS A BAND-AID. The permanent fix is `wsl --shutdown` and re-running
start.sh, which rebuilds the relay properly. Kill this process first when you
do that, or it will squat on the ports the relay wants back.
"""
import socket
import sys
import threading


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


def handle(client, target):
    try:
        upstream = socket.create_connection(target, timeout=10)
    except OSError:
        client.close()
        return
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


def serve(port, vm_ip):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
    except OSError as e:
        print(f"  {port}: SKIPPED ({e})", flush=True)
        return
    srv.listen(64)
    print(f"  {port}: 127.0.0.1:{port} -> {vm_ip}:{port}", flush=True)
    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            break
        handle(client, (vm_ip, port))


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    vm_ip = sys.argv[1]
    ports = [int(p) for p in sys.argv[2:]]
    print(f"WSL port bridge -> {vm_ip}", flush=True)
    threads = [threading.Thread(target=serve, args=(p, vm_ip), daemon=True)
               for p in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
