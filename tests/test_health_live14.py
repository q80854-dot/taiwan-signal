"""LIVE14（第三階段審查）：健檢假綠燈。

unknown（檢查無法完成/取不到資料）不得計 pass——有 unknown 時整體至少 warn、不顯示「全部正常」；
na（不適用）不影響 verdict；掃描錯誤>0 必為 warn 並誠實標明「僅存錯誤數、可能影響母體」。
Telegram 告警只在 verdict=fail 觸發，unknown 升 warn 不會製造新告警。
"""
import health


_ALL = ("check_infra", "check_security", "check_freshness", "check_data",
        "check_fundamentals", "check_engine", "check_strategy")


def _patch_all(monkeypatch, overrides):
    for name in _ALL:
        items = overrides.get(name, [health._c(name, "ok", "")])
        monkeypatch.setattr(health, name, (lambda it: (lambda: it))(items))


def test_all_ok_verdict_ok(monkeypatch):
    _patch_all(monkeypatch, {})
    h = health.run_health()
    assert h["verdict"] == "ok" and h["counts"]["unknown"] == 0


def test_unknown_not_counted_as_pass(monkeypatch):
    _patch_all(monkeypatch, {"check_data": [health._c("來源X", "unknown", "取不到資料")]})
    h = health.run_health()
    # 有 unknown → 不得「全部正常」，整體至少 warn；unknown 單獨計數、不混入 ok
    assert h["verdict"] == "warn"
    assert h["counts"]["unknown"] == 1 and h["counts"]["ok"] == 6
    assert h["counts"]["fail"] == 0            # unknown 不是 fail → 不觸發 Telegram 告警


def test_na_does_not_affect_verdict(monkeypatch):
    _patch_all(monkeypatch, {"check_data": [health._c("來源Y", "na", "此部署不適用")]})
    h = health.run_health()
    assert h["verdict"] == "ok" and h["counts"]["na"] == 1


def test_fail_still_wins(monkeypatch):
    _patch_all(monkeypatch, {"check_engine": [health._c("引擎", "fail", "壞了"),
                                              health._c("другое", "unknown", "x")]})
    h = health.run_health()
    assert h["verdict"] == "fail"


def test_scan_errors_flagged_with_population_note(monkeypatch):
    import state_store
    monkeypatch.setattr(state_store.store, "get_scan_history",
                        lambda n=3: [{"scan_date": "2026-11-11", "scanned": 100, "signals_found": 2,
                                      "signals_sent": 2, "duration_min": 3.0, "errors": 3}])
    items = health.check_engine()
    scan = next(i for i in items if i["name"] == "最近一次掃描")
    assert scan["status"] == "warn"
    # ★ 2026-11-12 缺口補齊後：無 errors_detail 的舊列誠實標「無逐類別明細（2026-11-12 起記錄）」
    assert "3" in scan["detail"] and "母體" in scan["detail"] and "無逐類別明細" in scan["detail"]


def test_scan_no_errors_ok(monkeypatch):
    import state_store
    monkeypatch.setattr(state_store.store, "get_scan_history",
                        lambda n=3: [{"scan_date": "2026-11-11", "scanned": 100, "signals_found": 2,
                                      "signals_sent": 2, "duration_min": 3.0, "errors": 0}])
    items = health.check_engine()
    scan = next(i for i in items if i["name"] == "最近一次掃描")
    assert scan["status"] == "ok" and "母體" not in scan["detail"]


def test_material_news_fetch_fail_is_unknown_not_fail(monkeypatch):
    """LIVE14 補漏：重大訊息來源取得失敗＝檢查無法完成→unknown（升 warn、不觸發 fail 告警），
    不是系統異常 fail。先前用 'fail' 會讓來源短暫失敗就發「系統健檢異常」Telegram 告警。"""
    import fundamentals
    monkeypatch.setattr(fundamentals, "fetch_monthly_revenue_map", lambda: {str(i): {"period": "2026-10"} for i in range(1600)})
    monkeypatch.setattr(fundamentals, "fetch_valuation_map", lambda: {str(i): {} for i in range(1600)})
    monkeypatch.setattr(fundamentals, "material_news_fetch_ok", lambda: False)   # 來源取得失敗
    monkeypatch.setattr(fundamentals, "fetch_material_news_risk_map", lambda: {})
    items = health.check_fundamentals()
    mn = [i for i in items if i["name"] == "重大訊息（僅上市）"]
    assert mn and mn[0]["status"] == "unknown"          # 取得失敗＝unknown，非 fail
    assert "非系統異常" in mn[0]["detail"]
    # 整體 verdict：unknown 升 warn、不是 fail → 不觸發 Telegram 告警
    _patch_all(monkeypatch, {"check_fundamentals": items})
    h = health.run_health()
    assert h["verdict"] == "warn" and h["counts"]["fail"] == 0
