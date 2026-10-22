# Installing netsnitch

## Requirements

- Python 3.9 or newer, with `pip`
- `psutil` and `rich`, installed for you by the commands below

Wheels exist for common Linux, macOS, and Windows versions. A C compiler is needed only when `pip` has to build `psutil` from source.

## Install from source

```bash
git clone https://github.com/masterxhackerx420-oss/netsnitch-py.git
cd netsnitch-py
python -m pip install .
```

This installs the `netsnitch` command for that Python. Re-run the `pip install .` line after you pull new commits.

## Install into a virtual environment

Linux and macOS:

```bash
git clone https://github.com/masterxhackerx420-oss/netsnitch-py.git
cd netsnitch-py
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Windows (Command Prompt or PowerShell):

```bash
git clone https://github.com/masterxhackerx420-oss/netsnitch-py.git
cd netsnitch-py
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install .
```

If `python3` or `py -3` is not on your `PATH`, use `python` instead. Stay inside the activated venv when you run the checks below. `deactivate` leaves it.

## Platform notes

Each sample asks `psutil` for TCP and UDP sockets. Listening sockets and loopback addresses (`127.0.0.1`, `::1`, and IPv4-mapped loopback) are left out. So are sockets with no process id, including time-wait sockets and sockets whose owner the OS did not reveal. If the pid is known but the process name cannot be read, the row is labeled `pid <id>` and the note below is printed.

**Linux and macOS.** Your own processes are visible without extra privileges. When another user's sockets cannot be listed, sampling continues and the report starts with:

```text
Note: some processes were hidden. Re-run with sudo to see other users' processes.
```

The process still exits 0. Running as root skips that check.

`sudo` does not search the venv's `bin` directory. Point it at the script:

```bash
sudo .venv/bin/netsnitch --duration 60
```

If the operating system refuses the connection list entirely, `netsnitch` prints `Permission denied while listing network connections. Re-run with sudo to see other users' processes.` and exits 1.

**Windows.** There is no `sudo`, and the other-user check is skipped because Windows has no `os.getuid`. You see the connections `psutil` can read for the current account. Other accounts on the machine do not by themselves trigger the hidden-process note. If the listing is denied, the same permission message is printed (it says "sudo") and the process exits 1. Run that command from an elevated terminal. Activate the venv with `.venv\Scripts\activate`, or call `.venv\Scripts\netsnitch.exe`.

## Verify

```bash
netsnitch --help
netsnitch --duration 10
```

`--help` lists `--duration` and `--interval` and exits 0.

`--duration 10` samples about once a second for 10 seconds, reverse-DNS resolves each remote address once, then prints the ranked table. A quiet machine prints `No outbound connections observed.` That still means the install works. To see rows, open an outbound connection during the run (a browser, or `curl https://example.com` in another terminal). An address with no reverse DNS record is shown as the IP. Name lookups happen after sampling, so the command can take a little longer than 10 seconds.

## Troubleshooting

### Permission errors

The hidden-process note means the table is incomplete. On Linux and macOS, re-run with `sudo .venv/bin/netsnitch`. On Windows, use an elevated terminal. Exit status stays 0.

Exit status 1 and `Permission denied while listing network connections` means the connection list itself could not be read. Use the same remedy.

### No connections shown

`No outbound connections observed.` means no sample contained an outbound socket tied to a process.

- Loopback and listening sockets are not counted.
- A connection that opens and closes between samples can be missed. The default interval is 1 second. Pass `--interval 0.2`, or hold a connection open for the whole run.
- On Linux and macOS, other users' traffic stays hidden until you run as root. Processes you own still appear.
- A failed reverse lookup does not drop the row. The remote IP is printed instead.

### psutil fails to build

`pip` uses a prebuilt wheel when one matches your platform and Python. Otherwise it compiles `psutil`, and the log ends with `Failed building wheel for psutil`. Upgrade `pip`, then install again:

```bash
python -m pip install --upgrade pip
python -m pip install .
```

If it still compiles from source, install a compiler and Python headers, then retry `python -m pip install .`:

- Debian/Ubuntu: `sudo apt install python3-dev build-essential`
- Fedora: `sudo dnf install python3-devel gcc`
- macOS: `xcode-select --install`
- Windows: current `pip` wheels cover recent Python versions. If the log asks for Microsoft Visual C++, install the "Desktop development with C++" workload from the Visual Studio Build Tools.
