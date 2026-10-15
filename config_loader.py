import json
import os
from pathlib import Path

class Config:
    def __init__(self, config_file='config.json'):
        self.config_path = Path(__file__).parent / config_file
        
        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件 {config_file} 不存在")
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.config_data = json.load(f)
        
        # 认证信息 - 允许为空或不存在
        self.Authorization = self.config_data.get('authorization', '')
        
        # 运行模式配置
        self.mode = self.config_data.get('mode', 'monitor_only')  # monitor_only 或 monitor_and_buy

        # 同地区完整克隆；不自动关停源实例，源实例必须预先关机。
        self.clone = self.config_data.get('clone', {})
        
        # GPU 配置
        self.gpu_model = self.config_data['gpu']['model']
        self.gpu_num = self.config_data['gpu']['num']
        
        # 镜像配置
        self.private_image_uuid = self.config_data['image']['private_image_uuid']
        self.image = self.config_data['image']['autodl_image']

        # 通知配置
        self.sendkey = self.config_data.get('notification', {}).get('sendkey', '')

    def has_authorization(self):
        """检查是否有有效的 authorization"""
        return bool(self.Authorization and self.Authorization.strip())
    
    def update_authorization(self, authorization):
        """更新 authorization 并保存到配置文件"""
        self.Authorization = authorization
        self.config_data['authorization'] = authorization
        
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.config_data, f, indent=2, ensure_ascii=False)

# 创建配置实例
config = Config()
