# PostPock Playwright 实验台：Fanbox / X

验证 PostPock 的交互流程：**检查凭证 → 必要时登录 → 关闭登录窗口 → 终端选择作者 → 列出帖子 → 选择一篇并完整下载**。Fanbox 作者来自关注 + 赞助列表；X（原 Twitter）作者来自当前登录账号的关注列表。依赖只有 Python 和 Playwright。

## 运行

本目录已安装依赖，有 Chrome/Edge 即可。在 PowerShell 中执行：

```powershell
cd D:\fanbox-haha
.\.venv\Scripts\python.exe fanbox_lab.py
```

也可用启动脚本自动准备环境：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

两个平台均默认采用普通浏览器两阶段登录：人先完成登录并关闭实验窗口，程序再无头启动浏览器，后台验证并导出凭证，不再弹出第二个验证窗口。已有有效凭证时仍直接在后台复用。

不传平台时保持使用 Fanbox。X 使用以下命令：

```powershell
.\.venv\Scripts\python.exe fanbox_lab.py --platform x
# 或通过启动脚本
powershell -ExecutionPolicy Bypass -File .\start.ps1 --platform x
```

### X 的使用方式

- 先后台检查 **X 自己的凭证**；缺失或过期时打开一个普通 Chrome/Edge 专用窗口。这个阶段没有 Playwright 连接，不带自动化启动标记，也不读取日常浏览器配置。
- 在这个实验窗口中手动登录 X 并进入首页，随后**手动关闭这个窗口**。程序才会使用同一个实验配置无头启动浏览器，确认登录成功后导出凭证并自动结束验证进程。
- 终端展示当前账号关注的作者，按序号选择；不读取其他账号的关注列表来代替自己的列表。
- 选择作者后先读取帖子页，再读取该页面实际提供的回复、转帖标签，逐页合并去重。兼容当前的 `UserOriginalsTimeline` / `UserRepliesTimeline` / `UserRepostsTimeline` 和旧接口，去除广告及会话中的其他作者上下文。
- 选择帖子后保存文字、可取得的长帖全文、原图，以及每个原生视频/动图中最高码率的可用 MP4。可取得的引用帖文字和媒体也会一并归档。
- 保存原始响应、JSON、媒体文件和离线 `index.html`，页面内可播放已保存的 MP4。

X 接入观察网页自身发出的只读请求，并通过滚动让网页继续分页；不写死 GraphQL query ID，也不要求开发者 API 密钥。分页等待超时、429 限流或未知响应都会停止并保留已取得的数据，不会将“没有新数据出现”直接当成“全部读取完成”。

**X 的网页时间线可能限制可返回的历史范围。** `all_posts_loaded: true` / `timeline_exhausted: true` 仅表示读到了本次网页返回的时间线末尾，不表示已获得该账号的全部历史、已删除帖子或当前账号无权访问的内容。

```powershell
# 仅检查 X 凭证；不会打开登录窗口
.\.venv\Scripts\python.exe fanbox_lab.py check --platform x

# 手动登录 X
.\.venv\Scripts\python.exe fanbox_lab.py login --platform x

# 调试旧的受控浏览器登录方式（默认已改为 manual）
.\.venv\Scripts\python.exe fanbox_lab.py login --platform x --login-mode playwright

# 当前账号关注的作者
.\.venv\Scripts\python.exe fanbox_lab.py authors --platform x

# 读取指定作者的网页时间线（将 example 换成实际用户名）
.\.venv\Scripts\python.exe fanbox_lab.py list --platform x --creator example

# 按 X/twitter.com 帖子链接或数字 ID 归档
.\.venv\Scripts\python.exe fanbox_lab.py fetch 'https://x.com/example/status/123456789' --platform x

# 如需代理，填写你自己的本地代理地址；下面的端口仅是示例
.\.venv\Scripts\python.exe fanbox_lab.py --platform x --proxy http://127.0.0.1:7890

# 显示读取页面，便于排查 X 的网络或页面验证问题
.\.venv\Scripts\python.exe fanbox_lab.py --platform x --show-browser
```

