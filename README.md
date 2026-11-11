# FastDeploy

FastRD 的 GitHub Actions 可复用发布流程。项目仓库执行测试、构建和上传；FastDeploy 校验产物，通过 SSH 上传、原子切换版本并重启 systemd。失败时恢复之前的版本和运行配置，再检查恢复结果。

当前完整接入 **FastResearch**。其他项目暂不修改发布入口，需要各自增加构建包约定、服务映射和验收，尤其是 FastRead 的 API/worker/前端及 FastWrite 的系统级服务。

## 项目部署 Action 如何写 env

在项目的 `.github/workflows/tag-ci.yml` 中，将发布 job 接在构建 job 后面：

```yaml
deploy:
  needs: validate
  if: startsWith(github.ref, 'refs/tags/v')
  uses: FastR-D/FastDeploy/.github/workflows/deploy.yml@v1
  with:
    app: fastresearch
    artifact-name: release-${{ github.sha }}
    runtime-env: |
      HOST=127.0.0.1
      PORT=8787
      NODE_ENV=production
      FASTRESEARCH_PUBLIC_URL=https://fastrd.io
      FASTRESEARCH_DATA_DIR=/home/dev/FastResearch/data
  secrets:
    SSH_HOST: ${{ secrets.FASTRD_SSH_HOST }}
    SSH_PORT: ${{ secrets.FASTRD_SSH_PORT }}
    SSH_USER: ${{ secrets.FASTRD_SSH_USER }}
    SSH_PRIVATE_KEY: ${{ secrets.FASTRD_SSH_PRIVATE_KEY }}
    SSH_KNOWN_HOSTS: ${{ secrets.FASTRD_SSH_KNOWN_HOSTS }}
    runtime-secrets: |
      {
        "FASTRESEARCH_JWT_SECRET": ${{ toJSON(secrets.FASTRESEARCH_JWT_SECRET) }}
      }
```

这是最小接口示例；生产使用 FastResearch 仓库中的完整映射，避免丢失 CAS/项目集成配置。生产调用应将 `@v1` 替换为审核过的 FastDeploy 完整提交 SHA。通用 workflow 通过 `job.workflow_repository` 和 `job.workflow_sha` 获取自身实现，保证脚本与 workflow 来自同一提交，不会误取调用方源码。

`runtime-env` 按行解析 `KEY=value`，忽略空行和整行注释，值按字面处理，不执行 shell、变量展开或 dotenv 转义。不要在值两侧添加作为语法的引号。复杂 JSON、含前后空格、多行内容放进 `runtime-secrets`，使用 `toJSON` 序列化。普通配置和密钥不能出现同名键，密钥不能为空。`VITE_*` 只能是公开配置，在调用方构建 job 的 `env` 中设置；仅传运行时不会改变已经构建的网页。

最终配置保存在各版本的 `.fastdeploy/runtime.json`，权限 `0600`。Python 启动器将其注入进程环境，再执行应用。它清除旧 systemd 环境继承，不生成可能改变密码字符的 `.env`，不覆盖原有线上 `.env.production`。应用继续使用原来的绝对数据目录。所有版本目录仅部署用户可访问；配置不进入 Actions artifact 或 cache。

## SSH 与项目密钥

组织级 Secrets，授权给实际调用的项目仓库：

| 名称 | 内容 |
| --- | --- |
| `FASTRD_SSH_HOST` | 服务器地址 |
| `FASTRD_SSH_PORT` | `22` |
| `FASTRD_SSH_USER` | `dev` |
| `FASTRD_SSH_PRIVATE_KEY` | 已授权的 Actions 专用 SSH 私钥 |
| `FASTRD_SSH_KNOWN_HOSTS` | 通过可信渠道核验的 known_hosts 条目 |

强制检查固定的 SSH 主机公钥，不在首次连接时运行 `ssh-keyscan` 信任新密钥。服务器 SSH 别名不会自动出现在 GitHub runner 中，workflow 会生成自己的临时配置。私钥和上传配置仅存在于临时私有目录；正常完成和失败都会清理。runner 强制终止或网络中断可能留下服务器 incoming 目录，恢复后由管理员核对并清理超过一天的孤立目录。

