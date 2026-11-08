"""Steam 凭据 DPAPI 加密存储测试。"""

from __future__ import annotations

import pytest

from qqbot.steam_vault import SteamVault, VaultError


def test_vault_roundtrip_does_not_store_plaintext(tmp_path):
    vault = SteamVault(tmp_path / "steam_vault.bin")
    assert vault.status()["configured"] is False

    vault.save("tfd-user", "tfd-pass")
    status = vault.status()
    assert status["configured"] is True
    assert status["readable"] is True

    credentials = vault.load()
    assert credentials is not None
    assert credentials.username == "tfd-user"
    assert credentials.password == "tfd-pass"

    text = (tmp_path / "steam_vault.bin").read_text(encoding="utf-8")
    assert "tfd-user" not in text
    assert "tfd-pass" not in text

    assert vault.clear() is True
    assert vault.status()["configured"] is False
    assert vault.load() is None


def test_vault_reports_corrupt_file(tmp_path):
    path = tmp_path / "steam_vault.bin"
    path.write_text('{"version":1,"ciphertext":"AAAA"}', encoding="utf-8")
    vault = SteamVault(path)
    status = vault.status()
    assert status["configured"] is True
    assert status["readable"] is False
    with pytest.raises(VaultError):
        vault.load()
