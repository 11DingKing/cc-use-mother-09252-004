"""HTTP 接口边界的端到端测试：上传、候选、评审、签发、撤回、回滚。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from service_09252_004.api import make_server
from service_09252_004.ports import ManualClock, SequentialIds
from service_09252_004.service import ReleaseService


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = ManualClock()
        self.service = ReleaseService(
            os.path.join(self.tmp.name, "releases.db"),
            clock=self.clock, id_gen=SequentialIds(),
        )
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.service.close()
        self.tmp.cleanup()

    def _request(self, method: str, path: str, payload: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_lifecycle(self) -> None:
        status, _ = self._request("GET", "/health")
        self.assertEqual(status, 200)

        # 上传组件与变体元数据；重复上传命中指纹去重。
        status, comp = self._request(
            "POST", "/components", {"name": "字幕", "kind": "subtitle", "author": "t-a"}
        )
        self.assertEqual(status, 201)
        variant_body = {
            "language": "zh-CN", "content": {"text": "你好"}, "standards": ["STD-A"],
        }
        status, var1 = self._request(
            "POST", f"/components/{comp['id']}/variants", variant_body
        )
        self.assertEqual(status, 201)
        status, var2 = self._request(
            "POST", f"/components/{comp['id']}/variants", variant_body
        )
        self.assertEqual(status, 200)
        self.assertTrue(var2["deduplicated"])
        self.assertEqual(var1["id"], var2["id"])

        # 授权（ISO 8601 时间由接口边界解析）。
        status, lic = self._request(
            "POST", "/licenses",
            {
                "component_id": comp["id"], "licensor": "t-a",
                "regions": ["EU"], "standards": ["STD-A"],
                "valid_until": "2099-01-01T00:00:00Z",
            },
        )
        self.assertEqual(status, 201)

        # 合并候选：评审人数不足时签发返回 422 与结构化原因。
        status, cand = self._request(
            "POST", "/candidates",
            {
                "course_id": "course-1", "target_region": "EU",
                "target_standard": "STD-A",
                "selections": {comp["id"]: var1["id"]}, "title": "联合课程",
            },
        )
        self.assertEqual(status, 201)
        status, body = self._request("POST", f"/candidates/{cand['id']}/publish", {})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_failed")
        issue_types = {i["type"] for i in body["error"]["details"]["issues"]}
        self.assertIn("insufficient_approvals", issue_types)

        # 并行评审后签发成功；重复签发幂等。
        for reviewer in ("alice", "bob"):
            status, _ = self._request(
                "POST", f"/candidates/{cand['id']}/reviews",
                {"reviewer": reviewer, "decision": "approve"},
            )
            self.assertEqual(status, 201)
        status, release = self._request("POST", f"/candidates/{cand['id']}/publish", {})
        self.assertEqual(status, 201)
        self.assertEqual(release["status"], "active")
        status, replay = self._request("POST", f"/candidates/{cand['id']}/publish", {})
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["id"], release["id"])

        # 撤回授权：已发布版本标记风险，历史保留。
        status, result = self._request(
            "POST", f"/licenses/{lic['id']}/withdraw", {"reason": "版权方终止授权"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["affected_release_ids"], [release["id"]])
        status, view = self._request("GET", f"/releases/{release['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(view["risk_status"], "at_risk")
        self.assertEqual(view["risk_reasons"][0]["type"], "license_withdrawn")

        # 回滚：状态流转且记录保留。
        status, rolled = self._request(
            "POST", f"/releases/{release['id']}/rollback", {"reason": "运营撤回"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(rolled["status"], "rolled_back")
        status, again = self._request(
            "POST", f"/releases/{release['id']}/rollback", {}
        )
        self.assertEqual(status, 409)
        status, listing = self._request("GET", "/releases")
        self.assertEqual(len(listing["releases"]), 1)

    def test_not_found_and_bad_routes(self) -> None:
        status, body = self._request("GET", "/components/cmp_missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        status, body = self._request("GET", "/no-such-route")
        self.assertEqual(status, 404)
        status, body = self._request("POST", "/candidates/cmp_missing/reviews",
                                     {"reviewer": "x", "decision": "approve"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