代理只作用于本次浏览器，不修改系统设置；需要代理时每次执行都带上 `--proxy`。支持 HTTP、HTTPS、SOCKS5 服务地址。`--max-pages 10` 可限制 X 每个时间线的读取页数，任一时间线达到上限会停止并明确报告未读完；默认 0 为不设上限。X 使用登录后的原生网页请求，不支持 `--anonymous` 或 `--transport request`。

两个平台默认使用 `--login-mode manual` 两阶段登录：先让人使用普通浏览器，再在窗口关闭后接管已登录的专用配置。登录期间不使用远程调试或隐藏自动化标记；凭证通过后续 Playwright `storage_state` 导出，不读取或解密日常 Chrome 的 Cookie 数据库。若等待关闭超时或你取消了程序，普通窗口会保留；完成登录并关闭它后重新运行 login 即可。旧的 `--x-login-mode` 保留为 `--login-mode` 的兼容别名。

Google 第三方登录可能拒绝自动化控制的浏览器，旧的 `--login-mode playwright` 可能遇到此问题。若受控页面提示“已临时限制登录”，程序记录 `login_blocked: "temporary_login_limit"`，以退出码 5 停止本次尝试，不反复提交。两阶段模式的普通登录窗口由你操作和判断页面提示；验证期间仍会检查平台限制。Chromium 启动时启用浏览器沙箱，不使用 `--no-sandbox`。

HLS-only 视频、尚未取得的长帖全文、不可用引用帖、X 长文章等会写入 `external-embeds.json` 并标记归档不完整，不会伪装成已完整下载。归档不包含整条评论串，也不递归下载普通外部链接。

### Fanbox 的使用方式

1. 在后台读取已有 `storage-state.json`，请求新的 Fanbox 页面，确认当前账号仍然登录。
2. 凭证缺失、损坏或过期时打开普通 Chrome/Edge 专用窗口。请自己完成 Pixiv 登录及验证码，回到 Fanbox 首页后**手动关闭该实验窗口**。程序随后在后台无头验证、导出凭证并自动结束验证进程。
3. 在终端显示当前账号的**关注作者 + 正在赞助的作者**，按作者 ID 去重，标记“关注”“赞助”或“关注 + 赞助”，输入序号选择。只赞助但未关注的作者同样可以选择。
4. 按所选作者的分页索引读取**所有帖子**，显示进度；全部读完后列出序号、ID、日期、权限、价格、标题。
5. 输入帖子序号，下载正文、原图、附件及封面，生成可离线打开的 `index.html`。
6. 下载结束后后台浏览器自动退出，终端显示保存目录。选择时输入 `q` 可退出。

凭证有效时不会出现登录窗口。为了继续使用已经实测有效的浏览器网络通路，凭证检查和数据读取仍由**无可见窗口的后台 Chromium**完成。网络错误不等于凭证失效：网络/站点验证错误会明确报错，不反复要求重新登录。

默认最长等待你关闭普通登录窗口 10 分钟，可用 `--login-timeout 900` 延长。关闭窗口前请确认已回到 Fanbox 且登录成功；验证失败不会覆盖原有凭证。需要对照旧的受控登录方式时，可使用 `--login-mode playwright`。

## 整篇下载的范围

- 保存原始帖子 JSON、标准化信息、正文文本，以及原始内容块和链接。
- 下载图片原图、附件（ZIP/PSD 等）和帖子封面到 `media/`。
- 通过浏览器网络流式写入文件，每次传输最多 1 MiB 数据，避免将整个大附件读入 Python 内存。
- 每个文件记录实际字节数、SHA-256、内容类型及本地路径；完成后才把临时文件改为正式文件。
- `index.html` 按 article 内容块顺序排列文字、图片和附件，可直接离线打开。
- 任何资源下载失败，都保留已经成功的文件，记录失败原因并以退出码 4 结束；不会显示“整篇保存完成”。

外部嵌入视频、第三方 iframe 和尚未解析的旧 HTML 内容会保留原始信息到 `external-embeds.json`，**不保证离线下载**，并将 `archive_complete` 标记为 false、退出码设为 4。普通正文中的外部超链接按链接保存，不递归下载链接目标。此实验的“整篇”指投稿内容及其直接资源，不包含评论区。

