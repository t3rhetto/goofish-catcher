# 版本与镜像发布

仓库：[https://github.com/t3rhetto/goofish-catcher](https://github.com/t3rhetto/goofish-catcher)。两个版本通过 `v1.3.0`、`v1.4.0` 标签保存，每个标签根目录包含该版本完整应用、测试和 Docker 文件；默认分支以 v1.4.0 为当前版本。

## 发布前验证

在待发布版本的源码目录执行：

```sh
docker compose build
docker run --rm --network none --entrypoint python \
  xianyu-watch:1.3.0 -m unittest discover -s tests -v
docker run --rm --network none --entrypoint python \
  xianyu-watch:1.3.0 -c 'from app.main import health; assert health()["version"] == "1.3.0"'
```

测试使用临时数据，不挂载生产 `data/`。v1.3.0 有 38 个测试方法；v1.4.0 有 44 个，其中新增主图缓存和离线浏览器界面回归。两个 `smoke_*.py` 脚本访问真实闲鱼页面，默认离线测试不会运行它们；需要单独准备浏览器显示环境和网络。

提交前检查忽略规则生效，确保暂存内容只包含源码、测试和部署文档。不要提交 `data/`、浏览器会话、真实 API 密钥、数据库或备份。当前未指定许可证；许可证由项目所有者确定后再添加。

## 标签与 GitHub Release

标签必须指向相应版本的代码，且 `/health` 返回版本与标签一致。以本版本为例：

```sh
git tag -a v1.3.0 -m "闲鱼蹲蹲 v1.3.0"
git push origin v1.3.0
gh release create v1.3.0 --title "闲鱼蹲蹲 v1.3.0" --notes-file CHANGELOG.md
```

上述示例使用 `CHANGELOG.md` 作为版本说明，也可在 GitHub 的 Releases 页面填写该版本的专属说明并发布。标签或 Release 已存在时，先核对现有内容，避免重复创建或覆盖。补发旧版时保留 v1.4.0 为最新正式版。

## 自动构建与附件

`.github/workflows/publish.yml` 由正式 `vX.Y.Z` Release 的发布事件触发，也支持手动指定已有 Release 标签：

1. 检出对应标签，从仓库根目录构建 Linux AMD64 镜像。
2. 在禁网容器中运行完整自动化测试，并核对应用版本。
3. 使用仓库的 `GITHUB_TOKEN` 推送 `ghcr.io/t3rhetto/goofish-catcher:X.Y.Z`。
4. 仅当该 Release 是 GitHub 的最新正式版时，同时更新 `latest`。
5. 为 Release 附加源码 ZIP、已填入该版镜像地址的 `compose.yaml`、`Docker-pull.txt` 和 `SHA256SUMS.txt`。

工作流需要仓库的 `contents: write`、`packages: write` 权限。第一次推送后，应检查 GHCR 软件包可见性；仓库公开不代表容器包已可匿名拉取。确认工作流成功、版本标签存在且镜像可拉取后，再把 README 或发布说明中的镜像状态改为“可用”。

手动补发示例：

```sh
gh workflow run publish.yml --ref main -f release_tag=v1.3.0
```

GitHub Actions 页面可查看运行结果。发布失败时先处理失败步骤，再重跑；镜像和附件的可用状态以实际成功结果为准。

## 发布后核验

```sh
docker pull ghcr.io/t3rhetto/goofish-catcher:1.3.0
```

在独立安装目录下载该 Release 的 `compose.yaml`，执行 `docker compose up -d`，检查健康状态、首次密码设置和登录界面。对旧版本补发后，应确认 `latest` 仍指向最新正式版。公开说明应区分离线测试通过与真实闲鱼扫码、搜索验证结果。
