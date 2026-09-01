#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import io
import urllib.parse
import requests
import time
from datetime import datetime, timedelta, timezone
from PIL import Image
import numpy as np
from playwright.sync_api import sync_playwright

# ================= 配置区 =================
# ---- 多账号配置（按优先级自动加载，任选其一） ----
# 1. 环境变量 DISCORD_TOKENS：JSON 数组字符串
#    DISCORD_TOKENS='[{"name":"账号1","token":"xxxx"},{"name":"账号2","token":"yyyy"}]'
# 2. 环境变量 DISCORD_TOKEN_1 / DISCORD_TOKEN_2 / ...（编号自动收集）
# 3. 本地 accounts.json 文件（与 DISCORD_TOKENS 同结构，适合本地运行）
# 4. 单个 DISCORD_TOKEN（旧版单账号兼容）
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
ACCOUNTS_FILE = os.environ.get("ACCOUNTS_FILE", "accounts.json")

# TG 通知（可选）
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")

# 网站根域

SITE_BASE = "https://openworld.eu.org"

# 续期天数阈值：剩余天数 <= 此值时才执行续期
RENEW_THRESHOLD_DAYS = 5
# ==========================================

# 截图保存目录（调试用）
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", ".")


def mask_token(token: str) -> str:
    """打码显示 token，避免日志泄露"""
    if len(token) <= 8:
        return "***"
    return token[:4] + "***" + token[-4:]


def load_accounts() -> list:
    """
    按优先级加载多账号配置：
    1. DISCORD_TOKENS（JSON 数组）
    2. DISCORD_TOKEN_1 ~ DISCORD_TOKEN_N（编号）
    3. accounts.json 本地文件
    4. 单个 DISCORD_TOKEN（旧版兼容）
    """
    accounts = []

    def _append(data, base_index):
        """从 JSON 数据中提取账号（支持 {name, token} 字典或纯 token 字符串）"""
        out = []
        for i, item in enumerate(data, base_index):
            if isinstance(item, str):
                out.append({"name": f"账号{i}", "token": item.strip()})
            elif isinstance(item, dict):
                name = str(item.get("name") or f"账号{i}").strip()
                token = str(item.get("token", "")).strip()
                out.append({"name": name, "token": token})
        return out

    # 1. DISCORD_TOKENS 环境变量（JSON）
    tokens_json = os.environ.get("DISCORD_TOKENS", "").strip()
    if tokens_json:
        try:
            data = json.loads(tokens_json)
            if isinstance(data, list):
                accounts = _append(data, 1)
                print(f"✅ 从 DISCORD_TOKENS 加载 {len(accounts)} 个账号")
        except Exception as e:
            print(f"⚠️ DISCORD_TOKENS 解析失败: {e}")

    # 2. DISCORD_TOKEN_N 编号环境变量
    if not accounts:
        i = 1
        while True:
            tok = os.environ.get(f"DISCORD_TOKEN_{i}", "").strip()
            if not tok:
                break
            accounts.append({"name": f"账号{i}", "token": tok})
            i += 1
        if accounts:
            print(f"✅ 从 DISCORD_TOKEN_1..{i-1} 加载 {len(accounts)} 个账号")

    # 3. 本地 accounts.json
    if not accounts and os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("accounts", [])
            if isinstance(data, list):
                accounts = _append(data, 1)
                print(f"✅ 从 {ACCOUNTS_FILE} 加载 {len(accounts)} 个账号")
        except Exception as e:
            print(f"⚠️ {ACCOUNTS_FILE} 解析失败: {e}")

    # 4. 旧版单账号兼容
    if not accounts and DISCORD_TOKEN:
        accounts = [{"name": "账号1", "token": DISCORD_TOKEN.strip()}]
        print("✅ 使用单个 DISCORD_TOKEN（旧版兼容模式）")

    # 去重 + 过滤空 token
    seen, result = set(), []
    for acc in accounts:
        if not acc["token"] or acc["token"] in seen:
            continue
        seen.add(acc["token"])
        result.append(acc)
    return result


def send_telegram_message(message: str):
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")


def save_screenshot(page, name: str):
    """已禁用 PNG 截图保存（仅保留原始验证码 GIF 文件）"""
    pass


def wait_for_cloudflare(page, timeout=15):
    """
    等待 Cloudflare 挑战通过。
    如果页面包含 CF 挑战指示器，等待其消失。
    """
    cf_indicators = ["verify you are human", "just a moment", "checking your browser",
                     "cf-browser-verification", "challenge-platform"]
    start = time.time()
    while time.time() - start < timeout:
        try:
            content = page.content().lower()
            if not any(indicator in content for indicator in cf_indicators):
                return True
        except Exception:
            pass
        time.sleep(1)
    print("⚠️ Cloudflare 挑战等待超时")
    return False


