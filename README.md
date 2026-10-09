# WorldSkills Sync

用**你自己的** WorldSkills 会员账号，把能访问的竞赛资料、资源库和公开名单归档到本地，并按届次、技能、类型、语言整理。也可以把已下载的正文打成 zip，放到 GitHub Releases，让其他人不用登录也能取用。

脚本是开源的。试题、技术描述等文件的版权仍属于 [WorldSkills International](https://worldskills.org/)，使用者需自行确认自己的会员权限和转载范围。本仓库不保存密码，也不提交登录令牌。

## 别人怎么用

### 1. 只要现成资料（不需要会员账号）

1. 克隆本仓库，查看 `data/` 名单和 `indexes/` 目录。
2. 打开 [GitHub Releases](https://github.com/AreaSong/worldskills-sync/releases)，按届次下载 zip。若 Releases 还是空的，说明正文尚未发布，请用下面的路径 2 自己下载。
3. 把 Release zip 解到本仓库的 `store/`，路径会与索引里的 `store_path` 一致。

```text
python sync.py unpack wsc-2026-shanghai-tp.zip
```

正文库里的试题本身常常还是 zip，那是网站发的原件，不要一次全部解开。要用时在搜索页点「解压」，或：

```text
python sync.py extract --path WSC2015/TP/34/actual/und/WSC2015_TP34_pre.zip --open
```

解开的内容在 `work/`，`store/` 里的原件不动。

### 2. 想自己再下一份（需要你自己的会员账号）

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chrome

python sync.py login          # 用你自己的账号登录会员区
python sync.py download       # 默认 4 路并发下载到 store/
python sync.py progress --open
python sync.py search --open      # 搜索已下载资料、试题包内文件名、成绩名单
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
python sync.py search --open         # 按技能、文件名、选手姓名搜索
python sync.py extract --path ...    # 把某个试题 zip 解到 work/
python sync.py unpack release.zip    # 把 GitHub Release zip 解到 store/
python sync.py data                  # 刷新名单表（成绩、成员等）
python sync.py pack                  # 按届次打 zip 到 dist/
python sync.py publish               # 打包并上传 GitHub Releases
python sync.py zipindex              # 扫描试题 zip 内部文件名，供搜索使用
python sync.py --self-test
```

## 试题是 zip，搜索做什么

WorldSkills 发出来的试题本来就是 zip，正文库也按原件保存。搜索不是替你把几千个包全解开，而是帮你找到**该下哪一个、该解哪一个**：技能编号、项目名、外层文件名，以及包里的内部文件名。本机搜到后可以「打开」或「解压」到 `work/`。

## 公开目录（GitHub Pages）

站点只托管**索引**，不托管试题正文：

- 页面：仓库根目录的 `index.html`
- 目录：`indexes/catalog.csv`、`indexes/zip-members.json`
- 成绩：`data/results.csv`
- 正文：GitHub Releases 分卷 zip

打开 [公开目录](https://areasong.github.io/worldskills-sync/)。搜到条目后，若对应分卷已发布，会给出 Release 下载链接；还没打包上传时显示「待发布」。GitHub 不能按 zip 内单文件下载，所以流程是「搜到 → 下分卷 → `python sync.py unpack` 进 `store/` → 需要时再 extract」。


本地下载日志：`downloads/download.log`。

## 要求

- Python 3.10+
- 自己下载时需要 WorldSkills 会员，以及本机 Chrome（Playwright 使用系统 Chrome）
- 发布 Releases 需要已登录的 [GitHub CLI](https://cli.github.com/)（`gh`）

## 不收录

人员名册与照片、出生日期、会员通讯录、报名岗位名单等个人敏感信息。竞赛文档里打不开的栏目（例如部分通信页 HTTP 500）无法归档。Flickr 照片和 YouTube 视频只保留链接，不镜像整站。

## 许可

脚本与文档为 MIT License，见 `LICENSE`。WorldSkills 竞赛资料的权利仍归原权利人。
