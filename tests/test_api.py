"""HTTP 接口边界测试：完整业务流程、幂等重放与错误码映射。"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from service_09252_004.api import make_server
from service_09252_004.ports import ManualClock, SequentialIds
from service_09252_004.service import CourseHubService

T0 = 1_700_000_000.0


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.svc = CourseHubService(
            str(Path(cls.tmp.name) / "api.db"),
            clock=ManualClock(T0),
            ids=SequentialIds(),
        )
        cls.server = make_server(cls.svc, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.svc.close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw) if raw else {}

    def test_full_publish_flow_with_idempotent_replays(self) -> None:
        # 健康检查
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

        # 上传组件元数据；重放返回同一记录
        comp_body = {
            "title": "联合物理课",
            "author": "三国教研组",
            "origin_region": "CN",
            "payload": {"lessons": ["L1", "L2"]},
        }
        status, body = self.request("POST", "/components", comp_body)
        self.assertEqual(status, 201)
        self.assertTrue(body["created"])
        component_id = body["component"]["component_id"]
        status, replay = self.request("POST", "/components", comp_body)
        self.assertEqual(status, 200)
        self.assertEqual(replay["component"]["component_id"], component_id)

        # 上传语言变体
        status, body = self.request(
            "POST",
            "/variants",
            {
                "component_id": component_id,
                "language": "zh",
                "region": "EU",
                "subtitles": ["zh", "en"],
                "cases": ["case-eu-1"],
                "payload": {"subtitle_track": "v1"},
            },
        )
        self.assertEqual(status, 201)
        variant_id = body["variant"]["variant_id"]

        # 登记版权授权
        status, body = self.request(
            "POST",
            "/licenses",
            {
                "component_id": component_id,
                "licensor": "版权联盟",
                "regions": ["EU"],
                "standards": ["STD-1"],
            },
        )
        self.assertEqual(status, 201)
        license_id = body["license"]["license_id"]

        # 合并候选；重放返回同一候选
        cand_body = {"region": "EU", "variant_ids": [variant_id]}
        status, body = self.request("POST", "/candidates", cand_body)
        self.assertEqual(status, 201)
        candidate_id = body["candidate"]["candidate_id"]
        status, replay = self.request("POST", "/candidates", cand_body)
        self.assertEqual(status, 200)
        self.assertEqual(replay["candidate"]["candidate_id"], candidate_id)

        # 未评审时签发被约束拦截（422）
        status, body = self.request("POST", "/releases", {"candidate_id": candidate_id})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "constraints_unsatisfied")
        self.assertTrue(body["violations"])

        # 并行评审（两名评审人）
        for reviewer in ("教研员A", "教研员B"):
            status, body = self.request(
                "POST",
                f"/candidates/{candidate_id}/reviews",
                {"reviewer": reviewer, "decision": "approve"},
            )
            self.assertEqual(status, 201)

        # 签发；重放返回同一版本
        status, body = self.request("POST", "/releases", {"candidate_id": candidate_id})
        self.assertEqual(status, 201)
        release_id = body["release"]["release_id"]
        self.assertEqual(body["release"]["status"], "published")
        status, replay = self.request("POST", "/releases", {"candidate_id": candidate_id})
        self.assertEqual(status, 200)
        self.assertEqual(replay["release"]["release_id"], release_id)

        # 撤回授权：已发布版本标出风险，历史保留
        status, body = self.request("POST", f"/licenses/{license_id}/withdraw")
        self.assertEqual(status, 200)
        self.assertEqual(body["at_risk_release_ids"], [release_id])
        status, body = self.request("GET", f"/releases/{release_id}")
        self.assertEqual(body["release"]["status"], "at_risk")
        self.assertEqual(
            [e["event"] for e in body["release"]["events"]], ["signed", "at_risk"]
        )

        # 回滚：版本保留为 rolled_back
        status, body = self.request(
            "POST", f"/releases/{release_id}/rollback", {"reason": "授权撤回"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["release"]["status"], "rolled_back")
        status, body = self.request("GET", "/releases?status=rolled_back")
        self.assertEqual(len(body["releases"]), 1)

    def test_error_codes(self) -> None:
        # 缺字段 → 400
        status, body = self.request("POST", "/components", {"title": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation_error")
        # 非法 JSON → 400
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/components", body="{not json", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 400)
        # 不存在的资源 → 404
        status, _ = self.request("GET", "/components/cmp_missing")
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/no/such/route")
        self.assertEqual(status, 404)
        # 同一组件两个变体合并 → 409
        status, body = self.request(
            "POST",
            "/components",
            {"title": "冲突课", "author": "组", "origin_region": "CN", "payload": {"x": 1}},
        )
        cid = body["component"]["component_id"]
        variant_ids = []
        for lang in ("zh", "en"):
            status, body = self.request(
                "POST",
                "/variants",
                {"component_id": cid, "language": lang, "region": "EU"},
            )
            variant_ids.append(body["variant"]["variant_id"])
        status, body = self.request(
            "POST", "/candidates", {"region": "EU", "variant_ids": variant_ids}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "conflict")


if __name__ == "__main__":
    unittest.main()
