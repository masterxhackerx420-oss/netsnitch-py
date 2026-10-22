"""Behaviour tests with fake connection snapshots, not live traffic."""

import io
import socket
from types import SimpleNamespace

import psutil
import pytest
from rich.console import Console

from netsnitch.cli import (
    foreign_sockets_hidden,
    main,
    rank_samples,
    read_connections,
    sample_once,
    watch,
)


def addr(ip, port=443):
    return SimpleNamespace(ip=ip, port=port)


def conn(ip, pid):
    raddr = () if ip is None else addr(ip)
    return SimpleNamespace(raddr=raddr, pid=pid, status="ESTABLISHED")


def ranked(samples, names, resolve):
    def process_name(pid):
        if pid not in names:
            raise psutil.NoSuchProcess(pid)
        name = names[pid]
        if name is None:
            raise psutil.AccessDenied(pid)
        return name

    limited = False
    normalized = []
    for sample in samples:
        observations, denied = read_connections(sample, process_name)
        normalized.append(observations)
        limited = limited or denied
    return rank_samples(normalized, resolve), limited


class Proc:
    def __init__(self, pid, uid, connections="ok"):
        self.pid = pid
        self._uid = uid
        self._connections = connections

    def uids(self):
        if self._uid == "denied":
            raise psutil.AccessDenied(self.pid)
        if self._uid == "gone":
            raise psutil.NoSuchProcess(self.pid)
        return SimpleNamespace(real=self._uid)

    def net_connections(self, kind="inet"):
        if self._connections == "denied":
            raise psutil.AccessDenied(self.pid)
        if self._connections == "gone":
            raise psutil.NoSuchProcess(self.pid)
        return []


def test_ranks_processes_by_how_often_they_talk_to_each_domain():
    samples = [
        [
            conn("1.1.1.1", 10),
            conn("8.8.4.4", 10),  # same process, same domain, same sample
            conn("9.9.9.9", 20),
            conn("127.0.0.1", 10),  # loopback is not outbound
            conn("::ffff:127.0.0.1", 10),
            conn(None, 10),  # listener
        ],
        [conn("1.1.1.1", 10), conn("9.9.9.9", 20)],
        [
            conn("1.1.1.1", 10),
            conn("203.0.113.9", 30),
            conn("203.0.113.9", None),  # no live process
            conn("::ffff:172.30.0.9", 30),
            conn("2606:4700:4700::1111", 40),
        ],
    ]

    def resolve(ip):
        if ip == "203.0.113.9":
            raise socket.herror("no ptr")
        return {
            "1.1.1.1": "one.one.one.one.",
            "8.8.4.4": "one.one.one.one",
            "9.9.9.9": "dns.quad9.net",
            "172.30.0.9": "gateway.internal",
            "2606:4700:4700::1111": "one.one.one.one",
        }[ip]

    rows, limited = ranked(
        samples,
        {10: "chrome", 20: "slack", 30: "curl", 40: "node"},
        resolve,
    )

    # Two sockets from chrome in the first sample share a domain, so that
    # sample counts once. curl's failed lookup stays as the IP. node talks to
    # the same domain as chrome and stays on its own row.
    assert limited is False
    assert rows == [
        ("chrome", "one.one.one.one", 3),
        ("slack", "dns.quad9.net", 2),
        ("curl", "203.0.113.9", 1),
        ("curl", "gateway.internal", 1),
        ("node", "one.one.one.one", 1),
    ]


def test_ties_sort_by_process_then_domain():
    samples = [[
        conn("10.0.0.1", 1),
        conn("10.0.0.2", 2),
        conn("10.0.0.3", 1),
    ]]

    def resolve(ip):
        return {
            "10.0.0.1": "b.example",
            "10.0.0.2": "a.example",
            "10.0.0.3": "a.example",
        }[ip]

    rows, _limited = ranked(samples, {1: "alpha", 2: "zeta"}, resolve)
    assert rows == [
        ("alpha", "a.example", 1),
        ("alpha", "b.example", 1),
        ("zeta", "a.example", 1),
    ]


@pytest.mark.parametrize(
    "error",
    [socket.herror, socket.gaierror, socket.timeout, TimeoutError, OSError],
)
def test_dns_errors_fall_back_to_the_ip(error):
    def resolve(_ip):
        raise error("no reverse dns")

    rows = rank_samples([[("curl", "203.0.113.9"), ("curl", "203.0.113.9")]], resolve)
    assert rows == [("curl", "203.0.113.9", 1)]


