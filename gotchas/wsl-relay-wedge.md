# Tabs render blank because WSL's port relay wedged (not ttyd)

**Seen:** 2026-09-02 — tabs 4-7 blank, tabs 1-3 fine. ttyd was healthy the whole time.

## Symptom

Some tabs blank, others fine. `start.sh` reports `Terminals listening: 7/7` and a
Windows TCP probe (`Test-NetConnection 127.0.0.1 -Port 7685`) says LISTENING — but

    curl http://localhost:7685/term/spark4/    ->  000 (no HTTP response)
    curl http://172.31.83.137:7685/term/spark4/ ->  200

Alongside it: `wsl.exe -e true` hangs and fails with `Wsl/Service/0x8007274c`, and the
running Claude panes log `WSL (NNNN) ERROR: UtilAcceptVsock:271: accept4 failed 110`.

## Cause

ttyd runs in WSL and binds 0.0.0.0. Windows reaches it on 127.0.0.1 only because WSL
runs a relay that forwards localhost into the VM over vsock. When that vsock channel
breaks, the relay still ACCEPTS the TCP connection on the Windows side but never
delivers it to the VM — so the port probes as open and then answers nothing.

The relay is not one process. `netstat -ano` on the ttyd ports tells you which:

    0.0.0.0:7682    LISTENING  4160    <- svchost (old path)   working
    127.0.0.1:7685  LISTENING  25876   <- wslrelay.exe         wedged

A split like that is the tell: the ports on the wedged relay are exactly the dead tabs.

## Do not

- Re-run `start.sh`. ttyd is fine; restarting it changes nothing and, while WSL can't
  spawn, the script cannot run at all.
- Restart Spark. Its persistent tmux helper is a `wsl.exe` child (see tmux_helper.py);
  it cannot be respawned while WSL is wedged, and you would lose the terminal controls
  that still work.
- `netsh interface portproxy`. Needs elevation, and it squats the port afterwards.

## Fix — permanent

`wsl --shutdown`, then re-run `start.sh`. This rebuilds the relay properly. It also
kills every tmux session, so all running Claude conversations are lost. Do it when the
tabs are idle.

## Fix — band-aid, keeps sessions alive

ttyd is still reachable at the VM's IP, so stand in for the dead relay:

    netstat -ano | findstr :768        # find the wedged relay's PID
    # kill it so the port frees up (only kills already-dead forwards)
    powershell Stop-Process -Id <pid> -Force
    python C:\dev\spark\wsl_port_bridge.py <vm-ip> 7685 7686 7687 7688

Get `<vm-ip>` from the `172.31.x.x` rows in that same netstat output — `wsl hostname -I`
won't run while WSL is wedged.

The bridge listens on 127.0.0.1 (no admin needed), so cloudflare/config.yml and Spark's
`local_url` keep working unchanged — no config edit, no cloudflared restart.

**The VM IP changes on every WSL restart.** Kill the bridge before `wsl --shutdown`, or
it will squat the ports the real relay is trying to reclaim.
