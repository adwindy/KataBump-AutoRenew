#!/usr/bin/env python3
"""
KataBump 自动续期脚本
Playwright + CDP 绕过 Cloudflare Turnstile
支持 Hysteria2/SOCKS5/HTTP 代理, 多账号, Telegram 通知
"""

import os, sys, json, re, time, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

DASHBOARD_URL = "https://dashboard.katabump.com"
LOGIN_URL = f"{DASHBOARD_URL}/auth/login"
LOGOUT_URL = f"{DASHBOARD_URL}/auth/logout"
SCREENSHOT_DIR = Path(os.environ.get("SCREENSHOT_DIR", "screenshots"))
TZ_CN = timezone(timedelta(hours=8))

HY2_PROXY_URL = os.environ.get("HY2_PROXY_URL", "")
HTTP_PROXY = os.environ.get("HTTP_PROXY", "")
SOCKS_PORT = os.environ.get("SOCKS_PORT", "51080")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")


def log(msg):
    ts = datetime.now(TZ_CN).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def send_tg(text, image_path=None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    from urllib.request import Request, urlopen
    try:
        data = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}).encode()
        req = Request(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", data=data)
        req.add_header("Content-Type", "application/json")
        urlopen(req, timeout=10)
        if image_path and Path(image_path).exists():
            subprocess.run(
                f'curl -s -X POST "https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto" '
                f'-F chat_id="{TG_CHAT_ID}" -F photo="@{image_path}"',
                shell=True, timeout=15
            )
        log("TG 通知已发")
    except Exception as e:
        log(f"TG 通知失败: {e}")


TURNSTILE_INJECT = """
(function() {
    if (window.self === window.top) return;
    try {
        let screenX = Math.floor(Math.random() * 400 + 800);
        let screenY = Math.floor(Math.random() * 200 + 400);
        Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
        Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
    } catch (e) {}
    try {
        const orig = Element.prototype.attachShadow;
        Element.prototype.attachShadow = function(init) {
            const root = orig.call(this, init);
            if (root) {
                const check = () => {
                    const cb = root.querySelector('input[type="checkbox"]');
                    if (cb) {
                        const r = cb.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && window.innerWidth > 0 && window.innerHeight > 0) {
                            window.__turnstile_data = {
                                xRatio: (r.left + r.width / 2) / window.innerWidth,
                                yRatio: (r.top + r.height / 2) / window.innerHeight
                            };
                            return true;
                        }
                    }
                    return false;
                };
                if (!check()) {
                    new MutationObserver(() => { if (check()) this.disconnect(); })
                        .observe(root, { childList: true, subtree: true });
                }
            }
            return root;
        };
    } catch (e) {}
})();
"""


def get_users():
    raw = os.environ.get("USERS_JSON", "")
    if not raw:
        log("❌ 未配置 USERS_JSON"); return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else data.get("users", [])
    except json.JSONDecodeError as e:
        log(f"❌ USERS_JSON 格式错误: {e}"); return []


def setup_proxy():
    """启动 Hysteria2 代理，返回 Playwright proxy dict"""
    if HTTP_PROXY:
        log(f"🌐 HTTP 代理: {HTTP_PROXY[:30]}...")
        return {"server": HTTP_PROXY}
    if HY2_PROXY_URL:
        log(f"📡 启动 Hysteria2 (端口 {SOCKS_PORT})...")
        try:
            proc = subprocess.Popen(
                ["hysteria", "client", "--server", HY2_PROXY_URL,
                 "--socks5", f"127.0.0.1:{SOCKS_PORT}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            time.sleep(3)
            if proc.poll() is not None:
                log("❌ Hysteria2 启动失败"); return None
            log("✅ Hysteria2 代理已启动")
            return {"server": f"socks5://127.0.0.1:{SOCKS_PORT}"}
        except FileNotFoundError:
            log("⚠️ hysteria 未安装，跳过代理"); return None
    return None


async def cdp_click_turnstile(page):
    """CDP 协议点击 Turnstile checkbox"""
    import asyncio, random
    for frame in page.frames:
        try:
            data = await frame.evaluate("window.__turnstile_data")
            if not data:
                continue
            el = await frame.frame_element()
            box = await el.bounding_box()
            if not box:
                continue
            cx = box["x"] + box["width"] * data["xRatio"]
            cy = box["y"] + box["height"] * data["yRatio"]
            log(f"  >> CDP 点击: ({cx:.1f}, {cy:.1f})")
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": cx, "y": cy,
                "button": "left", "clickCount": 1
            })
            await asyncio.sleep(0.05 + random.random() * 0.1)
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": cx, "y": cy,
                "button": "left", "clickCount": 1
            })
            await cdp.detach()
            return True
        except Exception:
            pass
    return False