@pytest.mark.parametrize("result", ["", "   ", None, "."])
def test_blank_reverse_dns_falls_back_to_the_ip(result):
    rows = rank_samples([[("curl", "203.0.113.9")]], lambda _ip: result)
    assert rows == [("curl", "203.0.113.9", 1)]


def test_each_ip_is_resolved_once():
    calls = []

    def resolve(ip):
        calls.append(ip)
        if ip == "203.0.113.9":
            raise OSError("no ptr")
        return "example.com"

    samples = [
        [("curl", "203.0.113.9"), ("python", "93.184.216.34")],
        [("curl", "203.0.113.9"), ("python", "93.184.216.34")],
    ]
    rows = rank_samples(samples, resolve)
    assert rows == [
        ("curl", "203.0.113.9", 2),
        ("python", "example.com", 2),
    ]
    assert sorted(calls) == ["203.0.113.9", "93.184.216.34"]
    assert len(calls) == 2


def test_denied_process_name_is_kept_by_pid():
    rows, limited = ranked(
        [[conn("8.8.8.8", 42), conn("1.1.1.1", 7)]],
        {42: None, 7: "curl"},
        lambda ip: "dns.example" if ip == "1.1.1.1" else ip,
    )
    assert limited is True
    assert rows == [
        ("curl", "dns.example", 1),
        ("pid 42", "8.8.8.8", 1),
    ]


def test_warns_only_when_another_users_sockets_are_hidden():
    ours = Proc(10, 1000)
    hidden_root = Proc(1, 0, "denied")
    readable_root = Proc(1, 0, "ok")
    gone_then_hidden = [Proc(2, 0, "gone"), Proc(1, 0, "denied")]

    assert foreign_sockets_hidden(gone_then_hidden, my_uid=1000, my_pid=10) is True
    assert foreign_sockets_hidden([hidden_root, ours], my_uid=1000, my_pid=10) is True
    assert foreign_sockets_hidden([readable_root], my_uid=1000, my_pid=10) is False
    assert foreign_sockets_hidden([ours, Proc(11, 1000)], my_uid=1000, my_pid=10) is False
    assert foreign_sockets_hidden([hidden_root], my_uid=0, my_pid=5, is_root=True) is False


def test_listing_connections_without_permission_says_sudo(monkeypatch):
    def boom(kind="inet"):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", boom)
    with pytest.raises(PermissionError, match="sudo"):
        sample_once()


def test_duration_takes_one_sample_per_interval():
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    def sample():
        return [("curl", "1.1.1.1")], False

    buf = io.StringIO()
    samples, limited = watch(
        10,
        1,
        sample_fn=sample,
        sleep=sleep,
        clock=now,
        console=Console(file=buf, force_terminal=False),
    )
    assert len(samples) == 10
    assert limited is False
    assert "for 10s every 1s" in buf.getvalue()


def test_ctrl_c_returns_the_samples_collected_so_far():
    def sleep(_seconds):
        raise KeyboardInterrupt

    samples, limited = watch(
        None,
        1,
        sample_fn=lambda: ([("curl", "1.1.1.1")], False),
        sleep=sleep,
        clock=lambda: 0.0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )
    assert samples == [[("curl", "1.1.1.1")]]
    assert limited is False


def test_cli_prints_ranked_table_and_sudo_note(capsys, monkeypatch):
    monkeypatch.setattr(
        "netsnitch.cli.sample_once",
        lambda: ([("curl", "93.184.216.34")], True),
    )
    monkeypatch.setattr("netsnitch.cli.reverse_dns", lambda _ip: "example.com")
    monkeypatch.setattr("netsnitch.cli.foreign_sockets_hidden", lambda *args, **kwargs: False)

    assert main(["--duration", "0.03", "--interval", "0.01"]) == 0
    out = capsys.readouterr().out
    assert "curl" in out
    assert "example.com" in out
    assert "sudo" in out


def test_cli_explains_when_sampling_is_forbidden(capsys, monkeypatch):
    def denied():
        raise PermissionError(
            "Permission denied while listing network connections. "
            "Re-run with sudo to see other users' processes."
        )

    monkeypatch.setattr("netsnitch.cli.sample_once", denied)
    assert main(["--duration", "1", "--interval", "1"]) == 1
    text = capsys.readouterr().out
    assert "sudo" in text
