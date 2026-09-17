# B站动态与直播订阅

这是 [ff14-ddl-qbot 主体](https://github.com/lchlzk/ff14-ddl-qbot) 的可单独安装 NoneBot 插件，提供/bili 命令、动态与直播推送。

先安装并配置机器人主体（插件使用主体的 `bot_tools` 和 `message_ui` 公共模块），再进入同一个 Python 环境安装本仓库：

```bash
python -m pip install 'git+https://github.com/lchlzk/ff14-ddl-qbot-plugin-bililive.git'
```

机器人启动时加载 `plugins.bililive`。此插件不需要安装其他第一方插件；运行数据与密钥继续由主体管理，不随插件仓库公开。

许可证：AGPL-3.0-only。游戏和第三方素材的权利归原权利人所有。