项目密钥保存在调用方仓库 Secrets；FastResearch 的确切清单见其 `runtime-secrets` 映射。可用 `python3 scripts/provision_secrets.py` 检查计划，执行时加 `--apply`。项目配置按服务器现值迁移，直接通过 stdin 交给 `gh secret set`，不输出密钥值、不落盘。默认拒绝覆盖同名已有 Secret；显式更新使用 `--overwrite`。

已有组织私钥默认保留。初次设置或轮换私钥时，必须显式传 `--ssh-key-file /安全目录/部署私钥`，并提前将对应公钥加入服务器的 `authorized_keys`。脚本会核对公钥已授权，不再从服务器读取旧 CI 私钥，避免将已轮换的组织 Secret 改回旧密钥。

### 当前部署密钥

当前使用独立的 Ed25519 部署密钥，公开指纹记录在 `config/deployment-key.json`。私钥只保存在组织 Secret；生成及验证时的本地私钥文件已删除，服务器没有新私钥副本。原 `github-actions-fast-cd` 公钥已从服务器授权中撤销，日常 SSH 公钥保留。

公钥在 `dev` 的 `authorized_keys` 中使用 `restrict`，禁用 PTY、端口/agent/X11 转发及用户 ssh rc，仍允许部署需要的非交互命令。它仍具有 `dev` 的命令权限；`restrict` 不是只能调用发布命令的沙箱。若后续要求命令级隔离，需要另建部署账户及可信的强制命令入口，并适配现有服务权限。

轮换顺序：本地生成新的 Ed25519 密钥 → 仅上传公钥并添加 `restrict` → 使用新私钥及固定 known_hosts 验证登录和限制 → 更新组织 Secret，保留原来的仓库授权范围 → 再次验证后撤销旧 CI 公钥 → 删除本地新私钥。不要覆盖或撤销日常 SSH 公钥。

配置组织 Secrets 的 GitHub CLI 凭据需要 `admin:org`（或等效细粒度权限）。若组织 API 返回 403，在本机执行 `gh auth refresh -h github.com -s admin:org` 并完成 GitHub 授权，然后再执行配置脚本。已有旧名称的 Secrets 不会被删除。

GitHub Free 组织的私有仓库无法使用组织 Secrets；私有调用项目需要同名仓库 Secrets，或升级套餐。公开调用项目无法复用私有 FastDeploy：正式启用前需将只含部署代码的 FastDeploy 发布为公开仓库，或另行设计私有中央发布入口。

## 构建产物约定

调用方必须在**同一 workflow run** 中上传一个包含以下两个文件的 artifact：

- `release.tar.gz`：仅包含 `server/`、`dist/`、生产 `node_modules/`、`package.json`、`package-lock.json`。
- `manifest.json`：`repository`、准确的 `sha`、`archive_sha256`。示例可见 FastResearch 的打包步骤。

构建和部署目标均为 Ubuntu 24.04 x86_64、Node 24。打包时解引用链接与硬链接；服务端拒绝符号链接、硬链接、设备文件、重复路径、越界路径、`.env*`、`.git`、`.ssh` 和 `.fastdeploy`。解包后总大小上限 2 GiB。部署前后两次验证归属、SHA 和校验值，不在服务器重新 npm install/build。系统运行时版本、共享数据目录和必需应用文件均在切换前检查。

## 首次上线顺序