列表包含受限帖子。交互模式选中列表标记为受限的帖子时会提示重新选择；若获取正文时才发现无权限，保存元数据并返回退出码 2。

## 分步命令

下面使用本项目虚拟环境中的 Python：

```powershell
# 仅检查保存的凭证，失败时不打开登录窗口
.\.venv\Scripts\python.exe fanbox_lab.py check

# 主动用普通浏览器重新登录，关闭后验证并保存凭证
.\.venv\Scripts\python.exe fanbox_lab.py login

# 查看关注 + 赞助作者的合并列表
.\.venv\Scripts\python.exe fanbox_lab.py authors

# 列出某作者的全部帖子
.\.venv\Scripts\python.exe fanbox_lab.py list --creator 'https://www.fanbox.cc/@szcb911'

# 按链接直接下载整篇；不展示选择菜单
.\.venv\Scripts\python.exe fanbox_lab.py fetch 'https://www.fanbox.cc/@szcb911/posts/12758879'

# 比较 API 请求方式；文件下载仍使用浏览器流
.\.venv\Scripts\python.exe fanbox_lab.py fetch 12301460 --transport request

# 匿名测试，不读写已保存的账号凭证
.\.venv\Scripts\python.exe fanbox_lab.py fetch 12301460 --anonymous

# 调试：显示后台数据获取窗口
.\.venv\Scripts\python.exe fanbox_lab.py --show-browser

# 大附件可以提高每个文件的总下载时限
.\.venv\Scripts\python.exe fanbox_lab.py --download-timeout 1800
```

`list` / `fetch` / `authors` 同样先检查凭证，必要时自动登录。`check` 只诊断，不打开登录窗口。旧的 `following` 命令保留为 `authors` 的兼容入口，现在也返回合并列表。`--creator` 用于直接执行 list/fetch；默认交互流程始终从关注和赞助的合并列表中选择作者。

赞助作者来自 `plan.listSupporting` 当前返回的赞助方案；同一个作者的多个方案只计为一位作者。关注列表与赞助列表都读取完成后才展示选择菜单，任何一方失败都会明确报错。

其他选项：`--browser chrome|msedge|chromium`、`--data-dir 路径`、`--timeout 60`、`--login-timeout 900`。旧的 `--headless` 仍可使用，但现在后台模式已是默认值。`--limit` 已移除，帖子列表始终读取全部分页。

## 输出

Fanbox 保持原来的 `data/` 路径；X 的凭证、专用浏览器配置和结果放在 `data/x/`，两者不会共用登录状态。普通登录配置分别放在 `data/manual-profiles/<browser>/` 和 `data/x/manual-profiles/<browser>/`。设置 `--data-dir` 时，X 仍使用指定目录下独立的 `x/` 子目录。

```text
data/
  auth/storage-state.json       # 实验凭证
  auth/browser.json             # 保存使用的浏览器类型
  manual-profiles/<browser>/    # 默认普通浏览器登录及后续验证使用的专用配置
  profiles/<browser>/           # 旧的 --login-mode playwright 配置
  runs/<时间>/
    report.json                 # 凭证检查、分页、下载状态和错误
    authors.json                # 按 creatorId 去重后的关注 + 赞助作者，含 sources 标记
    following.json              # 完整关注作者列表
    following-pages/*.json      # 关注列表原始响应
    supporting.json             # 从赞助方案中提取并去重的作者
    supporting-pages/*.json     # 赞助方案原始响应
    post-pages.json             # 全部帖子分页索引
    list-pages/*.json            # 各页原始响应
    list.raw.json               # 去重后合并的全部帖子
    post.raw.json               # 选中帖子的原始响应
    post.json                   # 标准化信息和 archive_complete
    text.txt                    # 正文文本
    assets.json                 # 每个资源的下载状态、路径、大小和 SHA-256
    external-embeds.json         # 尚未离线下载的嵌入内容
    index.html                  # 离线阅读入口
    media/                      # 实际下载的图片、封面和附件
```

默认数据位于脚本同目录的 `data/`，不受终端当前目录影响。该目录已被 Git 忽略。登录状态是未加密的本地实验凭证，不要分享或提交；程序不读取日常浏览器配置，也不接收账号密码。自定义数据目录也应自行排除版本控制。

