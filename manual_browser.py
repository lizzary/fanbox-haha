"""Human-only login in a separate browser profile, before Playwright is attached."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys

from fanbox_lab import LabError, close_browser_resource


def executable_for(channel, playwright):
    if channel == "chromium":
        path = Path(playwright.chromium.executable_path)
        return path if path.is_file() else None
    relative = {"chrome": "Google/Chrome/Application/chrome.exe", "msedge": "Microsoft/Edge/Application/msedge.exe"}[channel]
    if sys.platform == "win32":
        candidates = [Path(root) / relative for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
                      if (root := os.environ.get(key))]
    elif sys.platform == "darwin":
        app, binary = ("Google Chrome", "Google Chrome") if channel == "chrome" else ("Microsoft Edge", "Microsoft Edge")
        candidates = [Path(f"/Applications/{app}.app/Contents/MacOS/{binary}")]
    else:
        binary = shutil.which("google-chrome" if channel == "chrome" else "microsoft-edge")
        candidates = [Path(binary)] if binary else []
    return next((path for path in candidates if path.is_file()), None)


def launch_arguments(executable, profile, url, proxy=None):
    command = [str(executable), f"--user-data-dir={profile}", "--no-first-run",
               "--no-default-browser-check", "--disable-background-mode", "--new-window"]
    if proxy:
        command.append("--proxy-server=" + proxy)
    command.append(url)
    return command


async def wait_for_manual_browser(command, timeout):
    # No remote-debugging port, automation flags, input injection, or daily profile.
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, shell=False)
    deadline = asyncio.get_running_loop().time() + timeout
    while process.poll() is None:
        if asyncio.get_running_loop().time() >= deadline:
            # The browser belongs to the user; leave it open on timeout/cancellation.
            raise LabError("等待普通浏览器关闭超时。窗口仍可手动使用；登录成功后关闭该实验窗口，"
                           "再运行 login 完成凭证验证。", 3)
        await asyncio.sleep(0.5)
    if process.returncode != 0:
        raise LabError(f"普通登录浏览器异常退出（退出码 {process.returncode}）。请重新运行 login。", 3)


async def login_with_browser(experiment, playwright):
    """Shared Fanbox/X login: human browser closes before an automated verification."""
    for channel in experiment.channels():
        executable = executable_for(channel, playwright)
        if executable:
            break
    else:
        raise LabError("没有找到 Chrome/Edge/Chromium，请先安装浏览器或指定 --browser。")
    experiment.channel = channel
    profile = experiment.data / "manual-profiles" / channel
    profile.mkdir(parents=True, exist_ok=True)
    experiment.report.update({"login_opened": True, "login_mode": "manual", "browser": channel})
    print(f"[普通浏览器登录] {experiment.platform_label} / {channel}；此阶段没有 Playwright 连接。")
    print(f"请自行完成 {experiment.platform_label} 登录并进入首页，然后关闭这个实验浏览器的所有窗口。")
    print("关闭后程序才会接管这个专用配置，验证并保存凭证；不读取日常浏览器配置。")
    await wait_for_manual_browser(
        launch_arguments(executable, profile, experiment.login_url, experiment.args.proxy),
        experiment.args.login_timeout,
    )
    headless = experiment.args.headless
    experiment.report["credential_capture_headless"] = headless
    if headless:
        print("[后台验证] 普通登录窗口已关闭，正在无头验证并导出凭证，不再打开验证窗口。")
    else:
        print("[验证登录] 普通登录窗口已关闭，正在显示验证窗口（调试模式）。")
    options = {"headless": headless, "chromium_sandbox": True, "viewport": {"width": 1280, "height": 900}}
    if channel != "chromium":
        options["channel"] = channel
    if experiment.args.proxy:
        options["proxy"] = {"server": experiment.args.proxy}
    context = await playwright.chromium.launch_persistent_context(profile, **options)
    try:
        await experiment.prepare_context(context)
        page = context.pages[0] if context.pages else await context.new_page()
        await experiment.verify(page)
        await experiment.save_state(context)
        experiment.report["login_captured"] = True
        print(f"[保存] {experiment.auth}")
    finally:
        await close_browser_resource(context)
