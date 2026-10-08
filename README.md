# GitHub-OpenList-Sync

*Powered by AI*

**自动同步GitHub仓库到OpenList网盘**

*虽然这很神经，但真的很好玩*

## 用法

```bash
git clone https://github.com/TGBUG/GitHub-OpenList-Sync
cd GitHub-OpenList-Sync
cp example_config.yaml config.yaml
```

然后修改config.yaml

**启动**

```bash
python main.py
```

**若要单次运行，不启动管理面板**

```bash
python main.py --once
```

## 数据安全（mirror_delete）

`mirror_delete` 只依据**正向的存在性证据**执行删除：一次完整、成功（HTTP 200）的
GitHub 列表里确实没有这一项。以下情况一律**不删除任何东西**：

- GitHub 返回 401 / 403 / 404 / 5xx，或网络异常、重试耗尽（含 token 失效）；
- 仓库列表分页中途失败（不会拿"前几页"当完整列表）；
- 文件树被 GitHub 截断（`truncated: true`）；
- 失败只发生在单个仓库时：该仓库本轮跳过删除，其他仓库照常同步；
- 本地过滤（黑名单/白名单）、`sync_private_repos` 开关、fork 跳过
  ——这些仓库在 GitHub 上仍然存在，不会因为"不参与同步"而被删除。

额外的兜底是 **delete guard**：单次删除量达到 `delete_guard_min_count`（默认 5）
且超过已知条目的 `delete_guard_ratio`（默认 0.5）时，本次删除全部取消并报错。
确认 GitHub 侧无误后重跑即可；把 `sync.delete_guard_ratio` 设为 `1.0` 可关闭护栏。

故障会同时出现在日志、面板顶部横幅和 `--once` 的退出码（有错误时为 1）里，
不会静默发生。

设计与取舍见 [docs/adr/0001-fail-closed-mirror-delete.md](docs/adr/0001-fail-closed-mirror-delete.md)。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
