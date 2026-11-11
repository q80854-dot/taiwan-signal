"""LIVE20（第三階段審查）：停損解剖改為可驗證描述、不下事後因果結論。

不再用「方向其實沒錯／進場位置不佳／正常虧損」等事後價格推斷的因果標籤；
改為描述觀察事實（觀察窗口根數、是否曾回到進場價），並明示非因果結論。
"""
import app


def _mk(**kw):
    base = {"id": "s1", "name": "測試", "code": "2453", "direction": "buy",
            "entry_price": 100.0, "stop_loss": 95.0, "generated_at": "2026-10-01T00:00:00Z",
            "raw_json": "{}", "sl_pct": 5.0}
    base.update(kw)
    return base


def test_no_causal_labels(monkeypatch):
    # 構造「停損後回到進場價之外」的情境
    monkeypatch.setattr(app, "_bars_since", lambda *a, **k: (
        {"dates": ["2026-10-02", "2026-10-03", "2026-10-04"],
         "opens": [99, 96, 97], "highs": [99, 96, 103], "lows": [94, 95, 97],
         "closes": [94, 95, 103]}, [0, 1, 2]))
    out = app._autopsy_one(_mk())
    tags = {t["tag"] for t in out["tags"]}
    whys = " ".join(t["why"] for t in out["tags"])
    # 不得再出現事後因果字樣
    assert "方向其實沒錯" not in whys
    assert "進場位置不佳" not in whys
    assert "正常虧損" not in whys
    # 應為描述性標籤＋觀察窗口
    assert "停損後曾回到進場價之外" in tags
    assert "不等於不停損即獲利" in whys
    assert out["after"]["bars_after"] == 2


def test_neutral_tag_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(app, "_bars_since", lambda *a, **k: ({"dates": [], "opens": [],
                                                              "highs": [], "lows": [], "closes": []}, []))
    out = app._autopsy_one(_mk(sl_pct=5.0))
    tags = {t["tag"] for t in out["tags"]}
    assert "未命中可檢項目" in tags
    assert "正常停損" not in tags
