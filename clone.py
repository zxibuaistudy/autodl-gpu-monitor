"""同地区系统盘及数据盘克隆。网页内部接口可能变化，提交结果不明时绝不重试。"""

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://www.autodl.com/api/v1"
STATE_PATH = Path(__file__).with_name("clone_state.json")
LOCK_PATH = Path(__file__).with_name("clone_state.lock")
POLL_SECONDS = 30
GIB = 1024 ** 3
TRANSIENT_NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout)


class CloneError(RuntimeError):
    pass


class APIRejected(CloneError):
    """服务端明确拒绝，未创建克隆订单。"""

    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def save_state(state):
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def load_state():
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


@contextmanager
def single_process():
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CloneError("已有一个克隆监控进程在运行") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise CloneError("已有一个克隆监控进程在运行") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class AutoDL:
    def __init__(self, authorization):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": authorization, "User-Agent": "autodl-gpu-monitor/clone"})

    def request(self, method, path, *, params=None, body=None):
        response = self.session.request(method, BASE_URL + path, params=params, json=body, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "Success":
            raise APIRejected(payload.get("code"), f"{path}: {payload.get('msg', '')}")
        return payload.get("data")

    def snapshot(self, instance_uuid):
        return self.request("GET", "/instance/snapshot", params={"instance_uuid": instance_uuid})

    def regions(self):
        data = self.request("GET", "/common")
        regions = data.get("region_list") if isinstance(data, dict) else None
        if not isinstance(regions, list):
            raise CloneError("AutoDL 未返回有效的地区目录，无法安全判断同地区")
        return regions

    def instances(self):
        rows = []
        page = 1
        while True:
            data = self.request("POST", "/instance", body={"page_index": page, "page_size": 100})
            rows.extend(data.get("list") or [])
            if page >= data.get("max_page", 1):
                return rows
            page += 1

    def user_id(self):
        return self.request("GET", "/user/detail")["id"]

    def machines(self, region_signs, model, amount):
        data = self.request("POST", "/user/machine/list", body={
            "charge_type": "payg", "default_order": True,
            "gpu_idle_num": amount, "gpu_type_name": [model],
            "page_index": 1, "page_size": 100,
            "region_sign_list": region_signs,
        })
        return data.get("list") or []

    def migration_history(self, user_id):
        data = self.request("GET", "/instance/migrate/list", params={
            "uid": user_id, "date_from": "", "date_to": "",
            "page_index": 1, "page_size": 100,
        })
        return data.get("list") or []

    def clone(self, body):
        return self.request("POST", "/order/instance/clone/payg", body=body)


def region_group(source_sign, regions):
    """以 AutoDL 的 data_center 划定同地区，未知地区不提交克隆。"""
    source = next((region for region in regions
                   if region.get("region_sign") == source_sign), None)
    if not source or not source.get("data_center"):
        raise CloneError(f"AutoDL 地区目录中找不到源地区 {source_sign}")
    if not (source.get("setting") or {}).get("clone_instance"):
        raise CloneError(f"源地区 {source_sign} 当前不支持实例克隆")
    group = [region["region_sign"] for region in regions
             if region.get("data_center") == source["data_center"]
             and (region.get("setting") or {}).get("clone_instance")
             and region.get("region_sign")]
    if not group:
        raise CloneError(f"源地区 {source_sign} 没有可克隆的目标区域")
    return group


def find_source(client, source_uuid):
    for instance in client.instances():
        if instance.get("uuid") == source_uuid or instance.get("instance_uuid") == source_uuid:
            return instance
    raise CloneError(f"找不到源实例 {source_uuid}；请确认属于当前账号")


def cuda_version_parts(value):
    """同时接受网页的 13.2 和 API 中的 132 两种 CUDA 版本格式。"""
    try:
        version = str(value).strip()
        if "." in version:
            major, minor = version.split(".", 1)
            return int(major), int(minor)
        return divmod(int(version), 10)
    except (TypeError, ValueError) as exc:
        raise CloneError(f"无法识别 CUDA 版本 {value!r}") from exc


def build_body(source, snapshot, machine, allowed_regions, amount, clone_options):
    image = snapshot.get("image") or {}
    if not isinstance(image, dict):
        raise CloneError("源实例快照缺少镜像信息")
    target_region = machine.get("region_sign")
    if target_region and target_region not in allowed_regions:
        raise CloneError("目标主机不在源实例同地区")
    source_expand = int(source.get("expand_data_disk_size") or 0)
    requested_gb = clone_options.get("expand_data_disk_gb")
    target_expand = ((source_expand + GIB - 1) // GIB if requested_gb is None
                     else int(requested_gb)) * GIB
    if target_expand < source_expand:
        raise CloneError("目标数据盘扩容小于源实例；请设置 clone.expand_data_disk_gb")
    max_expand = machine.get("max_data_disk_expand_size")
    if max_expand is not None and target_expand > int(max_expand):
        raise CloneError("目标主机不支持所需的数据盘扩容")
    if int(machine.get("gpu_idle_num") or 0) < amount:
        raise CloneError("目标主机空闲 GPU 不足")
    source_disk_used = int((source.get("usage_info") or {}).get("data_disk_used_size") or
                           (snapshot.get("usage_info") or {}).get("data_disk_used_size") or 0)
    target_base_disk = int(machine.get("max_instance_disk_size") or 0)
    if target_base_disk and source_disk_used > target_base_disk + target_expand:
        raise CloneError("目标数据盘容量小于源实例已用空间")
    if machine.get("chip_corp") and image.get("chip_corp") and machine["chip_corp"] != image["chip_corp"]:
        raise CloneError("目标主机与源实例的芯片厂商不兼容")
    if machine.get("cpu_arch") and image.get("cpu_arch") and machine["cpu_arch"] != image["cpu_arch"]:
        raise CloneError("目标主机与源实例的 CPU 架构不兼容")
    if image.get("cuda_version") and machine.get("highest_cuda_version"):
        if cuda_version_parts(machine["highest_cuda_version"]) < cuda_version_parts(image["cuda_version"]):
            raise CloneError("目标主机驱动 CUDA 版本低于源镜像要求")

    machine_id = machine["machine_id"]
    info = {
        "machine_id": machine_id,
        "charge_type": "payg",
        "req_gpu_amount": amount,
        "cg_application_info": snapshot.get("cg_application_info") or {},
        "instance_name": clone_options.get("instance_name", ""),
        "expand_data_disk": target_expand,
        "copy_data_disk_after_clone": True,
        "keep_src_user_service_address_after_clone": False,
        "reproduction_id": int(source.get("reproduction_id") or 0),
        "ft_sparse": bool(clone_options.get("optimize_sparse_files", True)),
    }
    image_fields = {
        "base_image_uuid": image.get("image_name") if image.get("image_type") == "public" else None,
        "private_image_uuid": source.get("private_image_uuid"),
        "reproduction_uuid": source.get("reproduction_uuid"),
        "cg_application_uuid": snapshot.get("cg_application_uuid"),
    }
    info.update({key: value for key, value in image_fields.items() if value})
    return {
        "instance_uuid": source.get("uuid") or source.get("instance_uuid"),
        "instance_info": info,
        "price_info": {
            "coupon_id_list": [], "machine_id": machine_id,
            "charge_type": "payg", "duration": 1, "num": amount,
            "expand_data_disk": target_expand,
        },
    }


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def notify(notifier, title, content):
    try:
        notifier(title, content)
    except Exception as exc:
        print(f"通知失败: {exc}")


def track(client, state, notifier):
    target_uuid = state["target_instance_uuid"]
    user_id = None
    while True:
        disk_status = None
        try:
            instances = client.instances()
        except TRANSIENT_NETWORK_ERRORS as exc:
            print(f"查询克隆状态时网络暂不可用，稍后重试: {exc}")
            time.sleep(POLL_SECONDS)
            continue
        target = next((row for row in instances
                       if target_uuid in (row.get("uuid"), row.get("instance_uuid"))), None)
        if target:
            status = target.get("status")
            disk_status = (target.get("additional_info") or {}).get("copy_data_disk_after_clone_status")
            clone_status = target.get("clone_status")
            print(f"克隆状态: {status}, 系统盘: {clone_status}, 数据盘: {disk_status}")
            state.update({"last_status": status, "disk_status": disk_status, "clone_status": clone_status})
            save_state(state)
            if status in {"created_clone_failed", "create_failed"} or disk_status in {"Failed", "failed"}:
                state["stage"] = "failed"
                save_state(state)
                notify(notifier, "实例克隆失败", f"目标实例 {target_uuid}，状态 {status}，数据盘 {disk_status}。请到 AutoDL 网页检查。")
                return
            if disk_status in {"Succeed", "Success", "Succeeded", "succeeded", "Finished", "finished"}:
                state["stage"] = "completed"
                save_state(state)
                notify(notifier, "实例完整克隆成功", f"源实例 {state['source_instance_uuid']} → 新实例 {target_uuid}；系统盘和数据盘复制完成。")
                return
        try:
            if user_id is None:
                user_id = client.user_id()
            history = client.migration_history(user_id)
        except (requests.RequestException, APIRejected, KeyError, ValueError) as exc:
            print(f"暂时无法查询迁移历史: {exc}")
            time.sleep(POLL_SECONDS)
            continue
        record = next((entry for entry in history if
                       entry.get("source_product_uuid") == state["source_instance_uuid"] and
                       entry.get("target_product_uuid") == target_uuid), None)
        if record:
            state["migration_status"] = record.get("status")
            save_state(state)
            if record.get("status") == "failed":
                state["stage"] = "failed"
                save_state(state)
                notify(notifier, "实例克隆失败", f"目标实例 {target_uuid} 的迁移记录显示失败，请到 AutoDL 网页检查。")
                return
        time.sleep(POLL_SECONDS)


def run_clone_monitor(config, notifier):
    options = config.clone
    source_uuid = options.get("source_instance_uuid")
    if not source_uuid:
        raise CloneError("请在 config.json 中配置 clone.source_instance_uuid")
    if options.get("copy_data_disk") is not True:
        raise CloneError("完整克隆要求 clone.copy_data_disk 为 true")
    amount = int(config.gpu_num)
    if amount < 1:
        raise CloneError("GPU 数量必须大于 0")

    with single_process():
        client = AutoDL(config.Authorization)
        state = load_state()
        skipped_machines = set()
        if state:
            if state.get("source_instance_uuid") != source_uuid:
                raise CloneError("已有其他源实例的克隆记录，请先人工核实 clone_state.json")
            if state.get("stage") == "tracking" and state.get("target_instance_uuid"):
                print("恢复已有克隆任务，继续跟踪结果")
                track(client, state, notifier)
                return
            if state.get("stage") == "rejected" and not state.get("target_instance_uuid"):
                error_code = state.get("error_code") or str(state.get("error", "")).split(":", 1)[0]
                if error_code == "FacilityNotSupported" and state.get("target_machine_id"):
                    # 兼容旧版留下的明确拒绝记录；没有目标实例，可继续找其他机器。
                    state["stage"] = "monitoring"
                    state["excluded_machine_ids"] = [state["target_machine_id"]]
                    state["error_code"] = error_code
                    save_state(state)
            if state.get("stage") == "monitoring" and not state.get("target_instance_uuid"):
                excluded = state.get("excluded_machine_ids", [])
                if not isinstance(excluded, list) or any(not isinstance(item, str) or not item for item in excluded):
                    raise CloneError("克隆状态中的目标机器排除列表无效，请人工核实 clone_state.json")
                skipped_machines.update(excluded)
                if skipped_machines:
                    print(f"继续监控，已跳过 {len(skipped_machines)} 台明确不支持克隆的目标机器")
            else:
                raise CloneError(f"已有克隆记录（{state.get('stage')}），请核实 clone_state.json 和 AutoDL 网页后再处理")

        print("开始监控同地区 GPU；源实例必须保持关机")
        while True:
            try:
                source = find_source(client, source_uuid)
                group = region_group(source.get("region_sign"), client.regions())
                machines = client.machines(group, config.gpu_model, amount)
            except TRANSIENT_NETWORK_ERRORS as exc:
                print(f"查询实例或库存时网络暂不可用，稍后重试: {exc}")
                time.sleep(POLL_SECONDS)
                continue
            if source.get("status") != "shutdown":
                raise CloneError(f"源实例状态为 {source.get('status')}；请先在网页关机")
            for machine in machines:
                if machine.get("machine_id") in skipped_machines:
                    continue
                if int(machine.get("gpu_idle_num") or 0) < amount:
                    continue
                if machine.get("region_sign") and machine["region_sign"] not in group:
                    continue
                try:
                    snapshot = client.snapshot(source_uuid)
                except TRANSIENT_NETWORK_ERRORS as exc:
                    print(f"查询源实例快照时网络暂不可用，稍后重试: {exc}")
                    break
                try:
                    body = build_body(source, snapshot, machine, group, amount, options)
                except CloneError as exc:
                    print(f"跳过机器 {machine.get('machine_id')}: {exc}")
                    continue
                if options.get("dry_run"):
                    print("演练检查通过，未提交克隆。候选目标机器:", machine["machine_id"])
                    print("请求内容:", json.dumps(body, ensure_ascii=False, indent=2))
                    return
                state = {
                    "stage": "submitting", "source_instance_uuid": source_uuid,
                    "target_machine_id": machine["machine_id"], "started_at": utc_now(),
                    "target_instance_uuid": None,
                    "excluded_machine_ids": sorted(skipped_machines),
                }
                save_state(state)
                try:
                    target_uuid = client.clone(body)
                except APIRejected as exc:
                    if exc.code in {"MachineGpuNumUpdateFailed", "FacilityNotSupported"}:
                        if exc.code == "FacilityNotSupported":
                            skipped_machines.add(machine["machine_id"])
                            print(f"跳过不支持克隆的目标机器 {machine['machine_id']}: {exc}")
                        else:
                            print(f"目标机器 {machine['machine_id']} 库存已变化: {exc}")
                        state.update({"stage": "monitoring", "error_code": exc.code,
                                      "error": str(exc),
                                      "excluded_machine_ids": sorted(skipped_machines)})
                        save_state(state)
                        continue
                    state["stage"] = "rejected"
                    state["error_code"] = exc.code
                    state["error"] = str(exc)
                    save_state(state)
                    raise
                except Exception:
                    state["stage"] = "unknown"
                    save_state(state)
                    notify(notifier, "克隆提交结果待核查", "请求结果不明，程序已停止重试。请检查 AutoDL 实例列表，避免重复创建。")
                    raise
                if not isinstance(target_uuid, str) or not target_uuid:
                    state["stage"] = "unknown"
                    save_state(state)
                    raise CloneError("克隆接口未返回目标实例 UUID；请到网页核查")
                state.update({"stage": "tracking", "target_instance_uuid": target_uuid})
                save_state(state)
                notify(notifier, "实例克隆已提交", f"源实例 {source_uuid} → 新实例 {target_uuid}，正在复制系统盘和数据盘。")
                track(client, state, notifier)
                return
            print("同地区暂无符合条件的可用机器，30 秒后重试")
            time.sleep(POLL_SECONDS)
