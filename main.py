import gpu
from config_loader import config
from login_helper import login_and_update_config
from serverchan_sdk import sc_send
import time
import json

import watch
from clone import run_clone_monitor

# 检查是否有 authorization，如果没有则登录
if not config.has_authorization():
    print("检测到配置文件中没有 authorization，开始登录...")
    # 执行登录 - 这会自动更新 config.json 文件
    token = login_and_update_config()
    # 更新当前配置实例
    config.Authorization = token
    print("登录完成，继续执行后续操作...")

print("检测到有效的 authorization，开始执行主要功能...")

def send_notification(title, content, gpu_info=None, wallet_info=None):
    """发送通知"""
    message_data = {
        "title": title,
        "content": content,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    
    if gpu_info:
        message_data["gpu_info"] = gpu_info
    if wallet_info:
        message_data["wallet_info"] = wallet_info
    
    # 格式化消息内容
    desp = f"""
时间: {message_data['timestamp']}
内容: {content}
"""
    
    if gpu_info:
        desp += f"\nGPU信息:\n{json.dumps(gpu_info, indent=2, ensure_ascii=False)}"
    
    # 发送消息
    if config.sendkey and config.sendkey != "...":
        options = {"tags": "GPU监控|autogpu"}
        response = sc_send(config.sendkey, title, desp, options)
        return response
    else:
        print("未配置sendkey，跳过通知发送")
        return {"status": "skipped", "message": "未配置sendkey"}

# 根据配置模式选择运行方式
if config.mode == "monitor_and_clone":
    run_clone_monitor(config, send_notification)

elif config.mode == "monitor_only":
    print("运行模式: 仅监控")
    print("开始监控GPU...")
    
    while True:
        gpu_info = watch.watch_gpu(config=config, gpu=config.gpu_model)
        print("GPU监控结果:")
        print(gpu_info)
        
        # 检查是否有可用的GPU
        available_gpus = []
        for region, machines in gpu_info.items():
            if machines:  # 如果该区域有可用机器
                available_gpus.extend(machines)
        
        if available_gpus:
            print(f"发现可用GPU: {len(available_gpus)} 台")
            # 发送通知
            response = send_notification(
                title=f"GPU可用通知 - {config.gpu_model}",
                content=f"发现 {len(available_gpus)} 台可用的 {config.gpu_model}",
                gpu_info=gpu_info
            )
            print("通知发送结果:")
            print(response)
            break
        else:
            print("暂无可用GPU，继续监控...")
            time.sleep(30)  # 等待30秒后继续监控

elif config.mode == "monitor_and_buy":
    print("运行模式: 监控并购买")
    print("开始监控GPU...")
    
    while True:
        gpu_info = watch.watch_gpu(config=config, gpu=config.gpu_model)
        print("GPU监控结果:")
        print(gpu_info)
        
        # 检查是否有可用的GPU
        available_gpus = []
        for region, machines in gpu_info.items():
            if machines:  # 如果该区域有可用机器
                available_gpus.extend(machines)
        
        if available_gpus:
            # 购买gpu
            result = gpu.buy(config=config,
                           gpu=config.gpu_model,
                           gpu_num=config.gpu_num,
                           image=config.image,
                           private_image_uuid=config.private_image_uuid)
            
            if result['success']:
                # 只有购买成功才发送通知
                response = send_notification(
                    title=f"GPU购买成功 - {config.gpu_model}",
                    content=f"成功购买 {config.gpu_num} 台 {config.gpu_model}\n区域: {result['region_name']}\n价格: {result['price']}元/小时\n机器ID: {result['machine_id']}",
                    gpu_info=result['gpu_info'],
        
                )
                print("购买成功通知发送结果:")
                print(response)
                break
            else:
                # 购买失败，不发送通知，继续监控
                print(f"购买失败: {result['message']}")
                print("继续监控...")
                time.sleep(10)  # 等待10秒后继续尝试
        else:
            print("暂无可用GPU，继续监控...")
            time.sleep(30)  # 等待30秒后继续监控

else:
    print(f"未知的运行模式: {config.mode}")
    print("支持的模式: monitor_only, monitor_and_buy, monitor_and_clone")
