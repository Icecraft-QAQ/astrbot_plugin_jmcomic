"""
AstrBot 插件 — JM 禁漫本子下载
==============================
/jm <ID>      → 发送封面 + 信息
/jm_pdf <ID>  → 下载并发送 PDF
支持任务队列：忙时自动排队，完成后自动处理下一个。
针对 2G 内存小服务器优化。
"""

import os
import re
import asyncio
import tempfile
import shutil

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

from jmcomic import create_option_by_str

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "pdf_max_pages": 200,  # PDF 模式单次最大页数
    "download_threads": 3,  # 下载并发数
    "temp_dir": None,  # 临时目录（None = 系统 /tmp）
    "domains": ["jmcomic1.me", "jmcomic.me", "18comic.vip", "18comic.org"],
    "proxy": None,  # HTTP 代理，如 "http://127.0.0.1:7890"
    "quiet": True,  # 关掉 JM 库日志
    "pdf_max_width": 1400,  # PDF 图片最大宽度（超过等比缩小）
    "pdf_jpeg_quality": 80,  # PDF 图片 JPEG 质量 1-100
}

# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


@register(
    "astrbot_plugin_jmcomic",
    "icechan",
    "JM 禁漫下载：封面 / PDF，支持排队 (￣▽￣)~*",
    "1.2.0",
)
class JMComicPlugin(Star):
    """
    /jm <ID>            → 发送封面 + 信息
    /jm_pdf <ID> [页数]  → 下载合并 PDF 发送
    /jm_search <关键词>  → 搜索本子
    /jm_cancel          → 取消当前任务 & 清空队列
    """

    def __init__(self, context: Context):
        super().__init__(context)
        self.config = dict(DEFAULT_CONFIG)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._busy = False
        self._cancel_flag = False

    # ---- 生命周期 ----------------------------------------------------------

    async def initialize(self):
        cfg = self.config
        if cfg["temp_dir"]:
            os.makedirs(cfg["temp_dir"], exist_ok=True)
        try:
            user_cfg = self.context.get_config()
            if user_cfg:
                cfg.update(user_cfg)
                logger.info("已加载用户配置 (￣▽￣)~*")
        except Exception:
            pass
        logger.info("JM 插件初始化完成 ヽ(✿ﾟ▽ﾟ)ノ")

    async def terminate(self):
        self._cancel_flag = True
        # 清空队列
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        tmp = self.config.get("temp_dir")
        if tmp and os.path.isdir(tmp):
            try:
                shutil.rmtree(tmp)
            except Exception:
                pass

    # ---- 工具方法 ----------------------------------------------------------

    def _make_option(self):
        cfg = self.config
        yaml_config = f"""
log: {not cfg["quiet"]}
dir_rule:
  base_dir: {cfg["temp_dir"] or tempfile.gettempdir()}
  rule: Bd_Aid

client:
  impl: api
  domain:
    html: {cfg["domains"]}

download:
  threading:
    image: {cfg["download_threads"]}
    photo: 1
  image:
    decode: true
    suffix: .jpg
  cache: false
"""
        option = create_option_by_str(yaml_config)
        if cfg.get("proxy"):
            try:
                option.client.proxy = cfg["proxy"]
            except Exception:
                pass
        return option

    def _parse_args(self, event: AstrMessageEvent, max_pages_key="pdf_max_pages"):
        """解析 → (album_id, start, end) 或 None"""
        text = event.message_str.strip()
        parts = text.split()
        if len(parts) < 2:
            return None

        album_id = parts[1].strip()
        if not re.match(r"^\d+$", album_id):
            return None

        start = 1
        end = self.config[max_pages_key]

        if len(parts) >= 3:
            arg = parts[2].strip()
            if "-" in arg:
                m = re.match(r"^(\d+)-(\d+)$", arg)
                if not m:
                    return None
                start = int(m.group(1))
                end = int(m.group(2))
            else:
                if not arg.isdigit():
                    return None
                end = int(arg)

        if start < 1:
            start = 1
        if end < start:
            return None
        max_allowed = self.config[max_pages_key]
        if end - start + 1 > max_allowed:
            end = start + max_allowed - 1

        return album_id, start, end

    async def _fetch_album_and_client(self, album_id: str):
        option = self._make_option()
        client = await asyncio.to_thread(option.build_jm_client)
        album = await asyncio.to_thread(client.get_album_detail, album_id)
        return album, client

    # ---- 队列管理 ----------------------------------------------------------

    async def _process_queue(self):
        """处理完当前任务后，自动消费队列中下一个"""
        while not self._queue.empty() and not self._cancel_flag:
            item = await self._queue.get()
            origin = item["origin"]
            mode = item["mode"]

            # 通知排队用户：轮到你了
            await self._send_proactive(origin, "轮到你了！开始处理...")

            if mode == "pdf":
                await self._do_download_pdf_proactive(
                    origin, item["album_id"], item["start"], item["end"]
                )
            elif mode == "cover":
                await self._do_send_cover_proactive(origin, item["album_id"])

        self._busy = False

    async def _send_proactive(self, origin: str, text: str):
        """队列模式下发送纯文本"""
        try:
            await self.context.send_message(origin, MessageChain().message(text))
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

    # ---- 搜索 --------------------------------------------------------------

    @filter.command("jm_search")
    async def jm_search(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(
                "用法: /jm_search <关键词> [like]\n"
                "  默认按最新排序，加 like 按喜欢数排序\n"
                "  例如: /jm_search 全彩 人妻\n"
                "       /jm_search 全彩 like"
            )
            return

        # 解析关键词和排序方式
        raw = parts[1].strip()
        tokens = raw.rsplit(maxsplit=1)
        if tokens[-1].lower() in ("like", "喜欢"):
            keyword = tokens[0].strip()
            order_by = "tf"  # 喜欢最多
            sort_label = "喜欢最多"
        else:
            keyword = raw
            order_by = "mr"  # 最新
            sort_label = "最新"

        yield event.plain_result(f"正在搜索「{keyword}」（{sort_label}）...")

        try:
            # 分段 try 便于定位 "list index out of range" 来源
            try:
                option = self._make_option()
            except Exception:
                logger.error("搜索失败: _make_option", exc_info=True)
                raise

            try:
                client = await asyncio.to_thread(option.build_jm_client)
            except Exception:
                logger.error("搜索失败: build_jm_client", exc_info=True)
                raise

            try:
                page = await asyncio.to_thread(
                    client.search_site, keyword, page=1, order_by=order_by
                )
            except Exception:
                logger.error("搜索失败: search_site", exc_info=True)
                raise

            if not page or len(page) == 0:
                yield event.plain_result(f"没搜到「{keyword}」相关结果 (´-ι_-｀)")
                return

            # 搜索结果 items 是 (album_id, info_dict) 元组
            lines = [f"搜索「{keyword}」（{sort_label}）的结果:"]
            for i, item in enumerate(page[:10]):
                aid, info = item
                title = str(info.get("name", "未知"))
                if len(title) > 40:
                    title = title[:37] + "..."
                author = str(info.get("author", info.get("authors", "未知")))
                pages = info.get("page_count", "?")
                lines.append(f"{i + 1}. JM{aid} | 【{author}】{title} ({pages}P)")

            yield event.plain_result("\n".join(lines))

        except Exception as e:
            yield event.plain_result(
                f"搜索失败: {str(e)[:100]} （´_ゝ`）\n"
                f"可能是 JM 服务器抽风或关键词太生僻，换个词试试？"
            )

    # ---- 取消 --------------------------------------------------------------

    @filter.command("jm_cancel")
    async def jm_cancel(self, event: AstrMessageEvent):
        if not self._busy:
            yield event.plain_result("当前没有进行中的任务 (￣_￣|||)")
            return
        self._cancel_flag = True
        # 清空队列
        cleared = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                cleared += 1
            except asyncio.QueueEmpty:
                break
        msg = "已取消，正在停止..."
        if cleared > 0:
            msg += f" 队列中 {cleared} 个任务已清空"
        yield event.plain_result(f"{msg} (；´д｀)ゞ")

    # ---- /jm — 封面模式 ----------------------------------------------------

    async def _do_send_cover_yield(self, event: AstrMessageEvent, album_id: str):
        """直接模式：yield 封面+信息"""
        yield event.plain_result(f"正在获取 JM{album_id} 的信息...")

        try:
            album, client = await self._fetch_album_and_client(album_id)
        except Exception as e:
            yield event.plain_result(f"获取 JM{album_id} 失败: {str(e)[:100]}")
            return

        info = (
            f"【{album.author}】{album.oname}\n"
            f"JM号: {album_id} | 章节: {len(album)} | "
            f"总页数: {album.page_count if album.page_count > 0 else '?'}\n"
            f"标签: {getattr(album, 'tags', '无')}\n"
            f"想看全本请用 /jm_pdf {album_id}"
        )

        # 下载封面
        tmp_root = self.config["temp_dir"] or tempfile.gettempdir()
        tmp_dir = tempfile.mkdtemp(prefix=f"jm_cover_{album_id}_", dir=tmp_root)
        try:
            cover_path = os.path.join(tmp_dir, f"cover_{album_id}.jpg")
            await asyncio.to_thread(client.download_album_cover, album_id, cover_path)

            # 先发文字、再发图片，拆开避免 QQ NT 超时
            yield event.plain_result(info)

            if os.path.isfile(cover_path) and os.path.getsize(cover_path) > 0:
                yield event.chain_result([Comp.Image.fromFileSystem(cover_path)])
            else:
                yield event.plain_result("(封面下载失败)")
        except Exception:
            yield event.plain_result(info + "\n(封面下载失败)")
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass

    async def _do_send_cover_proactive(self, origin: str, album_id: str):
        """队列模式：proactive 发送封面+信息"""
        try:
            album, client = await self._fetch_album_and_client(album_id)
        except Exception as e:
            await self._send_proactive(
                origin, f"获取 JM{album_id} 失败: {str(e)[:100]}"
            )
            return

        info = (
            f"【{album.author}】{album.oname}\n"
            f"JM号: {album_id} | 章节: {len(album)} | "
            f"总页数: {album.page_count if album.page_count > 0 else '?'}\n"
            f"标签: {getattr(album, 'tags', '无')}\n"
            f"想看全本请用 /jm_pdf {album_id}"
        )

        tmp_root = self.config["temp_dir"] or tempfile.gettempdir()
        tmp_dir = tempfile.mkdtemp(prefix=f"jm_cover_{album_id}_", dir=tmp_root)
        try:
            cover_path = os.path.join(tmp_dir, f"cover_{album_id}.jpg")
            await asyncio.to_thread(client.download_album_cover, album_id, cover_path)

            # 先发文字、再发图片，拆开避免 QQ NT 超时
            await self._send_proactive(origin, info)

            if os.path.isfile(cover_path) and os.path.getsize(cover_path) > 0:
                await self.context.send_message(
                    origin, MessageChain().file_image(cover_path)
                )
            else:
                await self._send_proactive(origin, "(封面下载失败)")
        except Exception:
            await self._send_proactive(origin, info + "\n(封面下载失败)")
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass

    @filter.command("jm")
    async def jm_cover(self, event: AstrMessageEvent):
        """发送封面 + 信息"""
        text = event.message_str.strip()
        parts = text.split()
        if len(parts) < 2 or not re.match(r"^\d+$", parts[1].strip()):
            yield event.plain_result(
                "用法: /jm <本子ID>\n"
                "  发送封面 + 基本信息\n"
                "  例如: /jm 123456\n\n"
                "下载全本 PDF: /jm_pdf <ID>"
            )
            return

        album_id = parts[1].strip()
        item = {
            "origin": event.unified_msg_origin,
            "album_id": album_id,
            "mode": "cover",
        }

        if self._busy:
            await self._queue.put(item)
            position = self._queue.qsize()
            yield event.plain_result(
                f"当前有任务进行中，JM{album_id} 已加入队列，排第 {position} 位 (´-ι_-｀)"
            )
            return

        # 直接处理
        self._busy = True
        self._cancel_flag = False
        try:
            async for result in self._do_send_cover_yield(event, album_id):
                yield result
        finally:
            if not self._cancel_flag:
                await self._process_queue()
            else:
                self._busy = False

    # ---- /jm_pdf — PDF 下载模式 --------------------------------------------

    async def _download_pages(self, client, album, start: int, end: int, tmp_dir: str):
        """下载指定范围的图片到临时目录，返回已下载的文件路径列表"""
        downloaded = []
        total_pages = album.page_count
        known_pages = total_pages > 0
        if not known_pages:
            total_pages = end
        actual_end = min(end, total_pages)

        page_index = 0
        for photo in album:
            if self._cancel_flag:
                break
            await asyncio.to_thread(client.check_photo, photo)
            for image in photo:
                page_index += 1
                if page_index < start:
                    continue
                if page_index > actual_end:
                    break

                if self._cancel_flag:
                    break

                img_filename = f"{page_index:04d}{image.img_file_suffix}"
                img_path = os.path.join(tmp_dir, img_filename)
                decode = not image.is_gif

                try:
                    await asyncio.to_thread(
                        client.download_by_image_detail,
                        image,
                        img_path,
                        decode,
                    )
                    downloaded.append(img_path)
                except Exception as e:
                    logger.warning(f"图片下载失败 [{page_index}]: {e}")
                    continue

            if page_index > actual_end:
                break

        return downloaded

    async def _compress_and_merge(
        self, downloaded: list, tmp_dir: str, album, album_id: str
    ):
        """压缩图片 + 合成 PDF，返回 pdf 路径"""
        # 压缩
        batch_size = 25
        for i in range(0, len(downloaded), batch_size):
            batch = downloaded[i : i + batch_size]
            await asyncio.to_thread(
                _compress_batch,
                batch,
                self.config["pdf_max_width"],
                self.config["pdf_jpeg_quality"],
            )
            if self._cancel_flag:
                return None

        # 合成 PDF
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", f"{album.author} - {album.oname}")
        pdf_filename = f"JM{album_id} {safe_name}.pdf"
        if len(pdf_filename) > 200:
            pdf_filename = f"JM{album_id}.pdf"
        pdf_path = os.path.join(tmp_dir, pdf_filename)

        await asyncio.to_thread(_images_to_pdf, downloaded, pdf_path)
        return pdf_path

    async def _do_download_pdf_yield(
        self, event: AstrMessageEvent, album_id: str, start: int, end: int
    ):
        """直接模式：yield 下载进度 → 发送 PDF"""
        tmp_root = self.config["temp_dir"] or tempfile.gettempdir()
        tmp_dir = tempfile.mkdtemp(prefix=f"jm_pdf_{album_id}_", dir=tmp_root)

        try:
            # 获取信息
            yield event.plain_result(f"正在获取 JM{album_id} 的信息...")
            album, client = await self._fetch_album_and_client(album_id)

            total_pages = album.page_count
            known_pages = total_pages > 0
            if not known_pages:
                total_pages = end
            actual_end = min(end, total_pages)

            yield event.plain_result(
                f"【{album.author}】{album.oname}\n"
                f"JM号: {album_id} | 章节: {len(album)} | "
                f"总页数: {album.page_count if known_pages else '?'}\n"
                f"预计下载: 第 {start} ~ {actual_end} 页"
            )

            # 下载
            downloaded = await self._download_pages(client, album, start, end, tmp_dir)

            if self._cancel_flag:
                yield event.plain_result("下载已取消 _(:з」∠)_")
                return

            if not downloaded:
                yield event.plain_result(
                    f"JM{album_id} 没有成功下载任何图片 (；´д｀)ゞ"
                )
                return

            # 压缩 + 合成（无进度啰嗦，直接搞）
            yield event.plain_result(f"正在合成 PDF（共 {len(downloaded)} 页）...")
            pdf_path = await self._compress_and_merge(
                downloaded, tmp_dir, album, album_id
            )

            if self._cancel_flag:
                yield event.plain_result("下载已取消 _(:з」∠)_")
                return
            if pdf_path is None:
                yield event.plain_result("PDF 合成失败 (´-ι_-｀)")
                return

            pdf_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)

            yield event.plain_result(
                f"JM{album_id} PDF 合成完毕！共 {len(downloaded)} 页 | {pdf_size_mb:.1f} MB\n发送中..."
            )

            # 发送 PDF — 先文字再文件，拆开避免 QQ NT 超时
            info_text = (
                f"JM{album_id} 【{album.author}】{album.oname}\n"
                f"共 {len(downloaded)} 页 | {pdf_size_mb:.1f} MB"
            )
            yield event.plain_result(info_text)

            try:
                yield event.chain_result(
                    [
                        Comp.File(file=pdf_path, name=os.path.basename(pdf_path)),
                    ]
                )
            except Exception as e:
                logger.error(f"PDF 发送失败: {e}")
                yield event.plain_result(
                    "PDF 发送失败: 可能当前平台不支持文件类型 (´-ι_-｀)"
                )

        except Exception as e:
            logger.error(f"PDF 下载异常: {e}", exc_info=True)
            yield event.plain_result(
                f"下载 JM{album_id} 时出错: {str(e)[:200]}\n"
                f"可能原因: 本子不存在 / 域名被墙 / 网络超时"
            )
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass

    async def _do_download_pdf_proactive(
        self, origin: str, album_id: str, start: int, end: int
    ):
        """队列模式：proactive 发送下载进度 → 发送 PDF"""
        tmp_root = self.config["temp_dir"] or tempfile.gettempdir()
        tmp_dir = tempfile.mkdtemp(prefix=f"jm_pdf_{album_id}_", dir=tmp_root)

        try:
            await self._send_proactive(origin, f"正在获取 JM{album_id} 的信息...")
            album, client = await self._fetch_album_and_client(album_id)

            total_pages = album.page_count
            known_pages = total_pages > 0
            if not known_pages:
                total_pages = end
            actual_end = min(end, total_pages)

            await self._send_proactive(
                origin,
                f"【{album.author}】{album.oname}\n"
                f"JM号: {album_id} | 章节: {len(album)} | "
                f"总页数: {album.page_count if known_pages else '?'}\n"
                f"预计下载: 第 {start} ~ {actual_end} 页",
            )

            downloaded = await self._download_pages(client, album, start, end, tmp_dir)

            if self._cancel_flag:
                await self._send_proactive(origin, "下载已取消 _(:з」∠)_")
                return
            if not downloaded:
                await self._send_proactive(
                    origin, f"JM{album_id} 没有成功下载任何图片 (；´д｀)ゞ"
                )
                return

            await self._send_proactive(
                origin, f"正在合成 PDF（共 {len(downloaded)} 页）..."
            )
            pdf_path = await self._compress_and_merge(
                downloaded, tmp_dir, album, album_id
            )

            if self._cancel_flag:
                await self._send_proactive(origin, "下载已取消 _(:з」∠)_")
                return
            if pdf_path is None:
                await self._send_proactive(origin, "PDF 合成失败 (´-ι_-｀)")
                return

            pdf_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
            pdf_name = os.path.basename(pdf_path)

            # 发送 PDF — 先文字再文件，拆开避免 QQ NT 超时
            await self._send_proactive(
                origin,
                f"JM{album_id} 【{album.author}】{album.oname}\n"
                f"共 {len(downloaded)} 页 | {pdf_size_mb:.1f} MB",
            )

            try:
                await self.context.send_message(
                    origin,
                    MessageChain(chain=[Comp.File(file=pdf_path, name=pdf_name)]),
                )
            except Exception as e:
                logger.error(f"PDF 发送失败: {e}")
                await self._send_proactive(
                    origin, "PDF 发送失败: 可能当前平台不支持文件类型 (´-ι_-｀)"
                )

        except Exception as e:
            logger.error(f"PDF 下载异常: {e}", exc_info=True)
            await self._send_proactive(
                origin,
                f"下载 JM{album_id} 时出错: {str(e)[:200]}\n"
                f"可能原因: 本子不存在 / 域名被墙 / 网络超时",
            )
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass

    @filter.command("jm_pdf")
    async def jm_pdf(self, event: AstrMessageEvent):
        """下载 PDF"""
        parsed = self._parse_args(event, "pdf_max_pages")
        if parsed is None:
            yield event.plain_result(
                "用法: /jm_pdf <本子ID> [页数|起始-结束]\n"
                "  /jm_pdf 123456        → 前 200 页 PDF\n"
                "  /jm_pdf 123456 100     → 前 100 页 PDF\n"
                "  /jm_pdf 123456 10-50   → 第 10~50 页 PDF"
            )
            return

        album_id, start, end = parsed
        item = {
            "origin": event.unified_msg_origin,
            "album_id": album_id,
            "start": start,
            "end": end,
            "mode": "pdf",
        }

        if self._busy:
            await self._queue.put(item)
            position = self._queue.qsize()
            yield event.plain_result(
                f"当前有任务进行中，JM{album_id} PDF 已加入队列，排第 {position} 位 (´-ι_-｀)"
            )
            return

        # 直接处理
        self._busy = True
        self._cancel_flag = False
        try:
            async for result in self._do_download_pdf_yield(
                event, album_id, start, end
            ):
                yield result
        finally:
            if not self._cancel_flag:
                await self._process_queue()
            else:
                self._busy = False


# ---------------------------------------------------------------------------
# 工具函数（在线程中执行）
# ---------------------------------------------------------------------------


def _compress_batch(paths: list, max_width: int, quality: int):
    """压缩一批图片、原地替换，降低 PDF 合成的内存压力"""
    from PIL import Image

    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            img = Image.open(p)
            if img.mode in ("RGBA", "P", "LA", "PA"):
                img = img.convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")

            w, h = img.size
            if w > max_width:
                ratio = max_width / w
                img = img.resize((max_width, int(h * ratio)), Image.LANCZOS)

            img.save(p, "JPEG", quality=quality, optimize=True)
            img.close()
        except Exception:
            continue


def _images_to_pdf(image_paths: list, pdf_path: str):
    """将图片列表合并为 PDF"""
    import img2pdf

    valid = [p for p in image_paths if os.path.isfile(p)]
    if not valid:
        raise RuntimeError("没有可用的图片文件")

    with open(pdf_path, "wb") as f:
        f.write(img2pdf.convert(valid))
