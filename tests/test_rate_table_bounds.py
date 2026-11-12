"""限流表上限行為（2026-11-12，承接 security/hardening-auth-rate-limits 分支仍缺的部分）。

原本表滿時「整張清空」：只要湊滿上限個來源，所有人的限流計數（含正在被限流的攻擊者）都被歸零。
改為只清已過期的鍵；清完仍滿就拒絕新來源，既有來源的限流不受影響。
"""
import time
from collections import deque

import admin_auth
import protect


def test_protect_bucket_prunes_expired_instead_of_clearing():
    now = time.time()
    bucket = {f"old{i}": deque([now - 999]) for i in range(8000)}        # 全部已過期
    bucket["attacker"] = deque([now] * 5)                                # 仍在限流中的來源
    assert protect._hit(bucket, "newcomer", limit=5, window=60) is True  # 清掉過期鍵後可容納新來源
    assert "attacker" in bucket and len(bucket["attacker"]) == 5         # 既有計數沒有被歸零
    assert "old0" not in bucket and len(bucket) == 2
    assert protect._hit(bucket, "attacker", limit=5, window=60) is False


def test_protect_bucket_refuses_new_keys_when_full_of_live_entries():
    now = time.time()
    bucket = {f"live{i}": deque([now]) for i in range(8000)}
    assert protect._hit(bucket, "newcomer", limit=5, window=60) is False  # 表滿且皆有效：不新增、不清空
    assert len(bucket) == 8000 and "newcomer" not in bucket
    assert protect._hit(bucket, "live0", limit=5, window=60) is True      # 既有來源照常計數


def test_members_auth_rate_does_not_reset_live_limits(monkeypatch):
    import members
    now = time.time()
    monkeypatch.setattr(members, "_ip", lambda: "9.9.9.9")
    monkeypatch.setattr(members, "_AUTH_RATE", {f"k{i}": [now] for i in range(5000)})
    assert members.auth_rate_ok(limit=30, window=600) is False           # 表滿且皆有效：拒絕新來源
    assert len(members._AUTH_RATE) == 5000                               # 沒有被整張清空
    monkeypatch.setattr(members, "_AUTH_RATE", {f"k{i}": [now - 9999] for i in range(5000)})
    assert members.auth_rate_ok(limit=30, window=600) is True            # 過期鍵被清掉後可用
    assert list(members._AUTH_RATE) == ["9.9.9.9"]


def test_admin_fail_table_is_bounded_and_keeps_no_empty_entries(monkeypatch):
    monkeypatch.setattr(admin_auth, "_fails", {})
    monkeypatch.setattr(admin_auth, "_gfails", [])
    assert admin_auth._retry_after("1.1.1.1") == 0
    assert admin_auth._fails == {}                                       # 沒失敗過的來源不留空殼
    now = time.time()
    monkeypatch.setattr(admin_auth, "_fails", {f"ip{i}": [now] for i in range(5000)})
    admin_auth._record_fail("newcomer")
    assert "newcomer" not in admin_auth._fails and len(admin_auth._fails) == 5000
    assert len(admin_auth._gfails) == 1                                  # 全域限流仍累計
    admin_auth._record_fail("ip0")
    assert len(admin_auth._fails["ip0"]) == 2                            # 既有來源照常累計


def test_shadow_export_has_owner_guard_in_depth():
    """匯出端點除了集中登入牆與後台驗證，再加站主專屬裝飾器（縱深防禦，與其他站主端點一致）。"""
    src = open("app.py", encoding="utf-8").read()
    i = src.index('@app.route("/api/admin/shadow/export.csv")')
    head = src[i:i + 160]
    assert "@members.owner_required" in head and "@admin_auth.admin_required" in head
