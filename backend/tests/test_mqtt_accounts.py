from __future__ import annotations

import base64
import re

from app.core import mqtt_accounts as acc

UUID_A = "00112233445566778899AABB"
UUID_B = "3A0031000B51383438393735"


def test_mosquitto_hash_format():
    h = acc.mosquitto_hash("secret")
    parts = h.split("$")
    assert parts[0] == ""
    assert parts[1] == "7"
    assert parts[2] == "101"
    assert len(base64.b64decode(parts[3])) == 12
    assert len(base64.b64decode(parts[4])) == 64
    assert re.match(r"^\$7\$101\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$", h)


def test_mosquitto_hash_deterministic_with_salt_and_verifies():
    salt = b"\x01" * 12
    assert acc.mosquitto_hash("pw", salt) == acc.mosquitto_hash("pw", salt)
    assert acc.mosquitto_hash("pw", salt) != acc.mosquitto_hash("pw2", salt)
    assert acc.verify_mosquitto_hash("pw", acc.mosquitto_hash("pw"))
    assert not acc.verify_mosquitto_hash("x", acc.mosquitto_hash("pw"))
    assert not acc.verify_mosquitto_hash("pw", "garbage")


def test_render_passwd_server_devices_and_test_account():
    hashes = {UUID_B: "$7$101$b$b", UUID_A: "$7$101$a$a"}
    out = acc.render_passwd(
        hashes, server_username="server", server_password="spw",
        test_enabled=False, test_username="solarlte-test", test_password="tpw",
    )
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert lines[0].startswith("server:$7$101$")
    assert lines[1:] == [f"{UUID_A}:$7$101$a$a", f"{UUID_B}:$7$101$b$b"]  # 정렬됨
    assert "solarlte-test" not in out
    assert out.endswith("\n")


def test_render_passwd_with_test_account():
    out = acc.render_passwd(
        {}, server_username="server", server_password="spw",
        test_enabled=True, test_username="solarlte-test", test_password="tpw",
    )
    assert any(ln.startswith("solarlte-test:$7$101$") for ln in out.splitlines())


def test_render_acl_matches_spec():
    out = acc.render_acl(root="iotlight", server_username="server", test_enabled=False,
                         test_username="solarlte-test")
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert lines == [
        "user server",
        "topic readwrite iotlight/#",
        "pattern write iotlight/device/%u/register",
        "pattern write iotlight/device/%u/status",
        "pattern write iotlight/device/%u/result",
        "pattern write iotlight/device/%u/event",
        "pattern read  iotlight/device/%u/cmd",
        "pattern read  iotlight/device/%u/config",
        "pattern read  iotlight/group/#",
        "pattern read  iotlight/all/cmd",
    ]


def test_render_acl_test_account_only_when_enabled():
    on = acc.render_acl(root="iotlight", server_username="server", test_enabled=True,
                        test_username="solarlte-test")
    assert "user solarlte-test\ntopic readwrite iotlight/#" in on
    off = acc.render_acl(root="iotlight", server_username="server", test_enabled=False,
                         test_username="solarlte-test")
    assert "solarlte-test" not in off


def test_parse_accounts_csv():
    csv_text = (
        "uuid,password\n"
        f"{UUID_A},abc123\n"
        f"{UUID_B.lower()},Xy-9_\n"
        "\n"
        "BADUUID,pw\n"
        f"{UUID_A},newer\n"
        f"{UUID_B},has:colon\n"
        "onlyone\n"
    )
    result = acc.parse_accounts_csv(csv_text)
    assert result.accounts == {UUID_A: "newer", UUID_B: "Xy-9_"}
    assert len(result.errors) == 3
    assert any("UUID 형식" in e for e in result.errors)
    assert any("':'" in e for e in result.errors)


def test_content_digest_is_md5_hex():
    assert acc.content_digest("abc\n") == "0bee89b07a248e27c83fc3d5951213c1"


def test_export_disabled_when_paths_empty(monkeypatch):
    monkeypatch.setattr(acc.settings, "mosquitto_passwd_export", None)
    monkeypatch.setattr(acc.settings, "mosquitto_acl_export", None)
    result = acc.export_all({UUID_A: "$7$101$a$a"})
    assert result.enabled is False
    assert acc.acl_applied_path() is None


def test_export_writes_files_atomically(tmp_path, monkeypatch):
    passwd = tmp_path / "passwd.generated"
    aclfile = tmp_path / "aclfile.generated"
    monkeypatch.setattr(acc.settings, "mosquitto_passwd_export", str(passwd))
    monkeypatch.setattr(acc.settings, "mosquitto_acl_export", str(aclfile))
    result = acc.export_all({UUID_A: "$7$101$a$a"})
    assert result.enabled and result.passwd_ok and result.acl_ok
    assert acc.content_digest(passwd.read_text(encoding="utf-8")) == result.passwd_md5
    assert acc.content_digest(aclfile.read_text(encoding="utf-8")) == result.acl_md5
    assert acc.acl_applied_path() == tmp_path / "aclfile.applied"
    # 임시 파일이 남지 않는다
    assert sorted(p.name for p in tmp_path.iterdir()) == ["aclfile.generated", "passwd.generated"]


async def test_wait_acl_applied(tmp_path, monkeypatch):
    aclfile = tmp_path / "aclfile.generated"
    monkeypatch.setattr(acc.settings, "mosquitto_acl_export", str(aclfile))
    # 보고 파일이 없으면 기다리지 않는다
    assert await acc.wait_acl_applied("abc", timeout=0.2) is False
    marker = tmp_path / "aclfile.applied"
    marker.write_text("old\n", encoding="ascii")
    assert await acc.wait_acl_applied("abc", timeout=0.2) is False
    marker.write_text("abc\n", encoding="ascii")
    assert await acc.wait_acl_applied("abc", timeout=0.2) is True
    assert acc.read_applied_md5() == "abc"
