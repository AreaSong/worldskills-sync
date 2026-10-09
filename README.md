# WorldSkills Sync

用**你自己的** WorldSkills 会员账号，把能访问的竞赛资料、资源库和公开名单归档到本地，并按届次、技能、类型、语言整理。也可以把已下载的正文打成 zip，放到 GitHub Releases，让其他人不用登录也能取用。

脚本是开源的。试题、技术描述等文件的版权仍属于 [WorldSkills International](https://worldskills.org/)，使用者需自行确认自己的会员权限和转载范围。本仓库不保存密码，也不提交登录令牌。

## 别人怎么用

### 1. 只要现成资料（不需要会员账号）

1. 克隆本仓库，查看 `data/` 名单和 `indexes/` 目录。
2. 打开 [GitHub Releases](https://github.com/AreaSong/worldskills-sync/releases)，按届次下载 zip。若 Releases 还是空的，说明正文尚未发布，请用下面的路径 2 自己下载。
3. 解压到本仓库的 `store/`，路径会与索引里的 `store_path` 一致。

```text
unzip wsc-2026-shanghai-tp.zip -d store
```

### 2. 想自己再下一份（需要你自己的会员账号）

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chrome

python sync.py login          # 用你自己的账号登录会员区
python sync.py download       # 默认 4 路并发下载到 store/
python sync.py progress --open
```

登录后令牌只写在本机 `.session/`，不会进 git。不要把别人的账号写进脚本或提交到仓库。

## 仓库里有什么

| 路径 | 进 git？ | 说明 |
|---|---|---|
| `sync.py` 等脚本 | 是 | 登录、扫描、下载、打包、发布 |
| `data/` | 是 | 届次、成员、技能、成绩、IL 目录等名单 |
| `indexes/` | 是 | 已保存文件的目录，方便查找 |
| `store/` | 否 | 正文（PDF/ZIP）。克隆后从 Releases 解压，或自己下载 |
| `downloads/` | 否 | 本机队列、日志、进度页 |
| `.session/` | 否 | 登录令牌 |
| `.venv/` | 否 | 本地虚拟环境 |

正文不进 git：文件多、体积大。发布时按届次和类型打成 zip，单个 zip 大约超过 1.8GB 会切成 `part01`、`part02`。

## 命令

```bash
python sync.py login                 # 浏览器登录
python sync.py discover              # 只更新队列，不下载
python sync.py download              # 下载队列；默认 4 路并发，已存在的会跳过
python sync.py download --workers 8  # 最多 8 路；逐个下用 --workers 1
python sync.py download --delay 0.5  # 每个线程两次下载之间的间隔
python sync.py download --sample     # 先各下一份试题 / TD / IL
python sync.py download --refresh    # 重新扫描网站后再继续下
python sync.py status                # 终端进度
python sync.py progress --open       # 浏览器进度条和最近日志
python sync.py data                  # 刷新名单表（成绩、成员等）
python sync.py pack                  # 按届次打 zip 到 dist/
python sync.py publish               # 打包并上传 GitHub Releases
python sync.py --self-test
```

本地下载日志：`downloads/download.log`。

## 要求

- Python 3.10+
- 自己下载时需要 WorldSkills 会员，以及本机 Chrome（Playwright 使用系统 Chrome）
- 发布 Releases 需要已登录的 [GitHub CLI](https://cli.github.com/)（`gh`）

## 不收录

人员名册与照片、出生日期、会员通讯录、报名岗位名单等个人敏感信息。竞赛文档里打不开的栏目（例如部分通信页 HTTP 500）无法归档。Flickr 照片和 YouTube 视频只保留链接，不镜像整站。

## 许可

脚本与文档为 MIT License，见 `LICENSE`。WorldSkills 竞赛资料的权利仍归原权利人。