1. 审核并发布 FastDeploy，先建立 `v1`，把调用方改为它的完整提交 SHA；再发布调用方配置。当前源码不会自动改变仓库可见性或创建发布 tag。
2. 设置组织 SSH Secrets、项目 Secrets，以及调用方的 `production` Environment。环境允许 `v*` tag；手动回滚从主分支执行，环境规则还需允许该分支。Environment 功能受 GitHub 套餐和仓库可见性限制。
3. 核对项目 YAML 的普通配置，特别是绝对共享目录、域名和客户端 ID。现有 FastResearch 配置中密钥均以引用表示，缺少任意一个已声明密钥会在 SSH 上传前失败。
4. 当前 `fastrd` 已停用并屏蔽旧发布 timer/service，不需要再执行接管脚本。其他仍运行旧轮询的服务器，可将 `scripts/remote.py` 与 `scripts/migrate_legacy.py` 放在同一临时目录，以部署用户执行 `python3 migrate_legacy.py --app research`，仅让出该项目。它持有旧发布器相同的项目锁，并保存旧脚本备份；等待项目锁最多 120 秒。
5. 确认数据库变更向后兼容；首次接入先用兼容当前数据版本的 tag 验证。创建并推送版本 tag（如 `git tag v2026.11.11.1`，再 `git push origin v2026.11.11.1`）。普通提交信息包含 `v` 不触发部署。版本 tag 不应移动或复用。
6. 验证 Actions 输出、服务健康及公网页面/登录/集成。发布会短暂重启，原子切换不代表零停机。原服务定义和原配置文件保留，以便首版回退。

新流程在旧 timer/service 仍拥有项目时拒绝发布。新旧发布器共用 `/home/dev/ci-releases/locks/research.lock`；GitHub job 同时使用独立 concurrency group，构建取消不会取消已开始的发布。仓库管理员仍应限制可创建生产版本 tag 的人员。

### 当前服务器切换状态

`fast-tag-deploy.timer`、`fast-tag-deploy.service`、`fast-cd.timer`、`fast-cd.service` 均为 `inactive / masked`，原单元保存在 `/home/dev/.local/state/fastdeploy/legacy-retired/20261111T135607Z`。停用前等待了当前轮询结束，没有中断版本切换。业务服务的 systemd 单元继续保留，由 FastDeploy 调用重启；资讯定时任务和证书续期 timer 继续运行。

目前只有 FastResearch 接入了新的发布 workflow。旧轮询已整体停用，因此其他项目在完成自己的 FastDeploy 适配前不会自动发布；现有业务服务继续运行。不要恢复旧发布器作为并行发布入口。

## 发布与回滚记录

FastResearch 使用以下目录：

```text
/home/dev/ci-releases/research/
  current -> fastdeploy-releases/<tag>-<sha12>-<run_id>
  fastdeploy-releases/
  fastdeploy-active.json
  fastdeploy-run-watermark
  fastdeploy-transaction.json    # 仅未完成事务存在
```

成功日志输出准确 release ID。调用方的 `rollback.yml` 提供手动入口，填入当前 ID；若当前版本已变化，回滚会拒绝执行。回滚使用版本中保存的前态，恢复链接、systemd drop-in、环境与原服务；首次发布也可恢复尚未接入 FastDeploy 的旧服务。数据库和业务文件不会自动恢复。架构不兼容的数据库迁移需要项目独立的备份、迁移和恢复流程，不能依赖代码回滚。

重启或就绪检查失败会自动恢复；如果恢复也失败，保留事务记录，并使 Actions 失败。下一次请求在相同项目锁内先恢复未完成事务。旧 run 不能覆盖更新的发布或回滚；同一 run、同一内容可幂等重试。失败版本目录保留诊断，需重新发起 workflow 获得新 run ID；“Re-run jobs” 仍使用旧 run ID，不用于覆盖失败目录。

当前不自动清理版本，避免误删回滚所需文件。需要定期检查磁盘，在核对 `current`、active、transaction 和前态引用后清理历史版本；版本私有配置也随版本保留。

## 本地检查

Python 3.10+，无需第三方 Python 依赖：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
actionlint -ignore 'property "workflow_(repository|sha)" is not defined'
```

测试使用临时目录、模拟 systemd，覆盖配置保真、敏感输入不回显、恶意包、幂等、旧 run 拒绝、重启/健康检查失败恢复、事务恢复、手动回滚、旧轮询接管和启动器环境。不会连接生产或修改真实 systemd。actionlint 1.7.12 尚未识别 GitHub 已支持的 `job.workflow_*` 字段，仅忽略这两项字段错误，其他检查保留。