完整成功以 `report.json` / `post.json` 中的 `archive_complete: true` 为准。`author_list_complete: true` 表示关注和赞助两份列表都读取完成；`all_posts_loaded: true` 表示帖子所有分页读取完成；`login_opened: false` 表示这次没有弹出登录窗口。`credential_capture_headless: true` 表示手动关窗后的凭证验证和导出采用无头模式；仅指定 `--show-browser` 调试时会显示验证窗口。

X 的结果结构相同，但只有关注作者数据，没有赞助文件或 Fanbox 的分页索引文件；`following-pages/`、`list-pages/` 保存网页原始 GraphQL 响应。报告包含 `platform: "x"` 和实际读取范围 `post_list_scope`。

退出码：0 为当前命令完成，1 为网络/接口等错误，2 为正文受限或缺失，3 为未登录/登录未完成，4 为资源下载不完整，5 为平台临时限制登录，130 为手动取消。查看列表、检查凭证或在菜单中退出也可能返回 0，不代表下载过帖子。

## 已验证

2026-11-03，已清空两个平台的导出凭据和实验浏览器配置，从未登录状态重新测试：普通浏览器手动登录并关闭后，Fanbox 和 X 均通过无头验证、导出新凭据；报告中 `credential_capture_headless: true`。再次启动全新后台浏览器复用新凭据，Fanbox 取得 6 位关注/赞助作者，X 读完 2 页、61 位关注作者，全程不再弹出第二个验证窗口。

2026-11-02，Fanbox 在本机 Chrome 实测：

- 普通 Chrome 手动登录后关闭窗口，Playwright 接管实验配置并确认登录成功、保存凭证；再次启动全新后台浏览器可正常复用，关注和赞助接口均返回 HTTP 200，合并得到 6 位作者。
- 现有真实账号凭证通过后台检查，无需再次打开登录窗口。
- 合并 5 位关注作者与 1 位赞助作者，得到 6 位作者；选中 szcb911 后读完 16 页，共 157 篇帖子。
- 默认交互流程选择公开帖子 12301460 后，正文、JSON 和离线页面保存成功，`archive_complete: true`。
- 使用同一浏览器下载器成功流式保存真实站点的公开封面图片。
- 同一公开帖子的浏览器 API 请求返回 200，普通 HTTP 客户端请求返回 403；默认继续使用浏览器请求。
- 浏览器测试使用模拟响应和本机服务器，覆盖凭证复用/过期、两次选择、完整分页、分页中断、带 Cookie 和 Referer 的附件下载、长度校验与失败保留。

X 在本机真实站点实测：

- 普通 Chrome 手动登录后，关闭窗口并由 Playwright 接管同一个实验配置，服务端验证成功并导出凭证。
- 全新后台浏览器复用该凭证成功，不再弹出登录窗口；读完 2 页关注列表，共 61 位作者。
- 使用当前 `UserOriginalsTimeline` 读取 szcb911 的 2 页、40 篇帖子。本次特意设置 `--max-pages 2`，报告明确标记未读完，不据此宣称获得全部历史。
- 帖子 `2116398322999271718` 的正文和 2 张原图下载成功，图片大小分别为 518,515 和 398,569 字节，`archive_complete: true`，离线页面已生成。
- 原生视频归档、失败分支和三个时间线的合并由模拟测试覆盖；本轮真实站点样例是图文帖。

自动化检查覆盖账号凭证隔离、手动登录前不连接 Playwright、凭证验证后才保存、窗口关闭错误保留、原生滚动分页、关注选择、帖子选择、图文/视频归档、限流与分页中断。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Fanbox 接口属于站点内部实现，可能变化。目前核对的结构是 `body.creators`、`body.plans`、`body.pageUrls`、`body.posts` 和 `body.post`。原始响应会留在结果目录，便于继续实验。

首次在其他环境安装（Python 3.10+）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 仅没有 Chrome/Edge 或明确选择 Chromium 时需要
.\.venv\Scripts\python.exe -m playwright install chromium
```

参考：[Playwright 认证状态复用](https://playwright.dev/python/docs/auth)、[Chromium 响应流](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/#method-takeResponseBodyAsStream)。