def login_with_discord_token(page, dc_token: str) -> bool:
    """
    通过 Discord Token 完成 OAuth 登录到 openworld.eu.org。
    
    流程：
    1. 访问 /discord-login 触发服务端 302 重定向到 Discord OAuth 页面
    2. 从重定向后的 URL 中提取 OAuth 参数（client_id, redirect_uri, scope, state）
    3. 使用 Discord Token 通过 API 直接完成授权
    4. 用返回的回调 URL 完成登录
    """
    print("=" * 50)
    print("🔑 开始 Discord OAuth 登录流程")
    print("=" * 50)

    # ========== 第1步：触发 Discord OAuth 重定向 ==========
    # openworld.eu.org 的登录按钮指向 /discord-login，
    # 服务端会 302 重定向到 Discord 的 OAuth2 授权页面
    discord_login_url = f"{SITE_BASE}/discord-login"
    print(f"\n📌 第1步：访问 Discord 登录入口: {discord_login_url}")

    try:
        # 先访问首页建立基础 cookie/session
        page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
        time.sleep(2)
        print(f"   首页加载完成，当前 URL: {page.url}")

        # 访问 /discord-login，这会触发 302 到 Discord
        page.goto(discord_login_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
    except Exception as e:
        print(f"   ⚠️ 页面加载异常: {e}")
        # 即使超时也可能已经跳转了，继续检查

    current_url = page.url
    print(f"   跳转后 URL: {current_url}")

    # ========== 第2步：检查是否到达了 Discord 授权页 ==========
    print(f"\n📌 第2步：检查 Discord OAuth 页面")

    # 如果还在 openworld 的登录页，尝试点击 Discord 按钮
    if "discord.com" not in current_url:
        print("   未自动跳转到 Discord，尝试在登录页查找 Discord 按钮...")
        save_screenshot(page, "before_discord_click")

        try:
            # 查找登录页上的 Discord 登录链接/按钮
            discord_btn = page.locator("a[href*='discord-login'], a[href*='discord'], a:has-text('Discord')").first
            if discord_btn.is_visible(timeout=5000):
                href = discord_btn.get_attribute("href")
                print(f"   找到 Discord 按钮，href={href}")
                discord_btn.click()
                time.sleep(5)
                current_url = page.url
                print(f"   点击后 URL: {current_url}")
        except Exception as e:
            print(f"   ⚠️ 查找/点击 Discord 按钮失败: {e}")

    # 再次检查
    if "discord.com" not in current_url:
        # 最后尝试：有些网站的 /discord-login 可能需要处理 Cloudflare
        print("   仍未到达 Discord，等待可能的延迟重定向...")
        for i in range(10):
            time.sleep(1)
            current_url = page.url
            if "discord.com" in current_url:
                break
        
        if "discord.com" not in current_url:
            print(f"   ❌ 无法跳转到 Discord 授权页面")
            print(f"   当前 URL: {current_url}")
            print(f"   页面标题: {page.title()}")
            save_screenshot(page, "login_failed_no_discord")
            return False

    # ========== 第3步：从 URL 解析 OAuth 参数 ==========
    print(f"\n📌 第3步：解析 OAuth 参数")
    oauth_url = page.url
    print(f"   Discord OAuth URL: {oauth_url[:100]}...")

    parsed = urllib.parse.urlparse(oauth_url)
    params = urllib.parse.parse_qs(parsed.query)

    client_id    = params.get("client_id", [""])[0]
    redirect_uri = params.get("redirect_uri", [""])[0]
    scope        = params.get("scope", ["identify email"])[0]
    state        = params.get("state", [""])[0]
    response_type = params.get("response_type", ["code"])[0]

    print(f"   Client ID:    {client_id}")
    print(f"   Redirect URI: {redirect_uri}")
    print(f"   Scope:        {scope}")
    print(f"   State:        {state[:20]}..." if state else "   State:        (空)")

    if not client_id or not redirect_uri:
        print("   ❌ 无法解析关键 OAuth 参数 (client_id 或 redirect_uri)")
        save_screenshot(page, "login_failed_parse")
        return False

    # ========== 第4步：通过 API 完成 Discord 授权 ==========
    print(f"\n📌 第4步：通过 Discord API 完成授权")

    # 构建 API URL
    api_params = urllib.parse.urlencode({
        "client_id":     client_id,
        "response_type": response_type,
        "redirect_uri":  redirect_uri,
        "scope":         scope,
        "state":         state,
    })
    authorize_api = f"https://discord.com/api/v9/oauth2/authorize?{api_params}"

    # 构建 referer
    referer_params = urllib.parse.urlencode({
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": response_type,
        "scope":         scope,
        "state":         state,
    })
    referer = f"https://discord.com/oauth2/authorize?{referer_params}"

    headers = {
        "accept":           "*/*",
        "authorization":    dc_token.strip(),
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        "x-discord-locale": "zh-CN",
    }

    body = {
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    }

    try:
        resp = requests.post(authorize_api, headers=headers, json=body, timeout=20)
        print(f"   API 响应状态码: {resp.status_code}")

        if resp.status_code != 200:
            print(f"   ❌ Discord 授权失败: HTTP {resp.status_code}")
            print(f"   响应内容: {resp.text[:300]}")
            return False

        resp_data = resp.json()
    except Exception as e:
        print(f"   ❌ Discord API 请求异常: {e}")
        return False

    location = resp_data.get("location", "")
    if not location:
        print(f"   ❌ 授权响应中未找到 location 字段")
        print(f"   响应内容: {json.dumps(resp_data, ensure_ascii=False)[:300]}")
        return False

    masked_location = re.sub(r"code=[^&]+", "code=***", location)
    print(f"   ✅ 拿到回调 URL: {masked_location}")

    # ========== 第5步：用回调 URL 完成登录 ==========
    print(f"\n📌 第5步：通过回调 URL 完成登录写入 Cookie")

    try:
        page.goto(location, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"   ⚠️ 回调页面加载异常（可能正常）: {e}")

    time.sleep(5)
    wait_for_cloudflare(page)

    final_url = page.url
    print(f"   回调后 URL: {final_url}")

    # 检查是否登录成功
    if "/login" in final_url and "discord" not in final_url:
        print("   ⚠️ 回调后仍在登录页，登录可能失败")
        save_screenshot(page, "login_callback_stuck")
        # 有些情况下需要等待更久
        time.sleep(5)
        final_url = page.url
        if "/login" in final_url:
            print(f"   ❌ 登录最终失败，停留在: {final_url}")
            return False

    if "openworld.eu.org" in final_url:
        print(f"   ✅ 登录成功！当前 URL: {final_url}")
        save_screenshot(page, "login_success")
        return True

    print(f"   ⚠️ 登录状态不确定，当前 URL: {final_url}")
    save_screenshot(page, "login_uncertain")
    # 尝试继续，后续访问 VPS 页面会验证
    return True


def extract_gif_frames(gif_bytes: bytes) -> list:
    """提取 GIF 所有帧为 PIL Image 列表"""
    gif = Image.open(io.BytesIO(gif_bytes))
    frames = []
    try:
        while True:
            frame = gif.convert("L")  # 转灰度
            frames.append(frame.copy())
            gif.seek(gif.tell() + 1)
    except EOFError:
        pass
    print(f"   📊 成功提取 GIF 共 {len(frames)} 帧")
    return frames


def preprocess_frame(img: Image.Image) -> Image.Image:
    """对单帧图像进行预处理：二值化 + 放大"""
    threshold = 170
    binary = img.point(lambda p: 0 if p < threshold else 255, "L")
    w, h = binary.size
    binary = binary.resize((w * 2, h * 2), Image.LANCZOS)
    return binary


def _split_segments(frame):
    """按列投影自动切分一帧为若干字形段 (不依赖固定百分比/不重叠)

    返回 [(x0, x1, crop), ...], crop 为带边距的字形图像。
    """
    w, h = frame.size
    binary = frame.point(lambda p: 0 if p < 170 else 255, "L")
    px = binary.load()
    col_sum = []
    for x in range(w):
        cnt = 0
        for y in range(h):
            if px[x, y] < 128:
                cnt += 1
        col_sum.append(cnt)

    segs = []
    start = None
    for x in range(w):
        if col_sum[x] > 0:
            if start is None:
                start = x
        else:
            if start is not None:
                segs.append([start, x - 1])
                start = None
    if start is not None:
        segs.append([start, w - 1])
    if not segs:
        return []

    # 合并间隙 <= 2px 的相邻段 (笔画断裂/噪点)
    merged = [segs[0]]
    for s in segs[1:]:
        if s[0] - merged[-1][1] <= 2:
            merged[-1][1] = s[1]
        else:
            merged.append(s)

    crops = []
    for x0, x1 in merged:
        l = max(0, x0 - 3)
        r = min(w, x1 + 3)
        crops.append((x0, x1, frame.crop((l, 0, r, h))))
    return crops


def _classify_segment_text(res):
    """把 ddddocr 识别结果分类为 数字 / 运算符 / 混合

    返回 (kind, value):
      - ("num", "12")  纯数字
      - ("op", "*")    运算符
      - ("mixed", (digits, ops))  数字和运算符粘连
      - ("unknown", raw)
    """
    s = (res or "").strip()
    if not s:
        return ("unknown", "")
    replaced = (s.replace('O', '0').replace('o', '0').replace('l', '1')
                 .replace('I', '1').replace('S', '5').replace('s', '5')
                 .replace('B', '8').replace('g', '9').replace('q', '9')
                 .replace('z', '2').replace('Z', '2'))
    if re.fullmatch(r'[0-9]+', replaced):
        return ("num", replaced)

    ops = ""
    for ch in s:
        if ch in "+-*/":
            ops += ch
        elif ch in ("x", "X", "×"):
            ops += "*"
        elif ch in ("÷", ":"):
            ops += "/"
        elif ch in ("一", "—", "–", "-"):
            ops += "-"
        elif ch in ("十", "t", "T"):
            ops += "+"
    digits = "".join(ch for ch in replaced if ch.isdigit())
    if ops and digits:
        return ("mixed", (digits, ops))
    if ops:
        return ("op", ops)
    if digits:
        return ("num", digits)
    return ("unknown", s)


def recognize_captcha_by_frames(gif_bytes: bytes, ocr) -> str:
    """
    分解帧识别验证码：
    1. 获取每一帧。
    2. 按列投影自动切分字形段 (代替固定百分比裁剪, 避免区域重叠误判)。
    3. 逐段分类为数字/运算符, 按"运算符前=左数字, 运算符后=右数字"拼接。
    4. 跨帧投票取最高频结果, 组合成算式并求解。
    """
    frames = extract_gif_frames(gif_bytes)
    if not frames:
        return ""

    left_candidates = []   # 数字A候选
    op_candidates = []     # 运算符候选
    right_candidates = []  # 数字B候选

    for idx, frame in enumerate(frames):
        segs = _split_segments(frame)
        if not segs:
            continue

        # 逐段识别并分类
        seq = []  # [(kind, value), ...]
        for x0, x1, crop in segs:
            proc_img = preprocess_frame(crop)
            img_buf = io.BytesIO()
            proc_img.save(img_buf, format="PNG")
            res = ocr.classification(img_buf.getvalue()).strip()
            if not res:
                continue
            kind, val = _classify_segment_text(res)
            if kind == "num":
                seq.append(("num", val))
            elif kind == "op":
                seq.append(("op", val))
            elif kind == "mixed":
                digits, ops = val
                if digits:
                    seq.append(("num", digits))
                for o in ops:
                    seq.append(("op", o))

        if not seq:
            continue

        # 第一个运算符作为分界: 之前=左数字A, 之后=右数字B
        op_idx = None
        for i, (k, v) in enumerate(seq):
            if k == "op":
                op_idx = i
                break
        if op_idx is None:
            continue
        num_a = "".join(v for k, v in seq[:op_idx] if k == "num")
        num_b = "".join(v for k, v in seq[op_idx + 1:] if k == "num")
        op = seq[op_idx][1]
        if num_a and num_b:
            left_candidates.append(num_a)
            op_candidates.append(op)
            right_candidates.append(num_b)

    # 跨帧统计最高频的左数字、运算符、右数字
    from collections import Counter

    num_a = Counter(left_candidates).most_common(1)[0][0] if left_candidates else ""
    op = Counter(op_candidates).most_common(1)[0][0] if op_candidates else ""
    num_b = Counter(right_candidates).most_common(1)[0][0] if right_candidates else ""

    print(f"   🔍 跨帧区域统计结果 -> 左数字(A): '{num_a}' | 运算符: '{op}' | 右数字(B): '{num_b}'")

    # 拼接算式并求解
    if num_a and num_b:
        # 如果运算符没识别出来，默认加法或减法尝试
        if not op:
            op = "+"
        expr = f"{num_a}{op}{num_b}"
        try:
            val = int(eval(expr))
            print(f"   🧮 算式求解成功: {expr} = {val}")
            return str(val)
        except Exception as e:
            print(f"   ⚠️ 计算异常 ({expr}): {e}")

    # 如果区域切分没拿到结果，尝试全图逐帧识别
    all_text = []
    for frame in frames:
        proc_img = preprocess_frame(frame)
        img_buf = io.BytesIO()
        proc_img.save(img_buf, format="PNG")
        res = ocr.classification(img_buf.getvalue()).strip()
        # 清理常见错别字
        cleaned = re.sub(r'[^0-9+\-*/]', '', res.replace('x', '*').replace('X', '*').replace('O', '0').replace('o', '0').replace('l', '1'))
        if cleaned:
            all_text.append(cleaned)
            
    if all_text:
        most_common_full = Counter(all_text).most_common(1)[0][0]
        match = re.search(r'(\d+)\s*([+\-*/])\s*(\d+)', most_common_full)
        if match:
            a, o, b = match.groups()
            val = int(eval(f"{a}{o}{b}"))
            print(f"   🧮 全图统计求解: {a}{o}{b} = {val}")
            return str(val)

    return ""


def download_captcha_gif(page) -> bytes:
    """
    从页面中获取验证码 GIF 图片的原始字节数据。
    重点处理 blob: URL —— 必须在浏览器上下文内 fetch 才能拿到完整的多帧 GIF。
    """
    import base64

    captcha_selectors = [
        "img[alt='Captcha']",
        "img[alt='captcha']",
        "img[src*='captcha']",
        ".captcha img",
    ]

    captcha_element = None
    for selector in captcha_selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=5000):
                captcha_element = el
                print(f"   找到验证码元素 (选择器: {selector})")
                break
        except Exception:
            continue

    if not captcha_element:
        print("   ❌ 未找到验证码图片")
        return None

    src = captcha_element.get_attribute("src") or ""
    print(f"   📥 验证码 src: {src[:100]}")

    # ========== 方法1：blob: URL —— 在浏览器内 fetch 获取完整 GIF ==========
    if src.startswith("blob:"):
        print("   📦 检测到 blob: URL，通过浏览器内 fetch 获取完整 GIF...")
        try:
            b64_data = page.evaluate("""
                async (blobUrl) => {
                    try {
                        const resp = await fetch(blobUrl);
                        const arrayBuffer = await resp.arrayBuffer();
                        const bytes = new Uint8Array(arrayBuffer);
                        let binary = '';
                        for (let i = 0; i < bytes.length; i++) {
                            binary += String.fromCharCode(bytes[i]);
                        }
                        return btoa(binary);
                    } catch (e) {
                        return null;
                    }
                }
            """, src)
            if b64_data:
                gif_bytes = base64.b64decode(b64_data)
                print(f"   ✅ 通过 blob fetch 获取成功 ({len(gif_bytes)} bytes)")
                return gif_bytes
            else:
                print("   ⚠️ blob fetch 返回空")
        except Exception as e:
            print(f"   ⚠️ blob fetch 失败: {e}")

    # ========== 方法2：普通 http/https URL —— 用 requests 下载 ==========
    elif src.startswith("http"):
        try:
            cookies = page.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            resp = requests.get(src, cookies=cookie_dict, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 100:
                print(f"   ✅ HTTP 下载成功 ({len(resp.content)} bytes)")
                return resp.content
            else:
                print(f"   ⚠️ HTTP 下载失败: {resp.status_code}, {len(resp.content)} bytes")
        except Exception as e:
            print(f"   ⚠️ HTTP 下载异常: {e}")

    # ========== 方法3：相对路径 URL ==========
    elif src.startswith("/"):
        full_url = f"{SITE_BASE}{src}"
        try:
            cookies = page.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            resp = requests.get(full_url, cookies=cookie_dict, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 100:
                print(f"   ✅ 相对路径下载成功 ({len(resp.content)} bytes)")
                return resp.content
        except Exception as e:
            print(f"   ⚠️ 相对路径下载异常: {e}")

    # ========== 方法4：data: URL ==========
    elif src.startswith("data:"):
        try:
            # data:image/gif;base64,xxxxx
            b64_part = src.split(",", 1)[1]
            gif_bytes = base64.b64decode(b64_part)
            print(f"   ✅ data: URL 解码成功 ({len(gif_bytes)} bytes)")
            return gif_bytes
        except Exception as e:
            print(f"   ⚠️ data: URL 解码失败: {e}")

    # ========== 回退：元素截图（只能拍当前帧，最后手段） ==========
    print("   ⚠️ 所有下载方式失败，回退到元素截图（只能获取单帧）")
    try:
        return captcha_element.screenshot()
    except Exception as e:
        print(f"   ❌ 截图也失败了: {e}")
        return None


def try_renew_captcha(page, initial_days: int, max_attempts=5) -> bool:
    """
    尝试执行验证码续期流程，最多重试 max_attempts 次。
    以提交后剩余天数是否增加到 6 天来判断续期是否真正成功。
    返回 True 表示续期成功。
    """
    try:
        import ddddocr
    except ImportError:
        print("   ⚠️ ddddocr 未安装，无法执行验证码识别")
        print("   请运行: pip install ddddocr")
        return False

    ocr = ddddocr.DdddOcr(show_ad=False)

    for attempt in range(1, max_attempts + 1):
        print(f"\n   {'='*40}")
        print(f"   🔄 第 {attempt}/{max_attempts} 次尝试")
        print(f"   {'='*40}")

        try:
            # ========== 第1步：点击 Renew free 按钮打开弹窗 ==========
            print("   🔍 寻找并点击 [Renew free] 按钮...")
            renew_selectors = [
                "button:has-text('Renew free')",
                "button:has-text('Renew')",
                "a:has-text('Renew free')",
                "a:has-text('Renew')",
                "[class*='renew']",
            ]
            clicked = False
            for selector in renew_selectors:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=3000):
                        btn_text = btn.inner_text()
                        print(f"   找到按钮: '{btn_text}' (选择器: {selector})")
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                print("   ❌ 未找到可用的续期按钮")
                return False
            time.sleep(3)

            # ========== 第2步：下载并识别验证码 ==========
            print("   ⏳ 等待验证码图片加载...")
            time.sleep(1)

            gif_bytes = download_captcha_gif(page)
            if not gif_bytes:
                print("   ⚠️ 未获取到验证码图片")
                continue

            # 保存原始 GIF（调试用）
            gif_path = os.path.join(SCREENSHOT_DIR, f"captcha_raw_{attempt}.gif")
            try:
                with open(gif_path, "wb") as f:
                    f.write(gif_bytes)
                print(f"   💾 原始验证码已保存: {gif_path}")
            except Exception:
                pass

            # 分解帧识别算式并求解
            answer = recognize_captcha_by_frames(gif_bytes, ocr)
            if not answer:
                print("   ⚠️ 验证码识别求解失败，刷新重试...")
                # 刷新页面恢复干净状态
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                continue

            print(f"   📝 最终计算答案: {answer}")

            # ========== 第3步：填入并提交 ==========
            input_selectors = [
                "input[placeholder='Answer']",
                "input[placeholder='answer']",
                "input[name='captcha']",
                "input[name='answer']",
                "input[type='text']",
            ]

            input_filled = False
            for selector in input_selectors:
                try:
                    inp = page.locator(selector).first
                    if inp.is_visible(timeout=3000):
                        inp.fill("")  # 先清空
                        inp.fill(answer)
                        input_filled = True
                        print(f"   ✅ 答案已填入: {answer} (选择器: {selector})")
                        break
                except Exception:
                    continue

            if not input_filled:
                print("   ❌ 未找到验证码输入框")
                continue

            # 提交
            confirm_selectors = [
                "button:has-text('Confirm Renewal')",
                "button:has-text('Confirm')",
                "button:has-text('Submit')",
                "button[type='submit']",
            ]

            submitted = False
            for selector in confirm_selectors:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        submitted = True
                        print(f"   ✅ 已点击提交按钮 (选择器: {selector})")
                        break
                except Exception:
                    continue

            if not submitted:
                print("   ❌ 未找到提交按钮")
                continue

            # ========== 第4步：等待提交完成并刷新页面读取真实天数 ==========
            print("   ⏳ 等待提交请求处理完成...")
            time.sleep(4)

            print("   🔄 刷新页面验证最新剩余天数...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"   ⚠️ 页面刷新异常: {e}")
            wait_for_cloudflare(page)
            time.sleep(2)

            # 重新读取页面中的剩余天数
            page_text = page.locator("body").inner_text()
            match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", page_text)

            if match:
                new_days = int(match.group(1))
                print(f"   📊 刷新后最新剩余天数: {new_days} 天")

                if new_days >= 6:
                    print(f"   ✅ 续期成功！天数已从 {initial_days} 天更新为 {new_days} 天")
                    return True
                else:
                    print(f"   ❌ 续期失败！天数仍为 {new_days} 天（未达到 6 天），验证码可能填错，准备重试...")
                    continue
            else:
                print("   ⚠️ 页面刷新后无法解析剩余天数")
                continue

        except Exception as e:
            print(f"   ❌ 第 {attempt} 次尝试发生错误: {e}")
            continue

    print(f"   ❌ {max_attempts} 次尝试均失败")
    return False


def get_vps_urls(page) -> list:
    """
    自动从当前页面或控制面板/仪表盘中寻找用户绑定的 VPS 详情页 URL。
    """
    vps_urls = []

    def extract_vps_links():
        found = []
        try:
            links = page.locator("a[href*='/vps/']").all()
            for link in links:
                href = link.get_attribute("href") or ""
                if href:
                    full_url = urllib.parse.urljoin(SITE_BASE, href)
                    path = urllib.parse.urlparse(full_url).path.rstrip('/')
                    if path != "/vps" and full_url not in found:
                        found.append(full_url)
        except Exception as e:
            print(f"   ⚠️ 提取 VPS 链接异常: {e}")
        return found

    print("\n🔍 正在自动识别账号下的 VPS 实例...")
    # 1. 先从当前登录落地页提取
    vps_urls = extract_vps_links()

    # 2. 如果没有，前往首页 SITE_BASE
    if not vps_urls:
        try:
            print(f"   前往首页 {SITE_BASE} 提取实例列表...")
            page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
            wait_for_cloudflare(page)
            time.sleep(3)
            vps_urls = extract_vps_links()
        except Exception as e:
            print(f"   ⚠️ 前往首页提取失败: {e}")

    # 3. 如果还是没有，尝试访问 /dashboard 或 /vps
    if not vps_urls:
        for sub_path in ["/dashboard", "/vps"]:
            try:
                url = f"{SITE_BASE}{sub_path}"
                print(f"   尝试访问 {url} 提取实例列表...")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                wait_for_cloudflare(page)
                time.sleep(3)
                vps_urls = extract_vps_links()
                if vps_urls:
                    break
            except Exception:
                pass

    if vps_urls:
        print(f"   ✅ 成功检测到 {len(vps_urls)} 个 VPS 实例:")
        for u in vps_urls:
            print(f"      - {u}")
    else:
        print("   ❌ 未能在控制面板自动检测到任何 VPS 实例页面")

    return vps_urls


def process_account(browser, account):
    """
    处理单个账号：独立浏览器上下文 -> 登录 -> 检测 VPS -> 逐台续期。
    返回统计字典，单账号异常不会影响其他账号。
    """
    name = account["name"]
    token = account["token"]

    print(f"\n{'#' * 60}")
    print(f"# 🧑‍💻 处理账号: {name}")
    print(f"{'#' * 60}")

    # 每个账号使用独立 context，隔离 cookie/缓存
    context = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 720},
    )
    page = context.new_page()

    stats = {"name": name, "status": "未知", "vps": 0, "renewed": 0, "skipped": 0, "failed": 0}

    try:
        # ========== 登录 ==========
        success = login_with_discord_token(page, token)

        if not success:
            print(f"\n❌ [{name}] 登录流程失败，跳过该账号。")
            send_telegram_message(f"❌ Openworld 续期失败：账号 {name} 登录流程失败")
            stats["status"] = "登录失败"
            return stats

        # ========== 自动检测 VPS 列表 ==========
        target_vps_list = get_vps_urls(page)
        stats["vps"] = len(target_vps_list)

        if not target_vps_list:
            print(f"\n❌ [{name}] 未能从面板自动检测到任何 VPS 实例。")
            print("💡 请检查账号是否有活跃的 VPS 实例")
            save_screenshot(page, "no_vps_found")
            send_telegram_message(f"❌ Openworld 续期失败：账号 {name} 未在面板找到任何 VPS 实例")
            stats["status"] = "无VPS实例"
            return stats

        # 遍历每个 VPS 实例进行续期检测
        for idx, target_url in enumerate(target_vps_list, 1):
            print(f"\n{'=' * 50}")
            print(f"📌 [{name}] [{idx}/{len(target_vps_list)}] 导航到目标 VPS 页面: {target_url}")
            print(f"{'=' * 50}")

            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"⚠️ 页面加载异常: {e}")

            wait_for_cloudflare(page)
            time.sleep(3)

            current_url = page.url
            page_title = page.title()
            print(f"📝 当前 URL: {current_url}")
            print(f"📝 页面标题: {page_title}")

            # 验证是否真正到达了 VPS 页面（而非被重定向到登录页）
            if "/login" in current_url:
                print("❌ 被重定向到登录页，Cookie 可能无效")
                save_screenshot(page, f"redirect_to_login_{name}_{idx}")
                send_telegram_message(f"❌ Openworld 续期失败：账号 {name} 登录后仍被重定向到登录页")
                stats["failed"] += 1
                break

            page_text = page.locator("body").inner_text()

            # 检查是否 404 Page Not Found
            if "404" in page_title or "Page Not Found" in page_title or "doesn't exist" in page_text.lower():
                print(f"❌ [{name}] 目标 VPS 页面不存在或无权访问 (404 Not Found): {target_url}")
                print("⚠️ 原因分析: 此 URL 对应的机器可能已被注销或不存在。")
                save_screenshot(page, f"vps_404_{name}_{idx}")
                send_telegram_message(f"❌ Openworld 续期失败：账号 {name} 页面 404 Not Found\nURL: {target_url}")
                stats["failed"] += 1
                continue

            if "/vps/" not in current_url:
                print(f"⚠️ 当前页面可能不是 VPS 详情页: {current_url}")
                save_screenshot(page, f"not_vps_page_{name}_{idx}")

            print("✅ 已成功到达目标 VPS 页面")
            save_screenshot(page, f"vps_page_loaded_{name}_{idx}")

            # ========== 检查剩余天数 ==========
            match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", page_text)

            if match:
                days_left = int(match.group(1))
                print(f"🔍 [{name}] 当前 VPS 剩余续期时间: {days_left} 天")

                if days_left > RENEW_THRESHOLD_DAYS:
                    msg = f"⏳ 剩余 {days_left} 天 > {RENEW_THRESHOLD_DAYS} 天阈值，跳过续期"
                    print(msg)
                    send_telegram_message(f"ℹ️ Openworld 无需续期\n账号: {name}\n实例: {target_url}\n剩余时间: {days_left} 天")
                    stats["skipped"] += 1
                    continue
                else:
                    print(f"⚠️ 剩余 {days_left} 天 ≤ {RENEW_THRESHOLD_DAYS} 天，开始执行续期...")
            else:
                print("⚠️ 未能从页面提取剩余天数，将强制尝试续期")
                print(f"   页面文本片段: {page_text[:500]}")
                days_left = 0  # 未知天数，强制尝试续期

            # ========== 执行续期 ==========
            print(f"\n{'=' * 50}")
            print("🔄 开始执行验证码续期")
            print(f"{'=' * 50}")

            renew_success = try_renew_captcha(page, initial_days=days_left)

            if renew_success:
                # 计算续期后的到期时间（当前时间 + 6天）
                expiry_time = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=6)
                expiry_str = expiry_time.strftime("%Y-%m-%d %H:%M:%S") + " (GMT+8)"
                msg = f"✅ Openworld 续期成功！\n账号: {name}\n实例: {target_url}\n天数已更新为 6 天\n续期至: {expiry_str}"
                print(f"✅ 续期成功！天数已更新为 6 天")
                print(f"📅 续期至: {expiry_str}")
                send_telegram_message(msg)
                stats["renewed"] += 1
            else:
                print("❌ 续期失败（5次尝试均未成功）")
                send_telegram_message(f"❌ Openworld 续期失败：5次验证码尝试均未成功\n账号: {name}\n实例: {target_url}")
                stats["failed"] += 1

        stats["status"] = "完成"
        return stats

    except Exception as e:
        print(f"\n💥 [{name}] 账号处理发生未捕获异常: {e}")
        import traceback
        traceback.print_exc()
        save_screenshot(page, f"uncaught_error_{name}")
        send_telegram_message(f"❌ Openworld 续期脚本异常（账号 {name}）: {str(e)[:200]}")
        stats["status"] = f"异常: {str(e)[:50]}"
        return stats

    finally:
        context.close()