async def wait_turnstile_ok(page, timeout=10):
    import asyncio
    for _ in range(timeout):
        for frame in page.frames:
            if "cloudflare" in (frame.url or ""):
                try:
                    if await frame.get_by_text("Success!", exact=False).is_visible(timeout=500):
                        return True
                except Exception:
                    pass
        await asyncio.sleep(1)
    return False


async def renew_user(page, user):
    import asyncio, random
    username = user.get("username", "")
    password = user.get("password", "")
    safe = re.sub(r'[^a-z0-9]', '_', username, flags=re.I)

    log(f"\n{'='*40}")
    log(f"处理: {username[:3]}***@{username.split('@')[-1] if '@' in username else '?'}")

    try:
        # 登出
        if "dashboard" in page.url:
            await page.goto(LOGOUT_URL); await page.wait_for_timeout(2000)
        await page.goto(LOGIN_URL); await page.wait_for_timeout(2000)
        if "dashboard" in page.url and "login" not in page.url:
            await page.goto(LOGOUT_URL); await page.wait_for_timeout(2000)
            await page.goto(LOGIN_URL); await page.wait_for_timeout(2000)

        # 填表
        log("填入凭据...")
        await page.get_by_role("textbox", name="Email").wait_for(state="visible", timeout=5000)
        await page.get_by_role("textbox", name="Email").fill(username)
        await page.get_by_role("textbox", name="Password").fill(password)
        await page.wait_for_timeout(500)

        # 登录前 Turnstile
        log("  >> 登录页 Turnstile...")
        for _ in range(15):
            if await cdp_click_turnstile(page):
                break
            await page.wait_for_timeout(1000)
        await wait_turnstile_ok(page, 10)

        # 登录
        await page.get_by_role("button", name="Login", exact=True).click()

        # 检查密码错误
        try:
            if await page.get_by_text("Incorrect password or no account").is_visible(timeout=3000):
                log("  ❌ 账号或密码错误")
                shot = str(SCREENSHOT_DIR / f"{safe}_login_fail.png")
                await page.screenshot(path=shot, full_page=True)
                send_tg(f"❌ *KataBump 登录失败*\n用户: `{username}`\n原因: 账号或密码错误", shot)
                return False
        except Exception:
            pass

        # 进入服务器页
        server_id = user.get("server_id", "")
        log("寻找服务器...")
        try:
            # 方式1: 如果有 server_id，直接导航
            if server_id:
                log(f"  导航到服务器 {server_id}...")
                await page.goto(f"{DASHBOARD_URL}/server/{server_id}")
                await page.wait_for_timeout(3000)
            else:
                # 方式2: 在 dashboard 页面找链接
                # 尝试多种可能的链接文字
                found = False
                for link_text in ["See", "View", "Manage", "Open", "Details"]:
                    try:
                        link = page.get_by_role("link", name=link_text).first
                        await link.wait_for(state="visible", timeout=3000)
                        await page.wait_for_timeout(1000)
                        await link.click()
                        found = True
                        log(f"  找到 '{link_text}' 链接")
                        break
                    except Exception:
                        continue
                if not found:
                    # 方式3: 尝试点击任何服务器卡片/行
                    try:
                        # KataBump 可能用卡片或表格行显示服务器
                        card = page.locator(".server-card, .card, tr.server-row, [data-server-id]").first
                        await card.wait_for(state="visible", timeout=5000)
                        await card.click()
                        found = True
                        log("  点击了服务器卡片")
                    except Exception:
                        pass
                if not found:
                    log("  ❌ 未找到服务器入口")
                    shot = str(SCREENSHOT_DIR / f"{safe}_no_server.png")
                    await page.screenshot(path=shot, full_page=True)
                    send_tg(f"❌ *KataBump 未找到服务器*\n用户: `{username}`", shot)
                    return False
        except Exception as e:
            log(f"  ❌ 进入服务器失败: {e}")
            shot = str(SCREENSHOT_DIR / f"{safe}_nav_fail.png")
            try: await page.screenshot(path=shot, full_page=True)
            except: pass
            return False

        # --- 续期循环 (最多 20 次) ---
        for att in range(1, 21):
            log(f"\n[尝试 {att}/20] Renew...")
            renew_btn = page.get_by_role("button", name="Renew", exact=True).first
            try:
                await renew_btn.wait_for(state="visible", timeout=5000)
            except Exception:
                log("无 Renew 按钮"); break

            await renew_btn.click()
            modal = page.locator("#renew-modal")
            try:
                await modal.wait_for(state="visible", timeout=5000)
            except Exception:
                continue

            # 晃鼠标
            try:
                b = await modal.bounding_box()
                if b:
                    await page.mouse.move(b["x"]+b["width"]/2, b["y"]+b["height"]/2, steps=5)
            except Exception:
                pass

            # 模态框 Turnstile
            log("  >> 模态框 Turnstile...")
            cdp_ok = False
            for fa in range(30):
                if await cdp_click_turnstile(page):
                    cdp_ok = True; break
                if fa % 5 == 4:
                    log(f"  >> [{fa+1}/30] 等待...")
                await page.wait_for_timeout(1000)

            if cdp_ok:
                await page.wait_for_timeout(8000)
                await wait_turnstile_ok(page, 5)
            else:
                log("  >> 未找到 Turnstile")

            # 截图
            try:
                await page.screenshot(path=str(SCREENSHOT_DIR / f"{safe}_ts_{att}.png"), full_page=True)
            except Exception:
                pass

            # 点击确认
            confirm = modal.get_by_role("button", name="Renew")
            if not await confirm.is_visible():
                await page.reload(); await page.wait_for_timeout(3000); continue

            log("  >> 点击确认...")
            await confirm.click()

            # 检查结果
            captcha_err = False; done = False
            t0 = time.time()
            while time.time() - t0 < 3:
                try:
                    if await page.get_by_text("Please complete the captcha to continue").is_visible(timeout=200):
                        log("  ⚠️ 验证码未通过"); captcha_err = True; break
                except Exception:
                    pass
                try:
                    nt = page.get_by_text("You can't renew your server yet")
                    if await nt.is_visible(timeout=200):
                        txt = await nt.inner_text()
                        dm = re.search(r'as of\s+(.*?)\s*\(', txt)
                        nd = dm.group(1) if dm else "未知"
                        log(f"  ⏳ 未到时间: {nd}")
                        shot = str(SCREENSHOT_DIR / f"{safe}_skip.png")
                        await page.screenshot(path=shot, full_page=True)
                        send_tg(f"⏳ *KataBump 暂无法续期*\n用户: `{username}`\n下次: {nd}", shot)
                        done = True; break
                except Exception:
                    pass
                await page.wait_for_timeout(200)

            if done:
                return True
            if captcha_err:
                await page.reload(); await page.wait_for_timeout(3000); continue

            # 成功判断
            await page.wait_for_timeout(2000)
            try:
                if not await modal.is_visible():
                    log("  ✅ 续期成功！")
                    shot = str(SCREENSHOT_DIR / f"{safe}_success.png")
                    await page.screenshot(path=shot, full_page=True)
                    send_tg(f"✅ *KataBump 续期成功*\n用户: `{username}`", shot)
                    return True
            except Exception:
                pass

            log("  模态框仍在，刷新重试...")
            await page.reload(); await page.wait_for_timeout(3000)

        log("  ❌ 续期失败（超过重试次数）")
        shot = str(SCREENSHOT_DIR / f"{safe}_fail.png")
        await page.screenshot(path=shot, full_page=True)
        send_tg(f"❌ *KataBump 续期失败*\n用户: `{username}`\n原因: 超过最大重试次数", shot)
        return False

    except Exception as e:
        log(f"  ❌ 异常: {e}")
        try:
            await page.screenshot(path=str(SCREENSHOT_DIR / f"{safe}_error.png"), full_page=True)
        except Exception:
            pass
        return False


