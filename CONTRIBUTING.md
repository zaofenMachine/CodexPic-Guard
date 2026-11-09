# 开发与验证

目标环境是 Windows，Python 3.11+。在源码目录创建独立环境；开发测试不需要运行 Setup 或 Enable：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe -m pip install --no-deps -e .
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试只使用合成图片、本地模拟服务和临时配置，不需要账号凭据。真实 Codex 客户端的可选集成测试：

```powershell
.venv\Scripts\python.exe -m tests.native_codex_smoke --codex '<codex.exe完整路径>' --out '.test-data\native-check'
```

该集成测试使用隔离的 CODEX_HOME 和本地模拟 Responses 服务。`scripts/live_smoke.py` 是另一个需要显式选择的真实服务测试；不要把它加入自动检查。

## 源码包

```powershell
.venv\Scripts\python.exe scripts\build_release.py
```

生成 `dist/codexpic-guard-<版本>.zip` 和 `.zip.sha256`。打包脚本只选择源码、测试、入口脚本及公开文档，逐文件生成并核验 SHA-256 清单。包不包含 Python 环境；安装时仍需获取锁定的依赖。

原始验证结果写入被忽略的 `reports/`，本地缓存写入 `.test-data/`。只将审核过的汇总数值放入 `docs/validation-summary.json`，不要上传真实会话、图片或配置快照。

## 提交要求

修改保留策略时验证新图片、重试、固定图、原图取回和原生压缩后的恢复行为；修改网络路径时验证流式响应、超时、重定向和代理故障。涉及费用或质量的结论需要相应实测，不能由单纯的字节缩减或测试通过推出。

版本号位于 `pyproject.toml` 和 `src/codexpic_guard/__init__.py`。发布前同步版本、变更记录和验证边界；打 tag 后确认远端引用对应实际提交。