def main():
    print("#" * 50)
    print("   Openworld VPS 自动续期脚本（多账号版）")
    print("#" * 50)

    accounts = load_accounts()
    if not accounts:
        print("❌ 未找到任何账号配置，请检查：")
        print("   1. 环境变量 DISCORD_TOKENS（JSON 数组）")
        print("   2. 环境变量 DISCORD_TOKEN_1 / DISCORD_TOKEN_2 ...（编号）")
        print("   3. 本地 accounts.json 文件")
        print("   4. 单个 DISCORD_TOKEN（旧版兼容）")
        sys.exit(1)

    print(f"👥 共加载 {len(accounts)} 个账号:")
    for acc in accounts:
        print(f"   - {acc['name']}: {mask_token(acc['token'])}")

    headless_mode = os.environ.get("HEADLESS", "true").lower() == "true"
    print(f"🖥️  运行模式: {'无头' if headless_mode else '有头'}")
    print("🎯 每个账号登录后将自动从面板检测 VPS 实例并逐一续期")

    results = []
    with sync_playwright() as p:
        # 使用更真实的浏览器配置以避免被检测
        browser = p.chromium.launch(
            headless=headless_mode,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        try:
            # 串行处理每个账号（各自独立 context，cookie 互不干扰）
            for acc in accounts:
                results.append(process_account(browser, acc))
        except Exception as e:
            print(f"\n💥 脚本发生未捕获异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            browser.close()

    # ========== 汇总统计 ==========
    print("\n" + "#" * 60)
    print("📊 多账号执行汇总")
    print("#" * 60)

    summary_lines = []
    for r in results:
        line = (f"👤 {r['name']}: 状态={r['status']} | VPS={r['vps']} | "
                f"续期成功={r['renewed']} | 跳过={r['skipped']} | 失败={r['failed']}")
        print("  " + line)
        summary_lines.append(line)

    total_vps = sum(r["vps"] for r in results)
    total_renewed = sum(r["renewed"] for r in results)
    total_failed = sum(r["failed"] for r in results)
    print(f"\n🏁 全部执行完毕: {len(results)} 个账号, 共 {total_vps} 台 VPS, "
          f"成功续期 {total_renewed} 台, 失败 {total_failed} 台")

    if len(results) > 1:
        summary_msg = "📊 Openworld 多账号续期汇总\n" + "\n".join(summary_lines)
        send_telegram_message(summary_msg)

    # 有失败项时以非零退出码结束，便于在 GitHub Actions 中标记失败
    if total_failed > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