async def main():
    import asyncio
    from playwright.async_api import async_playwright

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    users = get_users()
    if not users:
        log("❌ 无账号，退出"); sys.exit(1)

    log(f"🚀 KataBump 自动续期！共 {len(users)} 个账号")
    log("=" * 50)

    proxy = setup_proxy()
    launch_args = {
        "headless": False,
        "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                 "--disable-gpu", "--window-size=1280,720"]
    }
    if proxy:
        launch_args["proxy"] = proxy

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_args)
        context = await browser.new_context()
        await context.add_init_script(TURNSTILE_INJECT)
        page = await context.new_page()
        page.set_default_timeout(60000)

        ok, fail = 0, 0
        for user in users:
            if await renew_user(page, user):
                ok += 1
            else:
                fail += 1
            # 截图
            safe = re.sub(r'[^a-z0-9]', '_', user.get("username",""), flags=re.I)
            try:
                await page.screenshot(path=str(SCREENSHOT_DIR / f"{safe}.png"), full_page=True)
            except Exception:
                pass

        log(f"\n{'='*50}")
        log(f"📊 结果: ✅ {ok} 成功 | ❌ {fail} 失败")
        await browser.close()

    summary = f"KataBump 自动续期报告\n{'='*20}\n成功: {ok}\n失败: {fail}\n共: {len(users)} 个账号"
    send_tg(summary)
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
