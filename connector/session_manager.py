import json
import time
import os
import base64
import requests
from typing import Optional
from .config import BOT_CONFIG, SERVER_CONFIG, COOKIE_CACHE_FILE


class SessionManager:
    def __init__(self):
        self._cached_cookie: Optional[str] = None
        self._cookie_timestamp: float = 0
        self._load_cache()

    def _load_cache(self):
        if COOKIE_CACHE_FILE.exists():
            try:
                with open(COOKIE_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._cached_cookie = data.get("cookie")
                    self._cookie_timestamp = data.get("timestamp", 0)
            except Exception:
                pass

    def _save_cache(self, cookie: str):
        try:
            with open(COOKIE_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "cookie": cookie,
                    "timestamp": time.time()
                }, f)
        except Exception as e:
            print(f"[Session] 保存cookie缓存失败: {e}")

    def _login(self, oat: str = None) -> Optional[str]:
        username = BOT_CONFIG["username"]
        # Use provided oat, or fallback to the one in config
        ticket = oat or BOT_CONFIG["access_token"]
        login_url = SERVER_CONFIG["login_url"]

        session = requests.Session()
        login_json = {"oa_ticket": ticket, "username": username}

        try:
            print(f"[Session] 尝试登录: {username}")
            response = session.post(login_url, data=login_json, timeout=10)
            print(f"[Session] 登录响应: HTTP {response.status_code}")
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    print(f"[Session] 登录JSON: {data}")
                    if not data.get("success", False):
                        print("[Session] 登录失败: API返回success为false")
                        return None
                except Exception as json_err:
                    print(f"[Session] 解析登录响应JSON失败: {json_err}")
                
                cookies = session.cookies.get_dict()
                print(f"[Session] 获取到的cookies: {cookies}")
                if "session" in cookies:
                    cookie_str = "session=" + cookies["session"]
                    print("[Session] 登录成功，获取新session")
                    if self._test_cookie_online(cookie_str):
                        print("[Session] 新cookie线上验证成功")
                        return cookie_str
                    else:
                        print("[Session] 新cookie线上验证失败")
                        return None
                else:
                    print("[Session] 登录失败: 未获取到session cookie")
            else:
                print(f"[Session] 登录失败: HTTP {response.status_code}, 响应文本: {response.text[:100]}")
        except Exception as e:
            print(f"[Session] 登录请求异常: {e}")
        return None

    def _test_cookie_online(self, cookie: str) -> bool:
        http_base = SERVER_CONFIG["http_base"]
        test_url = f"{http_base}/api/user"

        try:
            response = requests.get(test_url, headers={"Cookie": cookie}, timeout=5)
            print(f"[Session] 线上验证cookie: HTTP {response.status_code}")
            if response.status_code == 200:
                try:
                    data = response.json()
                    print(f"[Session] 验证响应: {data}")
                    return data.get("success", False) if "success" in data else True
                except Exception as e:
                    print(f"[Session] 解析JSON失败: {e}")
                    return True
            return False
        except Exception as e:
            print(f"[Session] 验证请求异常: {e}")
            return False

    def _get_cookie_exp(self, cookie: str) -> float:
        """解析JWT cookie获取过期时间，返回0表示解析失败"""
        try:
            if cookie.startswith("session="):
                cookie = cookie[8:]
            parts = cookie.split('.')
            if len(parts) != 3:
                return 0
            payload_b64 = parts[1]
            # 补充 base64 padding
            payload_b64 += '=' * (-len(payload_b64) % 4)
            payload_json = base64.urlsafe_b64decode(payload_b64).decode('utf-8')
            payload = json.loads(payload_json)
            return payload.get("exp", 0)
        except Exception as e:
            print(f"[Session] 解析JWT失败: {e}")
            return 0

    def _renew_cookie(self, old_cookie: str) -> Optional[str]:
        http_base = SERVER_CONFIG["http_base"]
        oats_api_url = f"{http_base}/api/oats"
        
        try:
            print("[Session] 自动续签开始，清理旧的 OAT...")
            # 1. 获取现有 OAT 列表
            list_resp = requests.get(oats_api_url, headers={"Cookie": old_cookie}, timeout=10)
            if list_resp.status_code == 200:
                oats_data = list_resp.json()
                if oats_data.get("success"):
                    tickets = oats_data.get("tickets", [])
                    for t in tickets:
                        t_id = t.get("id")
                        print(f"[Session] 清理旧 OAT (ID: {t_id})...")
                        requests.delete(f"{oats_api_url}?id={t_id}", headers={"Cookie": old_cookie}, timeout=5)
            
            # 2. 创建新的 OAT
            print("[Session] 尝试创建新的 OAT...")
            response = requests.post(oats_api_url, headers={"Cookie": old_cookie}, data={"label": "Endra Auto Renew"}, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    new_oat = data.get("ticket")
                    print(f"[Session] 成功获取新的OAT: {new_oat}")
                    # 3. 使用新 OAT 登录
                    return self._login(oat=new_oat)
            print(f"[Session] 获取新OAT失败: {response.text}")
        except Exception as e:
            print(f"[Session] 自动续签异常: {e}")
        return None

    def get_session(self, force_refresh: bool = False) -> Optional[str]:
        if not force_refresh and self._cached_cookie:
            exp = self._get_cookie_exp(self._cached_cookie)
            now = time.time()
            if exp > now:
                # 还有效，检查是否不足 10 天 (10天 = 864000秒)
                if exp - now < 864000:
                    print("[Session] 缓存cookie有效期不足10天，尝试自动续签...")
                    new_cookie = self._renew_cookie(self._cached_cookie)
                    if new_cookie:
                        self._cached_cookie = new_cookie
                        self._cookie_timestamp = time.time()
                        self._save_cache(new_cookie)
                return self._cached_cookie
            else:
                print("[Session] 缓存cookie已过期，需要重新登录")
                
        cookie = self._login()
        if cookie:
            self._cached_cookie = cookie
            self._cookie_timestamp = time.time()
            self._save_cache(cookie)
        return cookie

    def invalidate(self):
        self._cached_cookie = None
        self._cookie_timestamp = 0
        if COOKIE_CACHE_FILE.exists():
            COOKIE_CACHE_FILE.unlink()


session_manager = SessionManager()
