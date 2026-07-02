# astrbot_plugin_jmcomic

JM 禁漫下载 AstrBot 插件。群友发的神作本jm号没法直接看？发的猎奇本没法预览有多逆天？交给bot吧，无论是先看两眼（好怪哦）还是当场开导，都能一键做到 ヽ(✿ﾟ▽ﾟ)ノ

## 指令

| 指令 | 说明 |
|------|------|
| `/jm <ID>` | 发送封面 + 基本信息 |
| `/jm_pdf <ID>` | 下载并合成 PDF 发送 |
| `/jm_pdf <ID> <页数>` | 前 N 页 PDF，如 `/jm_pdf 123456 100` |
| `/jm_pdf <ID> <起始>-<结束>` | 指定范围 PDF，如 `/jm_pdf 123456 10-50` |
| `/jm_search <关键词>` | 搜索本子 |
| `/jm_cancel` | 取消当前任务 + 清空队列 |

## 任务队列

忙时自动排队，完成后自动处理下一个：

```
你: /jm_pdf 111111
Bot: 正在下载 JM111111...

你: /jm_pdf 222222
Bot: 排队中，排第 1 位

你: /jm 333333
Bot: 排队中，排第 2 位

--- JM111111 完成 ---

Bot: 轮到你了！开始处理 JM222222...
```

`/jm_cancel` 会同时取消当前任务并清空整个队列。

## 安装

```bash
# SSH 到服务器
pip install jmcomic img2pdf Pillow

# 复制插件目录到 AstrBot 插件目录
cp -r astrbot_plugin_jmcomic /path/to/astrbot/addons/plugins/
```

> 如果 `curl-cffi` 编译失败：`pip install curl-cffi --prefer-binary`

## 配置

```json
{
    "pdf_max_pages": 200,
    "download_threads": 3,
    "temp_dir": null,
    "domains": ["jmcomic1.me", "jmcomic.me", "18comic.vip", "18comic.org"],
    "proxy": null,
    "quiet": true,
    "pdf_max_width": 1400,
    "pdf_jpeg_quality": 80
}
```

| 配置 | 说明 |
|------|------|
| `pdf_max_pages` | PDF 单次最大页数 |
| `proxy` | 国内服务器建议 `"http://127.0.0.1:7890"` |
| `pdf_max_width` | 图片压缩最大宽度，超过等比缩小 |
| `pdf_jpeg_quality` | JPEG 压缩质量，越低文件越小 |

## 致谢

- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) — 禁漫 Python API
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 多平台 chatbot 框架

## License

MIT
