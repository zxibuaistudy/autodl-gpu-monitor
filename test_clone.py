import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import clone
import requests


class CloneTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "uuid": "instance-source", "region_sign": "nm-B1",
            "expand_data_disk_size": 2 * clone.GIB,
            "private_image_uuid": "image-private",
        }
        self.snapshot = {
            "image": {"image_type": "private", "chip_corp": "nvidia",
                      "cpu_arch": "x86", "cuda_version": 118},
            "usage_info": {"data_disk_used_size": clone.GIB},
        }
        self.machine = {
            "machine_id": "machine-target", "region_sign": "nm-B2",
            "gpu_idle_num": 2, "max_data_disk_expand_size": 4 * clone.GIB,
            "max_instance_disk_size": 50 * clone.GIB,
            "chip_corp": "nvidia", "cpu_arch": "x86", "highest_cuda_version": 124,
        }
        self.regions = [
            {"region_sign": sign, "data_center": "westDC3",
             "setting": {"clone_instance": True}}
            for sign in ("west-B", "west-C", "west-D", "west-E")
        ] + [{"region_sign": "west-X", "data_center": "westDC2",
              "setting": {"clone_instance": True}}]

    def test_full_clone_payload(self):
        body = clone.build_body(self.source, self.snapshot, self.machine,
                                ["nm-B1", "nm-B2"], 1, {"expand_data_disk_gb": 2})
        self.assertEqual(body["instance_uuid"], "instance-source")
        self.assertEqual(body["instance_info"]["machine_id"], "machine-target")
        self.assertTrue(body["instance_info"]["copy_data_disk_after_clone"])
        self.assertEqual(body["instance_info"]["expand_data_disk"], 2 * clone.GIB)
        self.assertEqual(body["price_info"]["expand_data_disk"], 2 * clone.GIB)

    def test_rejects_wrong_region_or_small_disk(self):
        with self.assertRaises(clone.CloneError):
            clone.build_body(self.source, self.snapshot,
                             {**self.machine, "region_sign": "cq-A1"},
                             ["nm-B1", "nm-B2"], 1, {"expand_data_disk_gb": 2})
        with self.assertRaises(clone.CloneError):
            clone.build_body(self.source, self.snapshot, self.machine,
                             ["nm-B1", "nm-B2"], 1, {"expand_data_disk_gb": 1})

    def test_west_e_uses_live_data_center_group(self):
        group = clone.region_group("west-E", self.regions)
        self.assertEqual(group, ["west-B", "west-C", "west-D", "west-E"])
        self.assertNotIn("west-X", group)
        with self.assertRaises(clone.CloneError):
            clone.region_group("unknown", self.regions)

    def test_cuda_versions_accept_web_and_api_formats(self):
        self.assertEqual(clone.cuda_version_parts("13.2"), (13, 2))
        self.assertEqual(clone.cuda_version_parts(132), (13, 2))
        self.assertEqual(clone.cuda_version_parts("12.8"), (12, 8))
        self.assertEqual(clone.cuda_version_parts(118), (11, 8))
        body = clone.build_body(
            self.source, {**self.snapshot, "image": {**self.snapshot["image"], "cuda_version": "12.8"}},
            {**self.machine, "highest_cuda_version": "13.2"}, ["nm-B1", "nm-B2"], 1,
            {"expand_data_disk_gb": 2},
        )
        self.assertTrue(body["instance_info"]["copy_data_disk_after_clone"])

    def test_tracks_data_disk_completion(self):
        class Client:
            def user_id(self):
                return 1

            def instances(self):
                return [{"uuid": "instance-target", "status": "shutdown",
                         "additional_info": {"copy_data_disk_after_clone_status": "Succeed"}}]

        notifications = []
        state = {"stage": "tracking", "source_instance_uuid": "instance-source",
                 "target_instance_uuid": "instance-target"}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(clone, "STATE_PATH", Path(directory) / "state.json"):
                clone.track(Client(), state, lambda title, body: notifications.append(title))
        self.assertEqual(state["stage"], "completed")
        self.assertEqual(notifications, ["实例完整克隆成功"])

    def test_uncertain_submit_is_never_repeated(self):
        class Client:
            calls = 0

            def __init__(self, authorization):
                pass

            def instances(self):
                return [{**self_source, "status": "shutdown"}]

            def machines(self, region, model, amount):
                return [self_machine]

            def regions(self):
                return [{"region_sign": "nm-B1", "data_center": "nmDC3",
                         "setting": {"clone_instance": True}},
                        {"region_sign": "nm-B2", "data_center": "nmDC3",
                         "setting": {"clone_instance": True}}]

            def snapshot(self, uuid):
                return self_snapshot

            def clone(self, body):
                Client.calls += 1
                raise requests.Timeout("simulated timeout")

        self_source, self_machine, self_snapshot = self.source, self.machine, self.snapshot
        config = SimpleNamespace(Authorization="fake", gpu_model="RTX 4090", gpu_num=1,
                                 clone={"source_instance_uuid": "instance-source",
                                        "copy_data_disk": True})
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(clone, "STATE_PATH", Path(directory) / "state.json"), \
                 patch.object(clone, "LOCK_PATH", Path(directory) / "lock"), \
                 patch.object(clone, "AutoDL", Client):
                with self.assertRaises(requests.Timeout):
                    clone.run_clone_monitor(config, lambda *args: None)
                self.assertEqual(clone.load_state()["stage"], "unknown")
                with self.assertRaises(clone.CloneError):
                    clone.run_clone_monitor(config, lambda *args: None)
        self.assertEqual(Client.calls, 1)

    def test_inventory_timeout_retries_without_submitting_in_dry_run(self):
        class Client:
            calls = 0
            clone_calls = 0

            def __init__(self, authorization):
                pass

            def instances(self):
                return [{**self_source, "status": "shutdown"}]

            def regions(self):
                return [{"region_sign": sign, "data_center": "nmDC3",
                         "setting": {"clone_instance": True}}
                        for sign in ("nm-B1", "nm-B2")]

            def machines(self, region, model, amount):
                Client.calls += 1
                if Client.calls == 1:
                    raise requests.Timeout("temporary")
                return [self_machine]

            def snapshot(self, uuid):
                return self_snapshot

            def clone(self, body):
                Client.clone_calls += 1

        self_source, self_machine, self_snapshot = self.source, self.machine, self.snapshot
        config = SimpleNamespace(Authorization="fake", gpu_model="RTX 4090", gpu_num=1,
                                 clone={"source_instance_uuid": "instance-source",
                                        "copy_data_disk": True, "dry_run": True})
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(clone, "STATE_PATH", Path(directory) / "state.json"), \
                 patch.object(clone, "LOCK_PATH", Path(directory) / "lock"), \
                 patch.object(clone, "AutoDL", Client), patch.object(clone.time, "sleep"):
                clone.run_clone_monitor(config, lambda *args: None)
                self.assertIsNone(clone.load_state())
        self.assertEqual(Client.calls, 2)
        self.assertEqual(Client.clone_calls, 0)


if __name__ == "__main__":
    unittest.main()
