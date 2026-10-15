import hashlib
from io import BytesIO
from PIL import Image
import json
from pathlib import Path

import requests
import matplotlib.pyplot as plt
from fake_useragent import UserAgent


def update_config_authorization(token):
    """更新配置文件中的 authorization"""
    config_path = Path(__file__).parent / 'config.json'
    
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        
        config_data['authorization'] = token
        
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
    else:
        print("配置文件不存在，无法更新 authorization")


def login_and_update_config(phone=None, password=None):
    """登录并自动更新配置文件"""
    headers = {
        'User-Agent': UserAgent().random
    }
    
    if phone is not None and password is not None:
        # 1、获取ticket
        login_url = 'https://www.autodl.com/api/v1/new_login'
        sha1 = hashlib.sha1()
        sha1.update(str(password).encode('utf-8'))
        data = {
            'phone': str(phone),
            'password': sha1.hexdigest(),
            'phone_area': '+86'
        }
        response = requests.post(url=login_url, headers=headers, json=data).json()
        ticket = response['data']['ticket']
        # 2、获取token
        passport_url = 'https://www.autodl.com/api/v1/passport'
        response = requests.post(url=passport_url, headers=headers, json={'ticket': ticket}).json()
        token = response['data']['token']
        
        # 更新配置文件
        update_config_authorization(token)
        print(f"登录成功，authorization 已更新到配置文件")
        return token
        
    else:
        # 1、获取二维码和uuid
        login_url = 'https://www.autodl.com/api/v1/wx/login'
        response = requests.get(url=login_url, headers=headers).json()
        qrcode_url = response['data']['qrcode_url']
        uuid = response['data']['uuid']
        # 2、获取ticket
        response = requests.get(url=qrcode_url, headers=headers).content
        img = Image.open(BytesIO(response))
        plt.imshow(img)
        plt.axis('off')
        plt.show()
        query_url = 'https://www.autodl.com/api/v1/wx/polling/query'
        response = requests.post(url=query_url, headers=headers, json={'uuid': uuid}).json()
        ticket = response['data']['ticket']
        # 3、获取token
        passport_url = 'https://www.autodl.com/api/v1/passport'
        response = requests.post(url=passport_url, headers=headers, json={'ticket': ticket}).json()
        token = response['data']['token']
        
        # 更新配置文件
        update_config_authorization(token)
        print(f"登录成功，authorization 已更新到配置文件")
        return token


# 保持原有的 login 函数兼容性
def login(phone=None, password=None):
    """原有的登录函数，保持向后兼容"""
    return login_and_update_config(phone, password)
