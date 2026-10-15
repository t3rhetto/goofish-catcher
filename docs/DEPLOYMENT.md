# 部署与维护

本文对应 **v1.4.0**。当前构建与发布目标是 Linux AMD64；Docker 需能运行 AMD64 容器。Apple Silicon 等 ARM 主机如已配置 Docker 的 AMD64 模拟，可在以下命令前设置 `export DOCKER_DEFAULT_PLATFORM=linux/amd64`。

## 使用已发布镜像安装（推荐）

截至 **2026-10-15**，GHCR 软件包已公开，`1.3.0`、`1.4.0` 均已发布，`latest` 对应 `1.4.0`。两版发布工作流的镜像构建、离线测试和版本一致性检查均已通过：[v1.3.0 运行记录](https://github.com/t3rhetto/goofish-catcher/actions/runs/38706825958)、[v1.4.0 运行记录](https://github.com/t3rhetto/goofish-catcher/actions/runs/38706886300)。后续版本以对应 Release 的实际发布结果为准。

新安装时，在一个新的目录下载现成 Compose 文件并启动，无需本地构建：

```sh
mkdir goofish-catcher-deploy
cd goofish-catcher-deploy
curl --fail --location --output compose.yaml \
  https://github.com/t3rhetto/goofish-catcher/releases/download/v1.4.0/compose.yaml
docker compose up -d
docker compose ps
```

该文件已填入 `ghcr.io/t3rhetto/goofish-catcher:1.4.0`，使用 Docker 命名卷 `xianyu-data` 持久保存数据。**已有实例请继续使用原 Compose 文件及数据映射，不要直接用新安装文件覆盖**；保留安装目录和 Compose 项目名，避免连接到另一个空卷。

| 版本 | 完整源码 | 已填镜像地址的部署文件 |
| --- | --- | --- |
| [v1.4.0（最新）](https://github.com/t3rhetto/goofish-catcher/releases/tag/v1.4.0) | [goofish-catcher-source-1.4.0.zip](https://github.com/t3rhetto/goofish-catcher/releases/download/v1.4.0/goofish-catcher-source-1.4.0.zip) | [compose.yaml](https://github.com/t3rhetto/goofish-catcher/releases/download/v1.4.0/compose.yaml) |
| [v1.3.0](https://github.com/t3rhetto/goofish-catcher/releases/tag/v1.3.0) | [goofish-catcher-source-1.3.0.zip](https://github.com/t3rhetto/goofish-catcher/releases/download/v1.3.0/goofish-catcher-source-1.3.0.zip) | [compose.yaml](https://github.com/t3rhetto/goofish-catcher/releases/download/v1.3.0/compose.yaml) |

## 从源码安装

```sh
git clone --branch v1.4.0 https://github.com/t3rhetto/goofish-catcher.git
cd goofish-catcher
docker compose build
mkdir -p data
docker run --rm --user root --entrypoint sh \
  -v "$PWD/data:/data" xianyu-watch:1.4.0 \
  -c 'chown pwuser:pwuser /data'
docker compose up -d --build
docker compose ps
```

应用以镜像中的 `pwuser` 用户运行。新安装时先为宿主机 `data/` 目录设置归属，使用用户名解析实际 UID/GID，避免假定固定数字。上面的命令只调整目录本身；如果迁入旧数据，应核对其文件归属后再启动。

## 访问与首次设置

默认仅监听服务器 `127.0.0.1:8818`，容器内端口为 `8080`。本机部署直接打开 [http://127.0.0.1:8818](http://127.0.0.1:8818)；远程部署在自己的电脑运行：

```sh
ssh -N -L 18818:127.0.0.1:8818 用户名@服务器地址
```

保持转发连接，在本机打开 [http://127.0.0.1:18818](http://127.0.0.1:18818)。依次设置管理密码、扫码登录闲鱼、创建任务；使用 AI 模式时再配置兼容接口和模型。会话到期或页面要求人工验证时，按界面提示处理。

## 配置与持久数据

| 项目 | 源码 Compose | Release Compose |
| --- | --- | --- |
| 镜像 | `xianyu-watch:1.4.0`（本地构建） | `ghcr.io/t3rhetto/goofish-catcher:1.4.0` |
| 数据映射 | `./data:/data` | 命名卷 `xianyu-data:/data` |
| `DATA_DIR` | `/data` | `/data` |
| `TZ` | `Asia/Shanghai` | `Asia/Shanghai` |
| 端口映射 | `127.0.0.1:8818:8080` | `127.0.0.1:8818:8080` |
| 共享内存 | `256mb` | `256mb` |
| 内存 / 内存加交换空间限制 | `900m` / `1400m` | `900m` / `1400m` |
| CPU 上限 | `1.5` | `1.5` |
| 日志轮转 | 每文件 `10m`，保留 `3` 个文件 | 每文件 `10m`，保留 `3` 个文件 |

容器 `/data` 中的 `watch.db` 保存管理设置、任务、商品、收藏和日志；`browser/`、`storage_state.json` 保存闲鱼浏览器登录资料；`ai-config.json` 保存 AI 接口和密钥；`item-images/` 保存商品详情主图。源码部署对应宿主机 `data/`，镜像安装对应 Docker 命名卷。管理密码与闲鱼账号是两套独立的登录信息。退出闲鱼或清除并换登会清理闲鱼会话，保留任务、管理密码和收藏。

## 日常维护

在 Compose 文件所在目录运行：

```sh
docker compose ps
docker compose logs --tail 100
docker compose restart
docker compose stop
docker compose up -d
```

`docker compose stop` 停止容器并保留数据。源码更新后需重新构建：`docker compose up -d --build`。不要将源码解压进 `data/`。

## 备份与恢复

下例适用于源码 Compose 的 `./data` 绑定目录。先停止应用，取得一致的数据库与浏览器资料副本；备份放在仓库目录外：

```sh
docker compose stop
tar -czf "../goofish-catcher-data-$(date +%Y%m%d-%H%M%S).tar.gz" data
docker compose up -d
```

备份包含密钥、账号会话和个人商品记录，应自行保管，勿上传 GitHub。恢复时先停止应用，在原项目目录解压所选备份，确认 `data/` 文件对镜像内 `pwuser` 可写，再启动。若使用发布模板的命名卷，请另行备份该卷；上述 `tar` 命令不备份 Docker 命名卷。

## 升级现有源码部署

先备份数据，并保留实际使用的 Compose 配置。若已自行修改 `compose.yaml`，切换标签前先保存本地差异，并在升级后继续使用原数据映射。

```sh
git fetch origin --tags
git switch --detach v1.4.0
docker compose up -d --build
docker compose ps
docker compose logs --tail 50
```

不要删除或替换 `data/`。更换版本的源码与部署配置不会自动搬迁数据；降级可能涉及数据库兼容问题，应使用升级前备份恢复对应版本。当前源码目录的版本可通过 `curl --fail http://127.0.0.1:8818/health` 检查。

## 镜像部署的模板与升级

除了下载 Release 附件中的现成文件，也可从源码使用 `compose.release.yaml` 模板新建实例：

```sh
XIANYU_IMAGE=ghcr.io/t3rhetto/goofish-catcher:1.4.0 \
  docker compose -f compose.release.yaml up -d
```

模板指定 `linux/amd64` 并使用命名卷 `xianyu-data`，首次启动是全新实例。**升级已有实例请保留原 Compose 文件及数据映射**，仅将 `image` 换为已确认发布的镜像地址，再执行：

```sh
docker compose pull
docker compose up -d --no-build
```

同一安装目录内继续使用相同的 Compose 项目名，避免命名卷名称变化。`latest` 仅在最新正式版发布成功时更新；运行中的容器不会自动升级。

## 常见问题

- **无法访问**：先检查 `docker compose ps` 和日志。远程浏览器应访问本机转发端口 `18818`；服务默认未绑定服务器公网地址。
- **数据目录权限错误**：按源码安装步骤为目录设置归属；已有数据需同时检查文件权限，勿删除数据重新开始。
- **闲鱼登录失效或验证**：在管理页面查看状态并重新扫码或处理验证；账号操作会暂停或取消当前检查。
- **AI 接口报错**：核对地址、模型、密钥和 JSON 模式；更换接口地址时需重新输入新接口的密钥。
- **镜像拉取失败**：核对对应 Release 的构建结果和 GHCR 包可见性；仍可从源码构建。
