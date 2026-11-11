"""美編 P1-D（VISUAL_DESIGN_PLAN P1-D／brief §4 策略學習）：分組導覽＋長表精簡。

- 拆 健康摘要/三組比較/因子/模型與實驗/原始資料 分組（lrn-g data-g），頁籤切換、全部為預設。
- 長表（收集紀錄/最近樣本/分組表現）預設夾 340px 可捲動＝完整資訊仍可達；超高才出現展開鈕。
- 純版面：所有資料 id 與渲染函式不變；自動刷新只重寫 id 內容、分組與展開狀態不重置（鐵律 4）。
"""
import os
import re


def _seg():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    s = open(p, encoding="utf-8").read()
    return s, s[s.index('id="page-learning"'):s.index("<!-- Audit -->")]


def test_groups_and_nav():
    s, seg = _seg()
    assert 'id="lrn-nav"' in seg
    for g in ["health", "compare", "factor", "model", "raw"]:
        assert f'data-g="{g}"' in seg, g
    for label in ["全部", "健康摘要", "三組比較", "因子", "模型與實驗", "原始資料"]:
        assert label in seg, label
    assert "lrnNav('all',this)" in seg                    # 預設全部、零驚訝


def test_all_data_ids_preserved():
    s, seg = _seg()
    for i in ["ad-alerts", "lr-progress", "sc-calib", "factor-ic", "ad-runs", "ad-days", "ad-cov",
              "ad-recent", "lr-groups", "lr-mc", "lr-equity", "lm-out", "lr-monthly", "lr-rules",
              "lr-find", "lr-dims"]:
        assert f'id="{i}"' in seg, i                      # 渲染目標一個都不能少


def test_clamp_keeps_full_info_reachable():
    s, seg = _seg()
    assert seg.count("lrn-clamp") >= 3                    # 收集紀錄/最近樣本/分組表現
    assert ".lrn-clamp{max-height:340px;overflow:auto}" in s       # 夾高仍可捲動＝完整可達
    assert ".lrn-clamp.open{max-height:none}" in s
    assert "lrnClampSync" in s and "scrollHeight<=c.clientHeight" in s   # 不超高就不顯示展開鈕


def test_group_toggle_is_pure_display():
    s, _ = _seg()
    i = s.index("function lrnNav")
    blk = s[i:i + 400]
    assert "classList.toggle('lrn-hide'" in blk           # 純 class 切換
    assert "innerHTML" not in blk                          # 不重寫內容→刷新不重置（鐵律 4）
