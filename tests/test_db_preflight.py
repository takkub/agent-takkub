"""Unit tests for the test-DB reachability preflight (#529)."""

from __future__ import annotations

import socket

from agent_takkub import db_preflight


def test_no_db_url_env_var_returns_none():
    assert db_preflight.check_test_db_reachable({}) is None


def test_unreachable_db_fails_with_host_port_in_detail():
    # A closed local port — nothing is listening, so the connect refuses
    # immediately instead of needing the full timeout.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    finding = db_preflight.check_test_db_reachable(
        {"DATABASE_URL": f"postgres://user:pw@127.0.0.1:{port}/app_test"}
    )

    assert finding is not None
    assert finding.ok is False
    assert finding.skipped is False
    assert f"127.0.0.1:{port}" in finding.detail
    assert "unreachable" in finding.detail


def test_reachable_db_passes():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        finding = db_preflight.check_test_db_reachable(
            {"DATABASE_URL": f"postgresql://u:p@127.0.0.1:{port}/app"}
        )
        assert finding is not None
        assert finding.ok is True
        assert finding.skipped is False
        assert f"127.0.0.1:{port}" in finding.detail
    finally:
        srv.close()


def test_test_database_url_takes_priority_over_database_url():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    bad_port = s.getsockname()[1]
    s.close()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    good_port = srv.getsockname()[1]
    try:
        finding = db_preflight.check_test_db_reachable(
            {
                "DATABASE_URL": f"postgres://u:p@127.0.0.1:{bad_port}/app",
                "TEST_DATABASE_URL": f"postgres://u:p@127.0.0.1:{good_port}/app_test",
            }
        )
        assert finding.ok is True
        assert f"127.0.0.1:{good_port}" in finding.detail
        assert "TEST_DATABASE_URL" in finding.detail
    finally:
        srv.close()


def test_unparseable_url_skips_instead_of_failing():
    finding = db_preflight.check_test_db_reachable({"DATABASE_URL": "not-a-url"})

    assert finding is not None
    assert finding.ok is True
    assert finding.skipped is True


def test_password_never_leaks_into_the_skip_detail():
    # A non-numeric port fails the `.port` parse (ValueError) — the skip
    # message must still redact the credential it echoes back for context.
    finding = db_preflight.check_test_db_reachable(
        {"DATABASE_URL": "postgres://user:secret1234@host:notaport/db"}
    )

    assert finding is not None
    assert finding.skipped is True
    assert "secret1234" not in finding.detail
    assert "***" in finding.detail
