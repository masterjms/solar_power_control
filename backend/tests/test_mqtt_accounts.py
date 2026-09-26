from __future__ import annotations

import base64
import re

from app.core import mqtt_accounts as acc


def test_goauth_hash_format():
    h = acc.goauth_hash("server", "secret")
    parts = h.split("$")
    assert parts[0] == "PBKDF2"
    assert parts[1] == "sha512"
    assert parts[2] == "100000"
    assert len(base64.b64decode(parts[3])) == 16
    assert len(base64.b64decode(parts[4])) == 64
    assert re.match(r"^PBKDF2\$sha512\$100000\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$", h)


def test_goauth_hash_deterministic_and_verifies():
    # 같은 (username, password) → 같은 줄. 기동마다 passwd md5 가 바뀌지 않아야 한다.
    assert acc.goauth_hash("server", "pw") == acc.goauth_hash("server", "pw")
    assert acc.goauth_hash("server", "pw") != acc.goauth_hash("server", "pw2")
    # salt 가 username 에도 묶인다
    assert acc.goauth_hash("server", "pw") != acc.goauth_hash("other", "pw")
    assert acc.derive_salt("server", "pw") == acc.derive_salt("server", "pw")
    assert len(acc.derive_salt("server", "pw")) == 16
    explicit = acc.goauth_hash("server", "pw", b"\x01" * 16)
    assert explicit != acc.goauth_hash("server", "pw")
    assert acc.verify_goauth_hash("pw", explicit)
    assert acc.verify_goauth_hash("pw", acc.goauth_hash("server", "pw"))
    assert not acc.verify_goauth_hash("x", acc.goauth_hash("server", "pw"))
    assert not acc.verify_goauth_hash("pw", "garbage")
    assert not acc.verify_goauth_hash("pw", "$7$101$abc$def")  # 옛 mosquitto 형식


def test_render_passwd_is_stable_across_calls():
    kw = dict(server_username="server", server_password="spw", test_enabled=True,
              test_username="solarlte-test", test_password="tpw")
    assert acc.render_passwd(**kw) == acc.render_passwd(**kw)
    first, second = acc.render_passwd(**kw), acc.render_passwd(**kw)
    assert acc.content_digest(first) == acc.content_digest(second)


def test_render_passwd_server_only():
    out = acc.render_passwd(
        server_username="server", server_password="spw",
        test_enabled=False, test_username="solarlte-test", test_password="tpw",
    )
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert len(lines) == 1
    assert lines[0].startswith("server:PBKDF2$sha512$100000$")
    assert "solarlte-test" not in out
    assert out.endswith("\n")
    stored = lines[0].split(":", 1)[1]
    assert acc.verify_goauth_hash("spw", stored)


def test_render_passwd_with_test_account():
    out = acc.render_passwd(
        server_username="server", server_password="spw",
        test_enabled=True, test_username="solarlte-test", test_password="tpw",
    )
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert [ln.split(":")[0] for ln in lines] == ["server", "solarlte-test"]


def test_render_acl_no_pattern_lines():
    out = acc.render_acl(root="iotlight", server_username="server", test_enabled=False,
                         test_username="solarlte-test")
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert lines == ["user server", "topic readwrite iotlight/#"]
    assert "pattern" not in out


def test_render_acl_test_account_only_when_enabled():
    on = acc.render_acl(root="iotlight", server_username="server", test_enabled=True,
                        test_username="solarlte-test")
    lines = [ln for ln in on.splitlines() if ln and not ln.startswith("#")]
    assert lines == ["user server", "topic readwrite iotlight/#",
                     "user solarlte-test", "topic readwrite iotlight/#"]
    off = acc.render_acl(root="iotlight", server_username="server", test_enabled=False,
                         test_username="solarlte-test")
    assert "solarlte-test" not in off


def test_content_digest_is_md5_hex():
    assert acc.content_digest("abc\n") == "0bee89b07a248e27c83fc3d5951213c1"


def test_export_disabled_when_paths_empty(monkeypatch):
    monkeypatch.setattr(acc.settings, "mosquitto_passwd_export", None)
    monkeypatch.setattr(acc.settings, "mosquitto_acl_export", None)
    result = acc.export_all()
    assert result.enabled is False
    assert acc.acl_applied_path() is None


def test_export_writes_files_atomically(tmp_path, monkeypatch):
    passwd = tmp_path / "passwd.generated"
    aclfile = tmp_path / "aclfile.generated"
    monkeypatch.setattr(acc.settings, "mosquitto_passwd_export", str(passwd))
    monkeypatch.setattr(acc.settings, "mosquitto_acl_export", str(aclfile))
    result = acc.export_all()
    assert result.enabled and result.passwd_ok and result.acl_ok
    assert acc.content_digest(passwd.read_text(encoding="utf-8")) == result.passwd_md5
    assert acc.content_digest(aclfile.read_text(encoding="utf-8")) == result.acl_md5
    assert "server:PBKDF2$" in passwd.read_text(encoding="utf-8")
    assert acc.acl_applied_path() == tmp_path / "aclfile.applied"
    assert acc.passwd_applied_path() == tmp_path / "passwd.applied"
    # 임시 파일이 남지 않는다
    assert sorted(p.name for p in tmp_path.iterdir()) == ["aclfile.generated", "passwd.generated"]


async def test_wait_applied_needs_both_markers(tmp_path, monkeypatch):
    passwd = tmp_path / "passwd.generated"
    aclfile = tmp_path / "aclfile.generated"
    monkeypatch.setattr(acc.settings, "mosquitto_passwd_export", str(passwd))
    monkeypatch.setattr(acc.settings, "mosquitto_acl_export", str(aclfile))
    (tmp_path / "aclfile.applied").write_text("acl\n", encoding="ascii")
    (tmp_path / "passwd.applied").write_text("old\n", encoding="ascii")
    assert await acc.wait_applied("pw", "acl", timeout=0.2) is False
    (tmp_path / "passwd.applied").write_text("pw\n", encoding="ascii")
    assert await acc.wait_applied("pw", "acl", timeout=0.2) is True


async def test_wait_acl_applied(tmp_path, monkeypatch):
    aclfile = tmp_path / "aclfile.generated"
    monkeypatch.setattr(acc.settings, "mosquitto_acl_export", str(aclfile))
    assert await acc.wait_acl_applied("abc", timeout=0.2) is False
    marker = tmp_path / "aclfile.applied"
    marker.write_text("old\n", encoding="ascii")
    assert await acc.wait_acl_applied("abc", timeout=0.2) is False
    marker.write_text("abc\n", encoding="ascii")
    assert await acc.wait_acl_applied("abc", timeout=0.2) is True
    assert acc.read_applied_md5() == "abc"
