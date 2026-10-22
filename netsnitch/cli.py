"""Sample outbound connections and rank processes by the domains they talk to."""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import time
from collections import Counter

import psutil
from rich.console import Console
from rich.table import Table
from rich.text import Text

SUDO_MESSAGE = (
    "Permission denied while listing network connections. "
    "Re-run with sudo to see other users' processes."
)
HIDDEN_NOTE = (
    "Note: some processes were hidden. "
    "Re-run with sudo to see other users' processes."
)


def remote_ip(conn) -> str | None:
    """Return the remote address of an outbound connection, or None to skip it."""
    raddr = getattr(conn, "raddr", None)
    if not raddr:
        return None
    ip = getattr(raddr, "ip", None)
    if not ip:
        return None
    parsed = _parse_ip(str(ip))
    if parsed is None or parsed.is_loopback:
        return None
    return str(parsed)


def read_connections(connections, process_name):
    """Turn one snapshot of connections into (process, ip) observations.

    Sockets with no pid (time-wait, or an owner the OS did not reveal) are
    skipped. Access denied on a known pid is reported as ``pid N`` and flagged
    so the caller can suggest sudo.
    """
    observations = []
    limited = False
    for conn in connections:
        ip = remote_ip(conn)
        if ip is None:
            continue
        pid = getattr(conn, "pid", None)
        if pid is None:
            continue
        try:
            name = process_name(pid)
        except psutil.AccessDenied:
            limited = True
            name = f"pid {pid}"
        except psutil.NoSuchProcess:
            continue
        if name is None or not str(name).strip():
            name = f"pid {pid}"
        observations.append((str(name), ip))
    return observations, limited


def foreign_sockets_hidden(processes, my_uid, my_pid, is_root=False) -> bool:
    """True when another user's sockets exist and this account cannot list them."""
    if is_root or my_uid is None:
        return False
    for proc in processes:
        if proc.pid == my_pid:
            continue
        try:
            uids = proc.uids()
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, NotImplementedError):
            return True
        if uids is None or uids.real == my_uid:
            continue
        try:
            proc.net_connections(kind="inet")
        except psutil.AccessDenied:
            return True
        except psutil.NoSuchProcess:
            continue
        return False
    return False


def rank_samples(samples, resolve):
    """Rank (process, domain) pairs by how many samples they appeared in.

    Each sample contributes at most one hit per process and domain, so several
    sockets to the same domain in one snapshot count once. Reverse-DNS failures
    and blank names fall back to the IP. Each IP is resolved once.
    """
    cache: dict[str, str] = {}

    def domain_for(ip: str) -> str:
        if ip not in cache:
            cache[ip] = _domain_or_ip(ip, resolve)
        return cache[ip]

    counts: Counter[tuple[str, str]] = Counter()
    for sample in samples:
        counts.update({(process, domain_for(ip)) for process, ip in sample})

    ranked = sorted(
        counts.items(),
        key=lambda item: (-item[1], item[0][0].casefold(), item[0][1].casefold()),
    )
    return [(process, domain, hits) for (process, domain), hits in ranked]


def reverse_dns(ip: str) -> str:
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(1.0)
    try:
        host, _aliases, _addrs = socket.gethostbyaddr(ip)
        return host
    finally:
        socket.setdefaulttimeout(previous)


def sample_once():
    try:
        connections = psutil.net_connections(kind="inet")
    except psutil.AccessDenied as exc:
        raise PermissionError(SUDO_MESSAGE) from exc
    return read_connections(connections, _process_name)


def watch(duration, interval, *, sample_fn, sleep, clock, console):
    """Sample until ``duration`` seconds elapse, or until Ctrl-C if duration is None."""
    if duration is None:
        console.print(
            f"Sampling outbound connections every {interval:g}s. Press Ctrl-C to stop."
        )
    else:
        console.print(
            f"Sampling outbound connections for {duration:g}s every {interval:g}s."
        )

    samples = []
    limited = False
    deadline = None if duration is None else clock() + duration
    try:
        while deadline is None or clock() < deadline:
            observations, denied = sample_fn()
            samples.append(observations)
            limited = limited or denied
            if deadline is None:
                nap = interval
            else:
                nap = min(interval, deadline - clock())
            if nap <= 0:
                break
            sleep(nap)
    except KeyboardInterrupt:
        pass
    return samples, limited


def render(rows, *, permission_limited, samples, console):
    if permission_limited:
        console.print(HIDDEN_NOTE)
    if not rows:
        console.print("No outbound connections observed.")
        return

    table = Table(title=f"Outbound connections ({samples} samples)")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Process", style="bold")
    table.add_column("Domain")
    table.add_column("Hits", justify="right")
    for index, (process, domain, hits) in enumerate(rows, start=1):
        table.add_row(str(index), Text(process), Text(domain), str(hits))
    console.print(table)
    console.print(
        "Hits = samples in which that process was seen talking to that domain."
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    console = Console()
    try:
        hidden = foreign_sockets_hidden(
            psutil.process_iter(["pid"]),
            my_uid=os.getuid() if hasattr(os, "getuid") else None,
            my_pid=os.getpid(),
            is_root=_running_as_root(),
        )
    except psutil.Error:
        hidden = False

    try:
        samples, sample_limited = watch(
            args.duration,
            args.interval,
            sample_fn=sample_once,
            sleep=time.sleep,
            clock=time.monotonic,
            console=console,
        )
    except PermissionError as exc:
        console.print(str(exc))
        return 1

    rows = rank_samples(samples, reverse_dns)
    render(
        rows,
        permission_limited=hidden or sample_limited,
        samples=len(samples),
        console=console,
    )
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="netsnitch",
        description=(
            "Sample outbound connections and print which processes "
            "talked to which domains, and how often."
        ),
    )
    parser.add_argument(
        "--duration",
        type=_positive_float,
        default=None,
        help="Seconds to sample before printing results. Default: run until Ctrl-C.",
    )
    parser.add_argument(
        "--interval",
        type=_positive_float,
        default=1.0,
        help="Seconds between samples (default: 1).",
    )
    return parser.parse_args(argv)


def _positive_float(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0 or number != number or number == float("inf"):
        raise argparse.ArgumentTypeError("must be a positive number")
    return number


def _parse_ip(ip: str):
    try:
        parsed = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    return parsed


def _domain_or_ip(ip: str, resolve) -> str:
    try:
        name = resolve(ip)
    except OSError:
        return ip
    if name is None:
        return ip
    text = str(name).strip().rstrip(".")
    return text or ip


def _process_name(pid: int) -> str:
    return psutil.Process(pid).name()


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0


if __name__ == "__main__":
    raise SystemExit(main())
