# B站动态与直播订阅

这是 [ff14-ddl-qbot 主体](https://github.com/lchlzk/ff14-ddl-qbot) 的完整 B站插件（0.2.0）。命令入口、动态/直播抓取、订阅存储、轮询推送、浏览器备用查询和动态卡片绘制均在本仓库，不再依赖主体中的 B站业务代码。

先安装并配置机器人主体（插件使用主体的 `bot_tools` 和 `message_ui` 公共模块），再进入同一个 Python 环境安装本仓库：

```bash
python -m pip install --upgrade 'git+https://github.com/lchlzk/ff14-ddl-qbot-plugin-bililive.git'
python -m playwright install chromium
```

Linux 手工部署需要浏览器系统依赖，可由管理员执行 `python -m playwright install --with-deps chromium`。新版主体 Docker 构建会自动安装；不安装本插件时，不再强制安装其浏览器依赖。卡片中文字体使用主体字体服务（Linux 建议 Noto CJK）。

机器人启动时加载 `plugins.bililive`。此插件不需要 FF14 或嘟嘟脸插件。请同时更新主体，避免旧版同名命令文件遮蔽安装包；Docker 部署需更新插件版本锁定文件并重建镜像。

## 功能和使用

群主或管理员在目标群发送：

```text
/bili 关注 UID
/bili 取关 UID
/bili 列表
/bili 已开播
/bili 开启直播 UID
/bili 关闭直播 UID
/bili 开启动态 UID
/bili 关闭动态 UID
/bili 状态
```

关注时同时开启直播和动态通知。首次检查建立当前位置，不补发旧动态。新动态会将正文与配图合成卡片并附原文链接，图片失败时保留文字降级。直播和动态轮询独立运行，多路公开接口和浏览器备用查询避免单一来源失败阻塞全部关注。

可在主体 `.env` 中设置（均非凭据）：

```dotenv
BILILIVE_LIVE_INTERVAL=60
BILILIVE_DYNAMIC_INTERVAL=120
BILILIVE_OFF_NOTIFY=false
```

订阅按机器人和群隔离。沿用主体 `BOT_DATA_DIR` 下原有数据库、`bililive-browser` 和 `secrets/bililive-targets-master.key`，升级无需重新订阅。务必同时备份数据库和密钥；卸载插件不会删除记录。后台的群开关与 `/command disable bili` 继续有效。

Webhook 模式也可推送，但是否送达取决于 QQ 开放平台批准的主动消息权限；B站接口可见时间、网络和风控也会影响延迟，不能保证固定时间内送达。不要求登录 B站，不安装原上游插件的 Web UI、OneBot 或 PostgreSQL。

## 源码与测试

- `src/plugins/bililive.py`：命令、独立轮询、QQ 主动推送和生命周期。
- `src/qbot_bililive/service.py`：用户/直播/动态查询、浏览器备用查询、订阅数据库和加密目标管理。
- `src/qbot_bililive/media.py`：动态图片卡片。
- `tests/`：接口、订阅、持久化、卡片、轮询及推送测试。

在已配置主体的环境下，将主体源码目录加入 `PYTHONPATH`，执行 `python -m unittest discover -s tests`。来源及改写说明见 [UPSTREAM.NOTICE.md](UPSTREAM.NOTICE.md)。运行数据和密钥不随仓库公开。

许可证：AGPL-3.0-only。游戏和第三方素材的权利归原权利人所有。
