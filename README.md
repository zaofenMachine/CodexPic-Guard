# CodexPic Guard

为 Codex 长时间看图任务控制上传体积的本地工具。采用 **HTTP 请求代理 + MCP 原图恢复工具**，无需修改 Codex 可执行文件。

项目维护仓库：[zaofenMachine/CodexPic-Guard](https://github.com/zaofenMachine/CodexPic-Guard)。当前版本为 **0.1.1，实验阶段**。

2026-11-14 的继续研究确认：当前 HTTP Guard 的未缓存输入可能明显增加；保留原生 WebSocket 并只压缩新图的原型更有希望。详见 [实验结果与限制](docs/RESEARCH-2026-11-14.md)。默认版尚未采用该原型，不应把上传量减少理解为费用减少。

原图按 SHA-256 保存在本机，出站请求中的预览图有单图预算；历史图片累计超预算时移出最旧图，留下可取回的编号。原会话和项目素材不改写。

本机离线 MV 回放：zstd 请求体 **144.06 MB → 6.60 MB（减少 95.4%）**。真实服务上的单张合成图验证也通过。详细边界见 [验证记录](VALIDATION.md)。

## 本机快速启用

需要 Windows、Python 3.11+。源码目录应保留在原位置；启用期间不要移动它。

```powershell
git clone https://github.com/zaofenMachine/CodexPic-Guard.git
cd CodexPic-Guard
.\Setup-CodexPic.ps1
.\Enable-CodexPic.ps1
```

然后在方便中断现有任务时，完整退出并重新启动 Codex。脚本不自动关闭应用。当前构建使用独立环境测试；交付时是否已启用以 `Status-CodexPic.ps1` 为准。

```powershell
.\Status-CodexPic.ps1
```

Setup 只安装本目录虚拟环境、生成 `%LOCALAPPDATA%\CodexPicGuard\guard.json`。Enable 才会启动隐藏的本地服务，备份并调整用户级 Codex 路由、增加 MCP 服务及必要的本地地址代理绕过。支持当前选中的单个 Responses provider；切换到其他 provider 后应检查状态、重新准备配置。

若需要指定梯子端口：

```powershell
.\Setup-CodexPic.ps1 -Proxy http://127.0.0.1:7897
```

默认上游适用于 ChatGPT 登录。使用内置 OpenAI provider 的 API Key 模式时，准备阶段显式指定 `-Upstream https://api.openai.com/v1`。自定义 provider 会保留配置中的原 `base_url`。外部 profile 文件不会被自动改写。

没有检测到上游代理时，准备操作会停止。需要直接联网时必须在 CLI 显式选择 `prepare --direct`；代理连接失败不会回退到 DIRECT。不会改系统代理、Clash/Mihomo、TUN、WFP 或 ClaudeSafe。

## 默认行为

| 项目 | 默认值 |
|---|---:|
| 单图预览目标 | 512,000 原始字节 |
| 预览长边 | 最多 2,000 像素 |
| 请求内图片 Base64 高水位 | 12 MiB |
| 超限后回落目标 | 8 MiB |
| 请求内图片数量 | 最多 24 |
| 优先保留最近图片 | 4 项，并保护最新整批图片 |
| 单请求解压后上限 | 256 MiB |
| 并行请求处理 | 2 |
| 本地原图和预览缓存预算 | 8 GiB |
| 监听地址 | `127.0.0.1:17993` |

所有大小预算都可以在 `guard.json` 的 `policy` 中调整；配置变更需要显式重启本工具，并重新加载 Codex 的 MCP（完整重启 Codex 最稳妥）。MCP 自动启动逻辑会拒绝复用监听同一端口但配置不同的服务。

图片处理顺序：

1. 仅识别 `input` 中消息及工具结果的结构化图片块，不替换普通文本、代码、工具参数或任意 Base64 字符串。
2. 缓存原图；优先确定需要保留的图片，再按需生成预览，避免压缩马上就要移出的几百张旧图。
3. 普通图可缩小并转成 JPEG；透明图保持透明通道，使用 PNG/无损 WebP，必要时降低尺寸。动图不静默转成单帧。
4. 同一请求中的相同原始图片保留最后一次，之前的位置变为引用。
5. 超过字节或数量预算时，移出旧图。最新一批、最近图片、固定参考图以及尚未成功送达的新图片优先保留。
6. 必须保留的图片仍超限时返回明确错误；不会偷偷删除新输入，也不会绕过预算直传整个旧请求。

首次接管一个已有聊天时无法知道每张图此前是否已被模型看过，因此将历史作为初始集合、保护最新图片组。此后工具会跟踪新图片及重试状态。

## 模型怎样取回图片

移出的图片位置会出现 `CPIMG-...` 与 `scope=...`。请求中还会带一个简短的缓存范围提示，帮助压缩后的对话找到本地图片。Codex 可使用：

- `codexpic_list_images`：按 scope 列出图片编号和尺寸。
- `codexpic_get_image`：取回预览；`original=true` 返回完整原图，并为随后的请求提供临时原图额度。一次成功响应完成后取消原图特例。
- `codexpic_pin_image`：固定/取消固定参考图。固定图仍遵循预览压缩；它保护请求中已有的该图片，不会自动把已被 Codex 自身压缩掉的图片重新加入。此时先用 get_image 取回。
- `codexpic_status`：读取编码请求体计数。

做人物接缝、透明边缘、细小文字等验收时，应要求模型按需取回原图。预览优化不代表视觉信息无损。固定过多图片会触发明确的预算错误。

这里的“原图”是代理收到、尚未被本工具压缩的字节。若 Codex 在交给代理之前已经缩小了素材，本工具无法还原从未收到的更高分辨率版本；仍应保留项目里的原始素材。原图返回会附带 Codex 的 original detail 元数据。

本地缓存只允许按合法图片编号和对应 scope 访问；读取原图和预览都会做哈希检查。缓存满时明确报错，不自动删除原图或会话；用户可停用后选择清理自己的缓存。

## 回退

```powershell
.\Disable-CodexPic.ps1
# 完整重启 Codex 后：
.\Stop-CodexPic.ps1
```

Disable 只恢复本工具管理的配置值；用户后来添加的其他配置保留。若管理值已被外部程序更改，会报告冲突而不是覆盖。备份和管理状态在 `%LOCALAPPDATA%\CodexPicGuard`。

Disable 暂不终止服务，因为旧的运行中聊天可能仍连接它。重新启动 Codex 后再 Stop。删除本地缓存前应确认不再需要其中的历史原图；项目素材和原 Codex 会话始终保留。

## 验证范围与限制

- 当前实测接入：Codex CLI `0.166.0-alpha.5.1`，隔离 CODEX_HOME，真实客户端 + 真实 MCP + 本地模拟 Responses 服务。
- HTTP 的身份、gzip、zstd、deflate 请求体解码及预算处理有测试；SSE 流式转发、失败不走 DIRECT、重定向不自动跟随有测试。
- 0.1.1 对未压缩 SSE 以成功完成事件结算图片送达，兼容 Codex 在 HTTP 流完全结束前关闭连接的行为。压缩的响应仍使用保守的完整传输判断；这与请求体压缩支持是两回事。
- 工具为了可靠改写完整历史，会对 Responses WebSocket 升级返回 426，让 Codex 回退 HTTP。这会改变连接方式，不能承诺更低延迟或更高缓存命中率。
- 非空 `previous_response_id` 的增量 HTTP 请求会被拒绝；缺少完整历史时无法可靠执行旧图预算。不会偷偷删掉该字段来伪装成功。
- 模型请求的上游重定向会明确拒绝，避免客户端带着未改写的原请求绕过代理；其他转发也不会自动跟随重定向。
- 预算适用于结构化内嵌图片，不涵盖文字中的 Base64、远端图片 URL、文件附件、下载流量或其他工具自己访问网络的流量。
- 非图片请求内容和未知字段保留。已经使用过图片缓存的 scope 会附加缓存提示；这可能影响提示词缓存。省带宽不等于省额度。
- 当前保留集根据每次完整请求重新计算；历史总量超过预算后，新图片可能持续改变更早的图片位置。这可能降低提示词缓存命中率，尚未证明能降低未缓存输入或总费用。具体待办见 [研究与维护计划](docs/ROADMAP.md)。
- 代理按原入口保留请求编码；上游认证头只在转发时使用，不读取账号凭据文件，不保存请求正文或认证头。日志/SQLite 指标只有大小、数量、scope 哈希与状态码。
- 自定义 provider 需要原生 Responses 接口。本工具不转换 OpenAI/Anthropic API，也不处理其他代理或模型切换器的全部动态重配置。
- 已用真实服务验证单张合成图可正常识别；未完成长 MV 的艺术质量验收，未测机场实际计费。离线回放和基础协议测试不能替代完整工作流验收。
- MCP 服务被标为启动必需，避免图片被移出后恢复工具不可用；启动失败会明确阻断新会话。停用脚本会移除该设置。

## 本地测试与离线回放

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m tests.native_codex_smoke --codex '<codex.exe完整路径>' --out '.test-data\native-check'
.venv\Scripts\python.exe scripts\replay_rollout.py --rollout '<会话.jsonl或.jsonl.zst>' --cache '.test-data\replay-cache' --out 'reports\replay.json'
```

离线回放不会发请求，但会在指定缓存目录写入原图与预览。不要将 `.test-data`、本地缓存、备份或真实会话提交到仓库。发布包不包含这些数据。

仓库 CI 在 Windows / Python 3.11、3.13 下运行单元与组件测试、构建源码包，不调用真实模型。开发与打包步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)；公开的历史验证数值见 [docs/validation-summary.json](docs/validation-summary.json)。

实现依据：本次对本机 Claude Code 图片字节预算的只读调查；[OpenAI MCP 配置](https://learn.chatgpt.com/docs/extend/mcp)和[图像工具结果格式](https://developers.openai.com/api/docs/guides/function-calling)。社区 [Context Attachment Manager](https://github.com/chipfighter/context-attachment-manager) 提供了本地请求代理与图片取回的相邻方案；本实现为独立代码，采用按字节预算保留最近图的策略，没有复制其源码或 Claude 运行时代码。
