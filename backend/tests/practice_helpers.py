"""测试用：结束练习（POST /api/practice/end 立即返回，复位在后台）并等后台复位完成，返回最终结果（last_result）。"""
from __future__ import annotations

import time


def end_practice(c, timeout: float = 30.0) -> dict:
    r = c.post("/api/practice/end")
    body = r.json()
    if r.status_code != 200:
        return body
    assert body.get("resetting") is True and body.get("active") is False, body
    end = time.time() + timeout
    while time.time() < end:
        cur = c.get("/api/practice/current").json()
        if not cur.get("resetting"):
            last = cur.get("last_result") or {}
            assert last.get("attempt_id") == body["attempt_id"], (body, cur)
            return last
        time.sleep(0.02)
    raise AssertionError("后台复位超时")
